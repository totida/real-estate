#!/usr/bin/env python3
"""네이버 부동산 신규 매물 알림.

config.json 에 지정한 단지들의 매물 목록을 네이버 부동산(fin.land)에서
조회하고, 이전 실행 때 없던 매물이 생기면 GitHub 이슈 / 텔레그램으로 알린다.
매물 조회에는 크로미움이 필요하고, 파이썬은 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import base64
import csv
import html
import json
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"
HISTORY_PATH = ROOT / "state" / "history.csv"
# 메일·텔레그램 계정 같은 비밀값. 저장소가 공개라 git 에 올리지 않는다 (.gitignore).
LOCAL_PATH = ROOT / "local.json"

KST = timezone(timedelta(hours=9))
M2_PER_PYEONG = 3.305785
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TRADE_NAMES = {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}


def setting(name: str, default: str = "") -> str:
    """환경변수, 없으면 local.json 에서 설정값을 읽는다 (cron 에서는 ~/.bashrc 가 안 읽히므로)."""
    if os.getenv(name):
        return os.environ[name]
    try:
        value = json.loads(LOCAL_PATH.read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError):
        value = None
    return str(value) if value else default


# ---------------------------------------------------------------- HTTP

def retry_delay(err: Exception, attempt: int) -> float:
    """재시도 전 대기 시간(초). 429(요청 과다)는 Retry-After 를 따르거나 길게 쉰다."""
    if isinstance(err, urllib.error.HTTPError) and err.code == 429:
        try:
            return min(float(err.headers.get("Retry-After", "")), 120)
        except (TypeError, ValueError):
            return 20 * (attempt + 1)
    return 2 * (attempt + 1)


# 브라우저가 보내는 헤더와 비슷하게 맞춰야 네이버가 빈 응답/429 를 덜 준다.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": '"Chromium";v="128", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def http_get(url: str, headers: dict | None = None, retries: int = 3) -> str:
    hdrs = dict(BROWSER_HEADERS)
    hdrs.update(headers or {})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(retry_delay(e, attempt))
    raise RuntimeError(f"GET {url} failed: {last_err}")


def http_json(url: str, headers: dict | None = None) -> dict:
    """JSON 객체를 돌려준다. 빈 응답·HTML(차단 페이지 등)이면 앞부분을 담아 예외를 낸다."""
    text = http_get(url, headers)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise RuntimeError(f"예상 밖 응답: {text[:200]!r}")
    return data


# ---------------------------------------------------------------- Browser (Chrome DevTools)
#
# fin.land 매물 API 는 Python/curl 로 직접 부르면 429(TOO_MANY_REQUESTS)로 막힌다.
# 실제 크로미움을 화면 없이 띄워 fin.land 페이지 안에서 fetch() 를 실행하면 통과된다.
# 추가 패키지 없이 쓰려고 DevTools 프로토콜(WebSocket)을 표준 라이브러리로 직접 다룬다.

class BlockedError(RuntimeError):
    """네이버가 429 로 막았다. 더 요청하면 차단이 길어지므로 즉시 멈춘다."""


class BrowserError(RuntimeError):
    """크로미움을 띄우거나 fin.land 페이지를 여는 데 실패했다 (다른 단지도 어차피 실패)."""


class _WebSocket:
    """DevTools 연결에 필요한 만큼만 구현한 WebSocket 클라이언트 (텍스트 프레임)."""

    def __init__(self, url: str, timeout: float = 60) -> None:
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = bytearray()
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket 연결 실패: {head[:200]!r}")

    def _fill(self) -> None:
        chunk = self.sock.recv(1 << 16)
        if not chunk:
            raise ConnectionError("WebSocket 연결이 끊겼습니다")
        self.buf += chunk

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        head = bytearray([0x81])  # FIN + 텍스트
        if n < 126:
            head.append(0x80 | n)
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + mask + body)

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            if b1 & 0x80:
                self._read(4)  # 서버 프레임은 마스킹하지 않지만 혹시 모를 경우 건너뜀
            payload = self._read(n)
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise ConnectionError("WebSocket 이 닫혔습니다")
            if opcode in (0x9, 0xA):  # ping/pong
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def find_chromium() -> str:
    """크로미움 실행 파일. 환경변수 CHROMIUM 으로 직접 지정할 수 있다."""
    for cand in (os.getenv("CHROMIUM"), "chromium-browser", "chromium", "google-chrome",
                 "google-chrome-stable", "/opt/pw-browsers/chromium"):
        if cand and (shutil.which(cand) or Path(cand).is_file()):
            return shutil.which(cand) or cand
    # 패키지마다 이름이 달라 PATH 에서 'chrom' 이 들어간 실행 파일을 찾아본다
    for d in os.getenv("PATH", "").split(os.pathsep):
        for f in sorted(Path(d).glob("*chrom*")) if d and Path(d).is_dir() else []:
            if "driver" not in f.name and f.is_file() and os.access(f, os.X_OK):
                return str(f)
    raise RuntimeError("크로미움을 찾지 못했습니다. Termux 에서 "
                       "'pkg install tur-repo x11-repo && pkg install chromium' 으로 설치하세요.")


def start_xvfb() -> tuple[subprocess.Popen, str]:
    """가상 화면(Xvfb)을 띄우고 DISPLAY 값을 돌려준다."""
    r, w = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x800x24",
                             "-nolisten", "tcp"], pass_fds=(w,),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    with os.fdopen(r) as f:
        num = f.readline().strip()
    if not num:
        proc.kill()
        raise RuntimeError("Xvfb 실행 실패")
    return proc, f":{num}"


def open_browser() -> "Chromium":
    """화면 없는 모드로 먼저 띄우고, 안 되면 가상 화면(Xvfb)에서 일반 모드로 띄운다.

    환경변수 CHROMIUM_XVFB=1 이면 처음부터 Xvfb 를 쓴다.
    """
    exe = find_chromium()
    if os.getenv("CHROMIUM_XVFB") != "1":
        try:
            return Chromium(exe)
        except RuntimeError as e:
            if not shutil.which("Xvfb"):
                raise
            print(f"  화면 없는 모드 실패, Xvfb 로 재시도: {str(e)[-300:]}", file=sys.stderr)
    return Chromium(exe, xvfb=True)


class Chromium:
    """크로미움 한 개와 페이지 한 개를 DevTools 로 조종한다."""

    def __init__(self, exe: str | None = None, xvfb: bool = False,
                 start_timeout: float = 60) -> None:
        self.profile = Path(tempfile.mkdtemp(prefix="naver-alert-chrome-"))
        self.log = open(self.profile / "chrome.log", "wb")
        self.xvfb: subprocess.Popen | None = None
        self.proc: subprocess.Popen | None = None
        self.ws: _WebSocket | None = None
        self.session = ""
        self._id = 0
        try:
            env = dict(os.environ)
            mode = ["--headless=new"]
            if xvfb:
                self.xvfb, env["DISPLAY"] = start_xvfb()
                mode = ["--window-size=1280,800"]
            args = [exe or find_chromium(), *mode, "--no-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled", "--lang=ko-KR",
                    "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                    *os.getenv("CHROMIUM_FLAGS", "").split(), "about:blank"]
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self.log, env=env)
            self._connect(start_timeout)
        except Exception:
            self.close()
            raise

    def _connect(self, timeout: float) -> None:
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while True:
            lines = port_file.read_text().split() if port_file.exists() else []
            if len(lines) >= 2:
                break
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self.log.flush()
                tail = (self.profile / "chrome.log").read_bytes()[-600:].decode(errors="replace")
                raise RuntimeError(f"크로미움 실행 실패:\n{tail}")
            time.sleep(0.2)
        self.ws = _WebSocket(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget",
                                 {"targetId": target, "flatten": True})["sessionId"]
        # 'HeadlessChrome' 표시를 지워 일반 크롬과 같게 보이게 한다.
        ver = self.call("Browser.getVersion")
        ua = ver["userAgent"].replace("HeadlessChrome", "Chrome")
        full = ver["product"].split("/")[-1]
        major = full.split(".")[0]
        self.call("Emulation.setUserAgentOverride", {
            "userAgent": ua, "acceptLanguage": "ko-KR,ko;q=0.9",
            "userAgentMetadata": {
                "brands": [{"brand": "Chromium", "version": major},
                           {"brand": "Not_A Brand", "version": "24"}],
                "fullVersion": full, "platform": "Linux", "platformVersion": "",
                "architecture": "", "model": "", "mobile": False},
        }, page=True)
        self.call("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
            page=True)

    def call(self, method: str, params: dict | None = None, page: bool = False,
             timeout: float = 60) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if page:
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = json.loads(self.ws.recv())
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})
        raise TimeoutError(method)

    def goto(self, url: str, timeout: float = 45) -> None:
        res = self.call("Page.navigate", {"url": url}, page=True)
        if res.get("errorText"):
            raise RuntimeError(f"{url} 열기 실패: {res['errorText']}")
        deadline = time.monotonic() + timeout
        loaded = 'document.readyState === "complete" && location.href !== "about:blank"'
        while not self.evaluate(loaded):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} 로딩 시간 초과")
            time.sleep(0.5)

    def evaluate(self, expression: str):
        res = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                             "returnByValue": True}, page=True)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
        return (res.get("result") or {}).get("value")

    def fetch_json(self, url: str, payload: dict) -> tuple[int, str]:
        """페이지 안에서 POST fetch 를 실행해 (상태코드, 본문) 을 돌려준다."""
        body = json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)
        js = f"""(async () => {{
            const r = await fetch({json.dumps(url)}, {{
                method: "POST", credentials: "include", body: {body},
                headers: {{"content-type": "application/json",
                          "accept": "application/json, text/plain, */*"}}}});
            return {{status: r.status, text: await r.text()}};
        }})()"""
        out = self.evaluate(js)
        return out["status"], out["text"]

    def close(self) -> None:
        if self.ws:
            try:
                self.call("Browser.close", timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self.ws.close()
        for proc in (self.proc, self.xvfb):
            if proc is None:
                continue
            if proc is self.xvfb or self.ws is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.log.close()
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------- Naver

class NaverLand:
    """매물은 fin.land.naver.com API 를 크로미움 안에서 호출해 조회한다."""

    NEW = "https://new.land.naver.com"
    FIN = "https://fin.land.naver.com"
    # 단지 페이지(/complexes/…)는 다른 오리진에서 그려져 fetch 가 CORS 로 막히므로 지도 페이지를 연다.
    START_PAGE = "/map"
    ARTICLE_API = "/front-api/v1/complex/article/list"

    def __init__(self, fin: str | None = None) -> None:
        self.fin = fin or self.FIN
        self._token: str | None = None
        self._browser: Chromium | None = None

    def _new_headers(self) -> dict:
        h = {"Referer": f"{self.NEW}/complexes", "Accept": "application/json"}
        if self._token is None:
            try:
                html = http_get(f"{self.NEW}/complexes")
                m = re.search(r'"token"\s*:\s*"([A-Za-z0-9._-]+)"', html)
                self._token = m.group(1) if m else ""
            except RuntimeError:
                self._token = ""
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def search_complexes(self, keyword: str) -> list[dict]:
        q = urllib.parse.quote(keyword)
        data = http_json(f"{self.NEW}/api/search?keyword={q}", self._new_headers())
        return [
            {"complexNo": str(c["complexNo"]), "complexName": c.get("complexName", ""),
             "address": c.get("cortarAddress", "")}
            for c in data.get("complexes", [])
        ]

    def _page(self) -> Chromium:
        if self._browser is None:
            try:
                browser = open_browser()
            except Exception as e:
                raise BrowserError(str(e)) from e
            try:
                browser.goto(self.fin + self.START_PAGE)
                time.sleep(2)  # 페이지 스크립트가 쿠키 등을 준비할 시간
                origin = browser.evaluate("location.origin")
                if origin != self.fin:
                    raise RuntimeError(f"지도 페이지가 다른 주소로 이동했습니다: {origin}")
            except Exception as e:
                browser.close()
                raise BrowserError(f"fin.land 페이지 열기 실패: {e}") from e
            self._browser = browser
        return self._browser

    def fetch_page(self, complex_no: str, trade_types: list[str], last_info: list) -> dict:
        """매물 목록 한 페이지(result)."""
        payload = {
            "size": 30, "complexNumber": complex_no, "tradeTypes": trade_types,
            "pyeongTypes": [], "dongNumbers": [], "userChannelType": "PC",
            # seed 고정: 매번 바꾸면 순서·대표 매물이 달라져 변동 추적이 흔들린다
            "articleSortType": "RANKING_DESC", "seed": "naver-alert", "lastInfo": last_info,
        }
        status, text = self._page().fetch_json(self.fin + self.ARTICLE_API, payload)
        if status == 429:
            raise BlockedError(f"네이버가 요청을 막았습니다(429): {text[:200]}")
        if status != 200:
            raise RuntimeError(f"매물 API 응답 {status}: {text[:200]}")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        # 실패 응답을 '매물 0건' 으로 저장하면 다음 조회 때 모든 매물이 새 매물로 알림되므로 오류로 처리
        if (not isinstance(data, dict) or data.get("isSuccess") is False
                or not isinstance(data.get("result"), dict)):
            raise RuntimeError(f"매물 API 예상 밖 응답: {text[:200]}")
        return data["result"]

    def articles(self, complex_no: str, trade_types: list[str]) -> list[dict]:
        out: list[dict] = []
        last_info: list = []
        for _ in range(50):
            result = self.fetch_page(complex_no, trade_types, last_info)
            out += [normalize_fin(item, complex_no) for item in result.get("list") or []]
            if not result.get("hasNextPage"):
                break
            last_info = result.get("lastInfo") or []
            time.sleep(1.5)
        return out

    def close(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None


def won_text(v) -> str:
    """원 단위 금액을 '8억 5,000만' 처럼 표기한다."""
    try:
        won = int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return str(v or "")
    eok, man = divmod(won // 10_000, 10_000)
    if eok and man:
        return f"{eok}억 {man:,}만"
    return f"{eok}억" if eok else f"{man:,}만"


def won_int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", ""))) or None
    except (TypeError, ValueError):
        return None


def fin_price(p: dict) -> str:
    main = p.get("dealPrice") or p.get("warrantyPrice") or p.get("depositPrice")
    txt = won_text(main) if main else ""
    if p.get("rentPrice"):
        txt = f"{txt}/{won_text(p['rentPrice'])}"
    return txt or "가격 정보 없음"


def normalize_fin(item: dict, complex_no: str) -> dict:
    """fin.land 매물 목록 항목. 같은 집을 여러 중개사가 올리면 대표 매물 하나로 묶여 온다."""
    a = item.get("representativeArticleInfo") or item
    dup = item.get("duplicatedArticleInfo") or {}
    detail = a.get("articleDetail") or {}
    space = a.get("spaceInfo") or {}
    no = str(a["articleNumber"])
    aliases = sorted({str(x["articleNumber"]) for x in dup.get("articleInfoList") or []
                      if x.get("articleNumber")} - {no})
    realtor = (a.get("brokerInfo") or {}).get("brokerageName", "")
    count = dup.get("realtorCount") or 1
    if realtor and count > 1:
        realtor = f"{realtor} 외 {count - 1}곳"
    trade = a.get("tradeType", "")
    dong = str(a.get("dongName") or "")
    price = a.get("priceInfo") or {}
    # 같은 집을 올린 중개사별 매물번호 → 호가 (가격 변동은 같은 매물번호끼리 비교)
    members, brokers = {}, {}
    for m in [a, *(dup.get("articleInfoList") or [])]:
        if not m.get("articleNumber"):
            continue
        pi = m.get("priceInfo") or {}
        won = won_int(pi.get("dealPrice") or pi.get("warrantyPrice") or pi.get("depositPrice"))
        if won:
            members[str(m["articleNumber"])] = won
        name = (m.get("brokerInfo") or {}).get("brokerageName")
        if name:
            brokers[str(m["articleNumber"])] = name
    # 표시 가격은 그 집의 최저 호가 (월세는 월세가 달라 대표 매물 그대로)
    price_txt = fin_price(price)
    price_won = won_int(price.get("dealPrice") or price.get("warrantyPrice") or price.get("depositPrice"))
    link_no = no
    if members and not won_int(price.get("rentPrice")):
        price_won, link_no = min((w, n) for n, w in members.items())
        price_txt = won_text(price_won)
        if brokers.get(link_no):
            realtor = brokers[link_no] + (f" 외 {count - 1}곳" if count > 1 else "")
    return {
        "articleNo": no,
        "aliases": aliases,
        "link_no": link_no,
        "complexNo": complex_no,
        "name": a.get("complexName", ""),
        "trade": TRADE_NAMES.get(trade, trade),
        "price": price_txt,
        "price_won": price_won,
        "rent_won": won_int(price.get("rentPrice")),
        "member_prices": members,
        "member_brokers": brokers,
        "realtor_count": max(count, len(members), 1),
        "building": dong + "동" if dong.isdigit() else dong,
        "floor": detail.get("floorInfo", ""),
        "area": f"{space.get('supplySpace', '')}/{space.get('exclusiveSpace', '')}㎡",
        "supply": to_float(space.get("supplySpace")),
        "exclusive": to_float(space.get("exclusiveSpace")),
        # 같은 면적 안의 평면 타입 (네이버 spaceInfo.nameType, 예: A/B)
        "unit_type": str(space.get("nameType") or "").strip(),
        "direction": detail.get("direction", ""),
        "desc": detail.get("articleFeatureDescription") or "",
        "realtor": realtor,
        "confirmed": (a.get("verificationInfo") or {}).get("articleConfirmDate", ""),
    }


def to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def pyeong_of(a: dict) -> int | None:
    """평형(공급면적 기준). 공급면적이 없으면 전용면적으로 추정한다."""
    if a.get("supply"):
        return round(a["supply"] / M2_PER_PYEONG)
    if a.get("exclusive"):
        # 아파트 전용률 약 75% 가정 (전용 59㎡ ≈ 25평형, 84㎡ ≈ 34평형)
        return round(a["exclusive"] / 0.75 / M2_PER_PYEONG)
    return None


def matches_pyeong(a: dict, pyeongs: list[int]) -> bool:
    """pyeongs 가 비어 있으면 모든 평형을 허용한다.

    공급면적이 있으면 네이버 표기와 같은 평형으로 정확히 비교하고,
    전용면적으로 추정한 경우에만 ±1평 오차를 허용한다.
    """
    if not pyeongs:
        return True
    p = pyeong_of(a)
    if p is None:
        return True  # 면적 정보가 없으면 놓치지 않도록 알림에 포함
    return any(abs(p - want) <= 1 if a.get("supply") is None else p == want
               for want in pyeongs)


def floor_text(floor: str) -> str:
    """'중/29' → '중층 (총 29층)', '10/29' → '10층 (총 29층)'."""
    cur, _, total = str(floor or "").partition("/")
    if not cur:
        return ""
    cur = cur if cur.endswith("층") else f"{cur}층"
    return f"{cur} (총 {total}층)" if total else cur


def size_label(pyeongs: list[int] | None) -> str:
    ps = sorted(pyeongs or [])
    if len(ps) > 1 and ps == list(range(ps[0], ps[-1] + 1)):
        return f"{ps[0]}~{ps[-1]}평"
    return f"{'/'.join(map(str, ps))}평" if ps else ""


def listing_line(a: dict) -> str:
    p = pyeong_of(a)
    size_txt = f"{p}평 ({a['area']})" if p else a["area"]
    where = " ".join(x for x in (a.get("building", ""), floor_text(a.get("floor", ""))) if x)
    return " · ".join(x for x in (f"[{a['trade']}] {a['price']}", where, size_txt) if x)


def article_url(a: dict) -> str:
    return f"https://fin.land.naver.com/articles/{a.get('link_no') or a['articleNo']}"


def complex_url(complex_no: str) -> str:
    return f"https://fin.land.naver.com/complexes/{complex_no}?tab=article"


def matches_keyword(name: str, keyword: str) -> bool:
    """키워드의 모든 단어가 단지명에 들어 있으면 일치 (띄어쓰기·순서 무시)."""
    squashed = re.sub(r"\s+", "", name).lower()
    return all(tok.lower() in squashed for tok in keyword.split())


# ---------------------------------------------------------------- State

def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"keyword_complexes": {}, "initialized_complexes": [], "seen": {}}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def diff_and_update(state: dict, articles: list[dict], now: datetime,
                    prune_days: int = 14) -> list[dict]:
    """새 매물을 반환하고 state['seen'] 을 갱신한다.

    한동안 목록에서 안 보인 매물은 prune_days 가 지나면 잊는다
    (일시적인 조회 누락 때문에 같은 매물이 다시 알림되지 않도록 여유를 둔다).
    같은 집을 여러 중개사가 올린 매물(aliases)은 대표 매물이 바뀌어도 새 매물로 보지 않는다.
    """
    seen: dict = state.setdefault("seen", {})
    today = now.strftime("%Y-%m-%d")
    new = []
    for a in articles:
        ids = [a["articleNo"], *a.get("aliases", [])]
        if not any(i in seen for i in ids):
            new.append(a)
        for i in ids:
            seen[i] = today
    cutoff = (now - timedelta(days=prune_days)).strftime("%Y-%m-%d")
    for no in [k for k, d in seen.items() if d < cutoff]:
        del seen[no]
    return new


TRACK_KEYS = ("complexNo", "name", "trade", "price", "price_won", "rent_won", "realtor_count", "building",
              "floor", "area", "supply", "exclusive", "realtor", "link_no", "direction", "unit_type")


def _pair_relisted(old_members: dict, new_members: dict, old_brokers: dict, new_brokers: dict,
                   old_ids: set, now_ids: set) -> list[tuple[str, str, int, int]]:
    """중개사가 매물을 지우고 새 번호로 다시 올린 경우를 짝짓는다: [(빠진 번호, 새 번호, 이전가, 새 가격)].

    같은 중개사 이름끼리만 짝짓는다 (다른 중개사가 들어온 것을 가격 변동으로 오해하지 않도록).
    """
    removed = [no for no in old_members if no not in new_members and no not in now_ids]
    added = [no for no in new_members if no not in old_members and no not in old_ids]
    pairs, used = [], set()
    for r in removed:
        name = old_brokers.get(r)
        if not name:
            continue
        for a in added:
            if a not in used and new_brokers.get(a) == name:
                used.add(a)
                pairs.append((r, a, old_members[r], new_members[a]))
                break
    return pairs


def _price_moves(key: str, before: dict, cur: dict, old_members: dict, new_members: dict) -> tuple:
    """(호가를 바꾼 중개사 목록, 최저가 변동 정보)를 계산한다.

    moves: [(매물번호, 이전가, 새 가격, 재등록 여부)]
    min_event: 호가 변경이 아니라 중개사 구성이 바뀌어(싼 중개사 빠짐/더 싼 중개사 추가) 최저가가
    바뀐 경우 (note, 매물번호, 이전 최저가, 새 최저가), 없으면 None
    stat_owner: 최저가 변화를 만든 호가 변경 매물번호 (통계에 셈), 없으면 None
    """
    old_brokers = before.get("member_brokers") or {}
    new_brokers = cur.get("member_brokers") or {}
    moves = [(no, old_members[no], won, False) for no, won in new_members.items()
             if old_members.get(no) and won and old_members[no] != won]
    pairs = []
    if old_members and new_members:
        pairs = _pair_relisted(old_members, new_members, old_brokers, new_brokers,
                               {key, before.get("rep", key), *before.get("aliases", [])},
                               {cur["articleNo"], *cur.get("aliases", [])})
        moves += [(a, old_won, new_won, True) for _, a, old_won, new_won in pairs if old_won != new_won]
    paired_removed = {r for r, _, _, _ in pairs}
    # 묶음에 있는데 가격을 모르는 중개사가 있으면(예전 기록·응답에 가격 없음) 구성 변화로 판단하지 않는다
    now_ids = {cur["articleNo"], *cur.get("aliases", [])}
    old_ids = {key, before.get("rep", key), *before.get("aliases", [])}
    incomplete = (any(n in now_ids for n in old_members if n not in new_members)
                  or any(n in old_ids for n in new_members if n not in old_members))
    old_min = min(old_members.values()) if old_members else None
    new_min = min(new_members.values()) if new_members else None
    stat_owner, min_event = None, None
    if old_min and new_min and old_min != new_min:
        if new_min < old_min:
            # 최저가 하락: 새 최저가로 호가를 내린 중개사가 있으면 그 변동, 없으면 더 싼 중개사가 새로 들어온 것
            cause = [m for m in moves if m[2] == new_min and m[1] > new_min]
            if cause:
                stat_owner = cause[0][0]
            elif not incomplete:
                no = min((w, n) for n, w in new_members.items())[1]
                min_event = ("더 싼 중개사 추가", no, old_min, new_min)
        else:
            # 최저가 상승: 최저가였던 중개사가 빠졌으면 구성 변화, 호가를 올렸으면 그 변동
            old_low = [n for n, w in old_members.items() if w == old_min]
            left = [n for n in old_low if n not in new_members and n not in paired_removed]
            if left:
                if not incomplete:
                    # 빠진 중개사 이름으로 적는다 (새 최저가 중개사가 가격을 올린 것처럼 읽히지 않게)
                    min_event = ("최저가 중개사 빠짐", left[0], old_min, new_min)
            else:
                raised = {n for n in old_low if n in new_members}
                raised |= {a for r, a, _, _ in pairs if r in old_low}
                cause = [m for m in moves if m[0] in raised and m[1] == old_min]
                if cause:
                    stat_owner = cause[0][0]
    return moves, min_event, stat_owner, old_min, new_min


def _merge_tracked(into: dict, other: dict, other_key: str | None = None) -> None:
    """같은 집으로 합쳐진 두 추적 기록을 하나로 (처음 본 날은 빠른 쪽, 중개사 호가는 합침).

    update 전에 부르므로, 원래 있던 중개사가 '더 싼 중개사 추가' 로 잘못 보이지 않는다.
    """
    if not other.get("member_prices") and other.get("price_won"):  # 중개사별 호가가 없던 기록
        other = dict(other, member_prices={other.get("rep") or other_key: other["price_won"]})
    if (other.get("first_seen") or "9") < (into.get("first_seen") or "9"):
        for k in ("first_seen", "first_price", "first_price_won"):
            into[k] = other.get(k)
    elif other.get("first_seen") == into.get("first_seen") and other.get("first_price_won"):
        if (into.get("first_price_won") or other["first_price_won"] + 1) > other["first_price_won"]:
            into["first_price"], into["first_price_won"] = other.get("first_price"), other["first_price_won"]
    for k in ("member_prices", "member_brokers", "member_first"):
        into[k] = {**(other.get(k) or {}), **(into.get(k) or {})}
    into["aliases"] = sorted((set(into.get("aliases", [])) | set(other.get("aliases", []))
                              | {other.get("rep"), other_key}) - {None})
    # 가격 이력은 변동 항목만 합친다 (처음 본 날 항목끼리 섞이면 가짜 변동처럼 보임)
    hist = into.get("price_history", [])[:1] + sorted(
        [h for h in into.get("price_history", []) + other.get("price_history", []) if len(h) > 3],
        key=lambda h: str(h[0]))
    into["price_history"] = hist[-10:]


def track_listings(state: dict, complex_no: str, articles: list[dict], today: str,
                   wanted=lambda a: True, changes: list | None = None,
                   misses_to_gone: int = 2, stamp: str | None = None,
                   relisted: set | None = None, keep_gone_days: int = 14) -> list[dict]:
    """조건(wanted)에 맞는 매물을 추적하고, 목록에서 사라진 매물을 돌려준다.

    - 한 집(같은 집을 여러 중개사가 올린 묶음)을 하나로 추적하고, 중개사 매물번호별 호가를 저장한다.
    - 가격 변동(changes): 같은 매물번호의 호가 변화, 같은 중개사가 새 번호로 다시 올린 경우의 가격 변화,
      그리고 중개사 구성이 바뀌어 최저가가 바뀐 경우(통계 제외). 상승·하락 통계(counts)는 그 집의
      최저가를 실제로 바꾼 호가 변경에만 붙인다.
    - 있는지 여부는 조건과 상관없이 단지 전체 매물로 판단하고, 조건 밖 매물은 알리지 않는다.
    - 조회에 성공한 단지에 대해서만 부른다. misses_to_gone 번 연속 안 보여야 사라진 것으로 본다.
    - 사라진 집이 keep_gone_days 안에 다시 올라오거나, 중개사 한 곳이 같은 집(동·층·면적·중개사)을
      새 번호로 다시 올리면 이어서 추적한다. 새 번호는 relisted 에 담아 '신규' 에서 빼게 한다.
    """
    tracked: dict = state.setdefault("tracked", {})
    recent_gone: dict = state.setdefault("recent_gone", {})
    when = stamp or today
    try:
        cutoff = (datetime.strptime(today[:10], "%Y-%m-%d") - timedelta(days=keep_gone_days)).strftime("%Y-%m-%d")
    except ValueError:
        cutoff = ""
    for k in [k for k, g in recent_gone.items() if (g.get("gone_date") or "") < cutoff]:
        del recent_gone[k]

    by_id = {}
    for a in articles:
        for i in (a["articleNo"], *a.get("aliases", [])):
            by_id[i] = a
    owner_of: dict = {}   # id(cur) → 이번에 그 집에 연결된 추적 key
    gone = []

    def emit(t: dict, **kw) -> None:
        if changes is not None and wanted(t):
            changes.append(dict(t, old_rent_won=None, rent_won=None, **kw))

    def update(key: str, t: dict, cur: dict) -> None:
        before = dict(t)
        old_members = dict(before.get("member_prices") or {})
        if not old_members and before.get("price_won"):  # 예전 기록: 대표 매물 가격만 있음
            old_members = {before.get("rep", key): before["price_won"]}
        new_members = dict(cur.get("member_prices") or {})
        if not new_members and cur.get("price_won"):
            new_members = {cur["articleNo"]: cur["price_won"]}
        moves, min_event, stat_owner, old_min, new_min = _price_moves(key, before, cur, old_members, new_members)
        brokers = {**(before.get("member_brokers") or {}), **(cur.get("member_brokers") or {})}
        # 예전 기록의 '처음 가격' 은 대표 호가였을 수 있어, 처음 봤을 때 중개사들 중 최저가로 한 번 바로잡는다
        if not before.get("first_is_min"):
            firsts = [w for w in (before.get("member_first") or {}).values() if w]
            if firsts and before.get("first_price_won"):
                t["first_price_won"] = min(before["first_price_won"], min(firsts))
                t["first_price"] = won_text(t["first_price_won"])
            t["first_is_min"] = True
        t.update({k: cur.get(k) for k in TRACK_KEYS}, complexNo=complex_no)
        t["member_prices"] = new_members or old_members  # 지금 올라와 있는 중개사 매물만
        t["member_brokers"] = {no: b for no, b in brokers.items() if no in t["member_prices"]}
        t["rep"] = cur["articleNo"]
        t.pop("member_updated", None)  # 예전 버전에서 쓰던 값
        # 묶음이 갈라져도 따라가지 않도록, 이번에 본 묶음으로 교체
        t["aliases"] = sorted({cur["articleNo"], *cur.get("aliases", [])} - {key})
        t["last_seen"], t["missed"] = today, 0
        t.pop("missing_since", None)
        others = {no: w for no, w in old_members.items()}
        for no, old_won, new_won, relist in moves:
            counts = no == stat_owner
            if counts:
                note = "재등록" if relist else ""
            elif new_won in {w for k, w in others.items() if k != no}:
                note = "뒤따라 " + ("올림" if new_won > old_won else "내림")
            elif old_min == new_min:
                note = "최저가 그대로"
            else:
                note = "통계 제외"
            old_txt, new_txt = won_text(old_won), won_text(new_won)
            t["price_history"] = [*t.get("price_history", []),
                                  [when, new_txt, new_won, old_txt, old_won, brokers.get(no, ""), note]][-10:]
            emit(t, articleNo=no, link_no=no, price=new_txt, price_won=new_won, old_price=old_txt,
                 old_price_won=old_won, broker=brokers.get(no, ""), note=note, counts=counts,
                 follow=note.startswith("뒤따라"),
                 stat_old_won=old_min if counts else None, stat_new_won=new_min if counts else None)
        if min_event:
            note, no, old_won, new_won = min_event
            old_txt, new_txt = won_text(old_won), won_text(new_won)
            low_no = min((w, n) for n, w in t["member_prices"].items())[1] if t["member_prices"] else no
            t["price_history"] = [*t.get("price_history", []),
                                  [when, new_txt, new_won, old_txt, old_won, brokers.get(no, ""), note]][-10:]
            emit(t, articleNo=no, link_no=low_no, price=new_txt, price_won=new_won, old_price=old_txt,
                 old_price_won=old_won, broker=brokers.get(no, ""), note=note, counts=False, follow=False)

    # 먼저 각 추적 기록이 오늘 어느 집과 이어지는지 찾고, 한 집에 여러 기록이 이어지면 병합한다
    matched: dict = {}
    for key, t in list(tracked.items()):
        if t["complexNo"] != complex_no:
            continue
        cur = next((by_id[i] for i in (key, *t.get("aliases", [])) if i in by_id), None)
        if cur is None:
            continue
        if id(cur) in owner_of:  # 따로 추적하던 두 기록이 한 집으로 합쳐짐
            _merge_tracked(tracked[owner_of[id(cur)]], t, key)
            del tracked[key]
            continue
        owner_of[id(cur)] = key
        matched[key] = cur
    pending_gone = []
    for key, t in list(tracked.items()):
        if t["complexNo"] != complex_no:
            continue
        if key in matched:
            update(key, t, matched[key])
            continue
        t["missed"] = t.get("missed", 0) + 1
        t.setdefault("missing_since", today)
        if t["missed"] >= misses_to_gone:
            pending_gone.append(key)  # 같은 실행에서 재등록으로 이어지면 사라짐이 아니므로 뒤에서 확정

    def same_house(t: dict, a: dict) -> bool:
        """중개사 한 곳이 같은 집을 새 번호로 다시 올린 것으로 볼 수 있는지.

        다른 집을 잘못 잇지 않도록 엄격하게: 같은 거래유형, 층이 숫자로 공개(저/중/고 구간이면 안 함),
        양쪽 모두 중개사 한 곳짜리이고 같은 중개사, 같은 동·면적, 가격 차이 10% 이내.
        """
        if t.get("trade") != a.get("trade") or not str(t.get("floor") or "").split("/")[0].isdigit():
            return False
        # 양쪽 모두 중개사 한 곳짜리 (가격을 모르는 중개사까지 세는 realtor_count 로도 확인)
        if (len(t.get("member_prices") or {}) > 1 or (t.get("realtor_count") or 1) > 1
                or (a.get("realtor_count") or 1) > 1):
            return False
        tb = set((t.get("member_brokers") or {}).values())
        ab = set((a.get("member_brokers") or {}).values())
        old, new = t.get("price_won"), a.get("price_won")
        return (bool(tb) and tb == ab and t.get("building") == a.get("building")
                and t.get("floor") == a.get("floor") and t.get("supply") == a.get("supply")
                and t.get("exclusive") == a.get("exclusive")
                # 같은 층 같은 평형의 다른 호수와 헷갈리지 않게 향도 같아야 함
                and (t.get("direction") or "") == (a.get("direction") or "")
                and bool(old and new) and abs(new - old) <= old * 0.1)

    def resume(key: str, a: dict, from_gone: bool, same_id: bool) -> None:
        """사라졌거나 안 보이던 기록을 새로 올라온 매물 a 로 이어간다."""
        t = recent_gone.pop(key) if from_gone else tracked.pop(key)
        t.pop("gone_date", None)
        tracked[key] = t
        owner_of[id(a)] = key
        if relisted is not None:
            relisted.add(a["articleNo"])
        old_min = min((t.get("member_prices") or {}).values(), default=t.get("price_won"))
        new_members = a.get("member_prices") or ({a["articleNo"]: a["price_won"]} if a.get("price_won") else {})
        new_min = min(new_members.values(), default=a.get("price_won"))
        # 매물번호별 비교는 하지 않고(새 번호일 수 있음) 아래에서 최저가로 한 번만 비교
        t["member_prices"], t["price_won"] = {}, None
        update(key, t, a)
        if old_min and new_min and old_min != new_min:
            # 같은 번호로 다시 나타나면 '다시 올라옴'(통계 제외), 같은 중개사가 새 번호로 올리면 '재등록'(통계에 셈)
            note = "다시 올라옴" if same_id else "재등록"
            brokers = t.get("member_brokers") or {}
            no = min((w, n) for n, w in new_members.items())[1]
            old_txt, new_txt = won_text(old_min), won_text(new_min)
            t["price_history"] = [*t.get("price_history", []),
                                  [when, new_txt, new_min, old_txt, old_min, brokers.get(no, ""), note]][-10:]
            emit(t, articleNo=no, link_no=no, price=new_txt, price_won=new_min, old_price=old_txt,
                 old_price_won=old_min, broker=brokers.get(no, ""), note=note,
                 counts=not same_id, follow=False,
                 stat_old_won=None if same_id else old_min, stat_new_won=None if same_id else new_min)

    # 1) 사라졌던 집이 같은 번호로 다시 나타남
    fresh_articles = []
    for a in articles:
        ids = {a["articleNo"], *a.get("aliases", [])}
        if id(a) in owner_of or any(i in tracked for i in ids):
            continue
        back = next((k for k, g in recent_gone.items() if g["complexNo"] == complex_no
                     and ids & {k, g.get("rep"), *g.get("aliases", [])}), None)
        if back is not None:
            resume(back, a, from_gone=True, same_id=True)
        else:
            fresh_articles.append(a)
    # 2) 같은 중개사가 같은 집을 새 번호로 다시 올림: 새 매물과 옛 기록이 서로 딱 하나씩 맞을 때만 잇는다
    olds = [(k, t, False) for k, t in tracked.items() if t["complexNo"] == complex_no and t.get("missed")]
    olds += [(k, g, True) for k, g in recent_gone.items() if g["complexNo"] == complex_no]
    cands = {id(a): [(k, g) for k, t, g in olds if same_house(t, a)] for a in fresh_articles}
    per_old: dict = {}
    for a in fresh_articles:
        for k, _ in cands[id(a)]:
            per_old[k] = per_old.get(k, 0) + 1
    for a in fresh_articles:
        c = cands[id(a)]
        if len(c) == 1 and per_old[c[0][0]] == 1:
            resume(c[0][0], a, from_gone=c[0][1], same_id=False)
            continue
        if not wanted(a):
            continue
        members = dict(a.get("member_prices") or {}) or (
            {a["articleNo"]: a["price_won"]} if a.get("price_won") else {})
        low = min(members.values()) if members else a.get("price_won")
        tracked[a["articleNo"]] = dict({k: a.get(k) for k in TRACK_KEYS}, complexNo=complex_no,
                                       aliases=a.get("aliases", []), rep=a["articleNo"],
                                       member_prices=members, member_first=dict(members),
                                       member_brokers=dict(a.get("member_brokers") or {}),
                                       first_seen=today, first_is_min=True,
                                       # 처음 대비는 처음 봤을 때 그 집의 최저 호가와 비교
                                       first_price=won_text(low) if low else a.get("price"),
                                       first_price_won=low,
                                       price_history=[[today, a.get("price"), a.get("price_won")]],
                                       last_seen=today, missed=0)
        owner_of[id(a)] = a["articleNo"]
    for key in pending_gone:
        t = tracked.get(key)
        if t is None or not t.get("missed"):  # 같은 실행에서 재등록으로 이어짐
            continue
        del tracked[key]
        recent_gone[key] = dict(t, gone_date=t["missing_since"])
        if wanted(t):
            gone.append(dict(t, articleNo=key, gone_date=t["missing_since"]))
    return gone


HISTORY_FIELDS = ["사라진날", "단지", "거래", "가격", "처음가격", "동", "층", "평", "면적",
                  "처음본날", "마지막본날", "중개사", "매물번호"]


def append_history(gone: list[dict], complexes: dict, path: Path | None = None) -> None:
    path = path or HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(HISTORY_FIELDS)
        for g in gone:
            w.writerow([g["gone_date"], complexes.get(g["complexNo"], g.get("name", "")),
                        g.get("trade", ""), g.get("price", ""), g.get("first_price", ""),
                        g.get("building", ""), g.get("floor", ""), pyeong_of(g) or "",
                        g.get("area", ""), g.get("first_seen", ""), g.get("last_seen", ""),
                        g.get("realtor", ""), g["articleNo"]])


def days_between(a: str, b: str) -> int | None:
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def gone_line(g: dict) -> str:
    line = listing_line(g)
    if g.get("first_price") and g["first_price"] != g.get("price"):
        line += f" · 처음 {g['first_price']}"
    days = days_between(g.get("first_seen"), g.get("gone_date"))
    if days is not None:
        line += f" · {g['first_seen'][5:].replace('-', '/')}부터 {days}일 게시"
    return line


def format_gone(gone: list[dict], complexes: dict, pyeongs: list[int] | None = None) -> tuple[str, str]:
    size = size_label(pyeongs)
    names = list(dict.fromkeys(complexes.get(g["complexNo"], g.get("name", "")) for g in gone))
    where = names[0] + (f" 외 {len(names) - 1}곳" if len(names) > 1 else "")
    title = f"📉{' ' + size if size else ''} 사라진 매물 {len(gone)}건 · {where} (거래 완료 또는 내림)"
    sections = []
    for cname in names:
        lines = [f"### {cname}"]
        lines += [f"- {gone_line(g)}" for g in gone
                  if complexes.get(g["complexNo"], g.get("name", "")) == cname]
        sections.append("\n".join(lines))
    return title, "\n\n".join(sections)


def mail_preview(to_self: bool = True) -> int:
    """네이버에 다시 조회하지 않고, 저장된 마지막 조회 결과로 브리핑 메일을 지금 보낸다 (기록은 바꾸지 않음).

    테스트용이라 기본은 보내는 주소(SMTP_USER)로만, --all 이면 MAIL_TO 전체로.
    """
    global SHOW_TIME, SORT_ORDER
    if not mail_configured():
        print("local.json 에 SMTP_USER, SMTP_PASSWORD 를 먼저 설정하세요.")
        return 1
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    SHOW_TIME = bool(config.get("show_time", True))
    SORT_ORDER = list(config.get("sort") or ["price", "dong", "-floor"])
    pyeongs = [int(p) for p in config.get("pyeong", [])]
    state = load_state()
    now = datetime.now(KST)
    tracked = state.get("tracked") or {}
    briefing = []
    for no, name in (config.get("complexes") or {}).items():
        listings = [tracked_info(state, dict(t, articleNo=t.get("rep", key)))
                    for key, t in tracked.items() if t["complexNo"] == str(no) and not t.get("missed")
                    and matches_pyeong(t, pyeongs)]
        briefing.append({"no": str(no), "name": name, "status": "ok", "listings": listings,
                         "new": [], "changes": [], "gone": [], "week": recent_events(state, str(no), now)})
    subject, text, html_body = format_briefing(briefing, size_label(pyeongs), now,
                                               parse_time(state.get("last_mail")))
    subject = subject.replace("[매물 브리핑]", "[매물 브리핑 미리보기]", 1)
    to = send_mail(subject, text, html_body,
                   [(f"매매매물_{now:%Y%m%d}.csv", listings_csv(briefing), "text/csv")], to_self=to_self)
    print(f"미리보기 메일을 보냈습니다: {to} (마지막 조회: {state.get('last_run', '-')})")
    return 0


def check_brokers(complex_no: str | None = None) -> int:
    """네이버에 한 페이지만 조회해, 대표가 아닌 중개사들의 호가가 응답에 들어있는지 확인한다.

    저장된 기록은 전혀 바꾸지 않는다.
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    complexes = {str(k): v for k, v in (config.get("complexes") or {}).items()}
    no = complex_no or next(iter(complexes))
    naver = NaverLand()
    try:
        result = naver.fetch_page(no, config.get("trade_types", ["A1"]), [])
    finally:
        naver.close()
    items = result.get("list") or []
    types = sorted({(normalize_fin(i, no)["unit_type"] or "없음", normalize_fin(i, no)["area"]) for i in items})
    print("타입(nameType) 값: " + ", ".join(f"{t} ({ar})" for t, ar in types[:10]))
    groups = [i for i in items if ((i.get("duplicatedArticleInfo") or {}).get("realtorCount") or 1) > 1]
    print(f"{complexes.get(no, no)} ({no}) 첫 페이지 매물 {len(items)}건 중 여러 중개사 매물 {len(groups)}건")
    ok = 0
    for i in groups[:3]:
        dup = i["duplicatedArticleInfo"]
        members = dup.get("articleInfoList") or []
        a = normalize_fin(i, no)
        print(f"- 대표 {a['articleNo']} {a['price']} · 중개사 {dup.get('realtorCount')}곳 · 목록 {len(members)}건")
        print(f"  타입(nameType): {a['unit_type'] or '없음'} · 면적 {a['area']}")
        if members:
            m = members[0]
            print(f"  다른 중개사 매물 항목: {sorted(m.keys())}")
            print(f"  priceInfo: {json.dumps(m.get('priceInfo'), ensure_ascii=False)[:200]}")
        print("  읽은 중개사별 호가: " + (" / ".join(
            f"{(a['member_brokers'] or {}).get(k, k)} {won_text(w)}"
            for k, w in sorted(a["member_prices"].items(), key=lambda x: x[1])) or "없음"))
        ok += len(a["member_prices"]) > 1
    if not groups:
        print("결과: 첫 페이지에 여러 중개사 매물이 없어 판단할 수 없습니다. 다른 단지번호로 해보세요.")
    elif ok:
        print(f"결과: ✅ 대표 외 중개사 호가도 받고 있습니다 ({ok}/{min(len(groups), 3)}건 확인)")
    else:
        print("결과: ❌ 대표 중개사 호가만 들어옵니다. 위 '다른 중개사 매물 항목' 줄을 알려주세요.")
    return 0


def print_status() -> int:
    """기록이 잘 쌓이는지 확인용: 단지별 추적 매물 수·가격 이력·최근 7일 변동."""
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state()
    print(f"마지막 조회: {state.get('last_run', '-')} · 마지막 메일: {state.get('last_mail', '-')}")
    tracked = state.get("tracked") or {}
    now = datetime.now(KST)
    # 대표가 아닌 중개사들의 호가가 네이버 응답에 들어오는지 확인
    multi = [t for t in tracked.values() if (t.get("realtor_count") or 0) > 1]
    priced = [t for t in multi if len(t.get("member_prices") or {}) > 1]
    print(f"여러 중개사 매물 {len(multi)}건 중 중개사별 호가가 들어온 매물 {len(priced)}건"
          + (" → 대표 외 중개사 호가도 받고 있음" if priced else
             " → 대표 중개사 호가만 받고 있음" if multi else ""))
    for t in priced[:3]:
        brokers = t.get("member_brokers") or {}
        print("  예: " + " / ".join(f"{brokers.get(no, no)} {won_text(w)}"
                                    for no, w in sorted(t["member_prices"].items(), key=lambda x: x[1])))
    for no, name in (config.get("complexes") or {}).items():
        ts = [t for t in tracked.values() if t["complexNo"] == str(no)]
        moved = sum(1 for t in ts if len(t.get("price_history") or []) > 1)
        week = recent_events(state, str(no), now)
        kinds = Counter(e["kind"] for e in week)
        stats = change_stats([e["item"] for e in week if e["kind"] == "change"])
        print(f"- {name}: 추적 {len(ts)}건 · 가격 바뀐 적 있는 매물 {moved}건 · 7일 변동 "
              f"신규 {kinds['new']} / 가격 {kinds['change']} / 사라짐 {kinds['gone']}"
              + (f" ({stats})" if stats else ""))
    return 0


def print_history(limit: int = 30, path: Path | None = None) -> int:
    path = path or HISTORY_PATH
    if not path.exists():
        print("아직 기록된 사라진 매물이 없습니다.")
        return 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"사라진 매물 {len(rows)}건 중 최근 {min(limit, len(rows))}건 (거래 완료 또는 중개사가 내림)")
    for r in rows[-limit:][::-1]:
        price = r["가격"] + (f" (처음 {r['처음가격']})" if r["처음가격"] and r["처음가격"] != r["가격"] else "")
        days = days_between(r["처음본날"], r["사라진날"])
        posted = f" · {days}일 게시" if days is not None else ""
        where = " ".join(x for x in (r["동"], floor_text(r["층"])) if x)
        size = f"{r['평']}평" if r["평"] else ""
        print(" · ".join(x for x in (f"{r['사라진날'][5:]} {r['단지']}", f"[{r['거래']}] {price}",
                                     where, size) if x) + posted)
    return 0


# ---------------------------------------------------------------- Notify

def format_message(new: list[dict], complexes: dict,
                   pyeongs: list[int] | None = None) -> tuple[str, str]:
    size = f" {size_label(pyeongs)}" if pyeongs else ""
    names = list(dict.fromkeys(complexes.get(a["complexNo"], a["name"]) for a in new))
    where = names[0] + (f" 외 {len(names) - 1}곳" if len(names) > 1 else "")
    title = f"🏠{size} 새 매물 {len(new)}건 · {where} ({datetime.now(KST):%m/%d %H:%M})"
    sections = []
    for cname in names:
        lines = [f"### {cname}"]
        for a in new:
            if complexes.get(a["complexNo"], a["name"]) != cname:
                continue
            head = listing_line(a)
            extra = " · ".join(x for x in (a["direction"], a["desc"], a["realtor"]) if x)
            lines.append(f"- **{head}**\n  {extra}\n  {article_url(a)}")
        sections.append("\n".join(lines))
    return title, "\n\n".join(sections)


def notify_github(title: str, body: str) -> bool:
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body, "labels": ["새매물"]}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "naver-alert"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20):
        pass
    return True


def notify_telegram(title: str, body: str) -> bool:
    token, chat = setting("TELEGRAM_BOT_TOKEN"), setting("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    text = f"{title}\n\n{body.replace('**', '').replace('### ', '▶ ')}"
    for i in range(0, len(text), 4000):  # 텔레그램 메시지 길이 제한
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 4000],
                                       "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20):
            pass
    return True


# ---------------------------------------------------------------- Mail briefing

def diff_text(old: int | None, new: int | None) -> str:
    if not old or not new or old == new:
        return ""
    return f"{'▲' if new > old else '▼'}{won_text(abs(new - old))}"


def change_diff(c: dict) -> str:
    """가격 변동 폭. 보증금/매매가가 같고 월세만 바뀌었으면 월세 변동을 보여준다."""
    d = diff_text(c.get("old_price_won"), c.get("price_won"))
    if d:
        return d
    d = diff_text(c.get("old_rent_won"), c.get("rent_won"))
    return f"월세 {d}" if d else ""


def dup_text(a: dict) -> str:
    """여러 중개사가 같은 집을 올렸으면 '중개사 3곳'."""
    n = a.get("realtor_count") or len(a.get("member_prices") or {})
    if not n or n < 2:
        return ""
    wons = sorted(set((a.get("member_prices") or {}).values()))
    rng = f" · {won_text(wons[0])}~{won_text(wons[-1])}" if len(wons) > 1 else ""
    return f"중개사 {n}곳{rng}"


def type_code(a: dict) -> str:
    """네이버식 타입 이름: 전용면적(소수점 버림) + 평면 기호, 예: 59.96㎡ · A → '59A'.

    네이버는 A/B/C 를 면적별로 따로 붙여서(59㎡ A, 84㎡ A) 기호만으로는 구분이 안 된다.
    """
    t = a.get("unit_type") or ""
    if len(t) <= 2 and t.isalpha() and a.get("exclusive"):
        return f"{int(a['exclusive'])}{t}"
    return t


def type_text(a: dict) -> str:
    """'59A타입' (타입 정보가 없으면 빈 문자열)."""
    t = type_code(a)
    return t if not t or t.endswith("타입") else f"{t}타입"


def where_text(a: dict) -> str:
    p = pyeong_of(a)
    size = f"{p}평" if p else a.get("area", "")
    size = " ".join(x for x in (size, type_text(a)) if x)
    place = " ".join(x for x in (a.get("building", ""), floor_text(a.get("floor", ""))) if x)
    return " · ".join(x for x in (place, size, dup_text(a)) if x)


def trade_summary(listings: list[dict]) -> str:
    counts = Counter(a["trade"] for a in listings)
    parts = [f"{t} {counts[t]}" for t in ("매매", "전세", "월세") if counts[t]]
    parts += [f"{t} {n}" for t, n in counts.items() if t not in ("매매", "전세", "월세")]
    text = f"매물 {len(listings)}건" + (f" ({' · '.join(parts)})" if len(counts) > 1 else "")
    sale = sorted(a["price_won"] for a in listings if a["trade"] == "매매" and a.get("price_won"))
    if sale:
        text += f" · 매매 {won_text(sale[0])}" + (f"~{won_text(sale[-1])}" if sale[-1] != sale[0] else "")
    return text


# 현재 매매 표 정렬 순서 (config.json 의 sort). 앞이 우선, '-' 는 내림차순.
SORT_ORDER = ["price", "dong", "-floor"]
SORT_NAMES = {"price": "가격", "dong": "동", "floor": "층", "pyeong": "평", "registered": "등록일"}


def dong_num(a: dict) -> float:
    m = re.search(r"\d+", a.get("building") or "")
    return int(m.group()) if m else float("inf")


def floor_num(a: dict) -> float:
    """층을 숫자로. 저/중/고층은 총 층수의 20/50/80% 위치로 본다."""
    cur, _, total = str(a.get("floor") or "").partition("/")
    if cur.isdigit():
        return int(cur)
    try:
        return int(total) * {"저": 0.2, "중": 0.5, "고": 0.8}[cur]
    except (KeyError, ValueError):
        return -1


SORT_KEYS = {
    "price": lambda a: a.get("price_won") or 0,
    "dong": dong_num,
    "floor": floor_num,
    "pyeong": lambda a: pyeong_of(a) or 0,
    "registered": lambda a: a.get("first_seen") or "",
}


def sort_listings(rows: list[dict], order: list[str] | None = None) -> list[dict]:
    """order 순서대로(앞이 우선) 정렬. 우선순위가 낮은 키부터 안정 정렬을 반복한다."""
    rows = list(rows)
    for key in reversed(order or SORT_ORDER):
        name = key.lstrip("-")
        if name in SORT_KEYS:
            rows.sort(key=SORT_KEYS[name], reverse=key.startswith("-"))
    return rows


def sort_label(order: list[str] | None = None) -> str:
    return " → ".join(f"{SORT_NAMES.get(k.lstrip('-'), k)}{'↓' if k.startswith('-') else '↑'}"
                      for k in (order or SORT_ORDER) if k.lstrip("-") in SORT_KEYS)


def sale_rows(r: dict) -> list[dict]:
    """현재 매매 매물을 설정한 정렬 순서로."""
    return sort_listings([a for a in r.get("listings", []) if a["trade"] == "매매"])


LISTING_CSV_FIELDS = ["단지", "가격(만원)", "가격", "동", "층", "층(정렬용)", "평", "타입", "공급㎡", "전용㎡",
                      "처음가격", "처음대비(만원)", "처음본날", "최근변동", "중개사", "중개사 수",
                      "최저호가(만원)", "최고호가(만원)", "중개사별 호가", "특징", "링크"]


def broker_prices(a: dict) -> list[tuple[str, int, str]]:
    """같은 집을 올린 중개사별 (중개사, 호가, 매물번호), 싼 순."""
    brokers = a.get("member_brokers") or {}
    out = [(brokers.get(no, f"매물 {no}"), won, no) for no, won in (a.get("member_prices") or {}).items()]
    return sorted(out, key=lambda x: x[1])


def listings_csv(reports: list[dict]) -> bytes:
    """모든 단지의 현재 매매 매물 표 (엑셀·구글 시트에서 원하는 열로 정렬해 보기용)."""
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(LISTING_CSV_FIELDS)
    for r in reports:
        for a in sale_rows(r):
            first = a.get("first_price_won")
            diff = (a["price_won"] - first) // 10_000 if first and a.get("price_won") else ""
            hist = recent_price_changes(a, 1)
            bp = broker_prices(a)
            w.writerow([r["name"], (a.get("price_won") or 0) // 10_000 or "", a.get("price", ""),
                        a.get("building", ""), a.get("floor", ""), round(floor_num(a), 1),
                        pyeong_of(a) or "", type_code(a), a.get("supply") or "",
                        a.get("exclusive") or "",
                        a.get("first_price", ""), diff, a.get("first_seen", ""),
                        hist[0] if hist else "", a.get("realtor", ""),
                        max(a.get("realtor_count") or 0, len(bp), 1),
                        bp[0][1] // 10_000 if bp else "", bp[-1][1] // 10_000 if bp else "",
                        " / ".join(f"{name} {won_text(won)}" for name, won, _ in bp),
                        a.get("desc", ""), article_url(a)])
    return buf.getvalue().encode("utf-8-sig")  # 엑셀에서 한글이 깨지지 않게 BOM


# 하루 한 번만 조회하면 시각은 의미가 없어 날짜만 쓴다 (config.json 의 show_time)
SHOW_TIME = True


def time_label(dt: datetime, with_date: bool = True) -> str:
    """'10/03 오후 1시' (with_date=False 면 '오후 1시'). SHOW_TIME 이 꺼져 있으면 '10/03'."""
    if not SHOW_TIME:
        return f"{dt:%m/%d}"
    t = f"{'오전' if dt.hour < 12 else '오후'} {dt.hour % 12 or 12}시"
    return f"{dt:%m/%d} {t}" if with_date else t


def parse_time(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


EVENT_KEYS = ("articleNo", "trade", "price", "price_won", "rent_won", "old_price", "old_price_won",
              "realtor_count", "broker", "member_prices", "follow", "note", "counts", "link_no", "unit_type",
              "stat_old_won", "stat_new_won",
              "old_rent_won", "building", "floor", "area", "supply", "exclusive", "first_seen",
              "first_price", "gone_date", "desc", "realtor")
EVENT_NAMES = {"new": "신규", "change": "가격", "gone": "사라짐"}


def log_events(state: dict, reports: dict, now: datetime, since: datetime | None,
               keep_days: int = 14) -> None:
    """이번에 확인한 변동을 시각과 함께 state['events'] 에 쌓는다 (최근 7일 브리핑용)."""
    events: list = state.setdefault("events", [])
    at = now.isoformat(timespec="minutes")
    prev = since.isoformat(timespec="minutes") if since else None
    for no, r in reports.items():
        for kind, key in (("new", "new"), ("change", "changes"), ("gone", "gone")):
            for a in r.get(key, []):
                events.append({"at": at, "since": prev, "kind": kind, "complexNo": no,
                               "item": {k: a.get(k) for k in EVENT_KEYS if a.get(k) is not None}})
    cutoff = (now - timedelta(days=keep_days)).isoformat(timespec="minutes")
    state["events"] = [e for e in events if e["at"] >= cutoff]


def recent_events(state: dict, complex_no: str, now: datetime, days: int = 7) -> list[dict]:
    cutoff = (now - timedelta(days=days)).isoformat(timespec="minutes")
    return sorted((e for e in state.get("events") or []
                   if e["complexNo"] == complex_no and e["at"] >= cutoff),
                  key=lambda e: e["at"], reverse=True)


def event_text(e: dict) -> str:
    a = e["item"]
    if e["kind"] == "change":
        fol = note_text(a)
        return (f"[{a.get('trade')}] {move_text(a)} ({change_diff(a)})"
                + (f" [{fol}]" if fol else "") + f" · {where_text(a)}")
    if e["kind"] == "gone":
        days = days_between(a.get("first_seen"), a.get("gone_date"))
        parts = [f"[{a.get('trade')}] {a.get('price')}", where_text(a),
                 f"{days}일 게시" if days is not None else ""]
        return " · ".join(x for x in parts if x)
    return " · ".join(x for x in (f"[{a.get('trade')}] {a.get('price')}", where_text(a)) if x)


def stamp_label(stamp: str) -> str:
    """'2026-10-03T13:00+09:00' → '10/03 오후 1시', 날짜만 있으면 '10/03'."""
    dt = parse_time(stamp) if "T" in str(stamp) else None
    return time_label(dt) if dt else str(stamp)[5:].replace("-", "/")


def recent_price_changes(a: dict, n: int = 2) -> list[str]:
    """매물의 최근 가격 변동 n개 (최신순): '10/03 오후 1시 6억 2,000만 → 5억 9,000만 ▼3,000만'."""
    hist = a.get("price_history") or []
    out = []
    for prev, cur in list(zip(hist, hist[1:]))[-n:][::-1]:
        old_txt, old_won = (cur[3], cur[4]) if len(cur) > 4 else (prev[1], prev[2] if len(prev) > 2 else None)
        d = diff_text(old_won, cur[2] if len(cur) > 2 else None)
        who = cur[5] if len(cur) > 5 and cur[5] else ""
        note = cur[6] if len(cur) > 6 else ""
        note = "뒤따라" if note is True else (note or "")
        fol = f" ({note})" if note and note not in COMPOSITION_NOTES else ""
        out.append(f"{stamp_label(cur[0])} {move_text({}, old_txt, cur[1], who, note)}"
                   + (f" {d}" if d else "") + fol)
    return out


def seen_at(a: dict) -> str:
    """메일 사이에 여러 번 조회했을 때, 그 변동을 확인한 시각."""
    at = parse_time(a.get("_at"))
    return f" · {time_label(at)} 확인" if at else ""


def follow_text(c: dict) -> str:
    """통계에서 빠진 변동의 이유: '뒤따라 내림/올림' 또는 '최저가 그대로'."""
    if c.get("note"):
        return c["note"]
    if not c.get("follow"):
        return ""
    return "뒤따라 " + ("올림" if (c.get("price_won") or 0) > (c.get("old_price_won") or 0) else "내림")


def counted(c: dict) -> bool:
    """상승·하락 통계에 넣는 변동인지 (그 집의 최저 호가가 바뀐 변동만)."""
    if "counts" in c:
        return bool(c["counts"])
    return not c.get("follow")  # 예전 기록


def stat_pair(c: dict) -> tuple:
    """통계에 쓰는 (이전, 이후) 가격: 그 집의 최저 호가 변화 (예전 기록은 매물 호가 변화)."""
    return (c.get("stat_old_won") or c.get("old_price_won"), c.get("stat_new_won") or c.get("price_won"))


COMPOSITION_NOTES = {"최저가 중개사 빠짐": "빠짐", "더 싼 중개사 추가": "추가"}


def move_text(c: dict, old: str | None = None, new: str | None = None, broker: str | None = None,
              note: str | None = None) -> str:
    """가격 변동 문구. 중개사 구성 변화는 그 중개사가 가격을 바꾼 것처럼 읽히지 않게
    'OO공인 빠짐 · 최저가 5억 1,000만 → 5억 3,000만' 으로 쓴다."""
    old = old if old is not None else c.get("old_price")
    new = new if new is not None else c.get("price")
    broker = broker if broker is not None else c.get("broker", "")
    note = note if note is not None else c.get("note", "")
    if note in COMPOSITION_NOTES:
        return f"{broker or '중개사'} {COMPOSITION_NOTES[note]} · 최저가 {old} → {new}"
    return f"{broker + ' ' if broker else ''}{old} → {new}"


def note_text(c: dict) -> str:
    """변동 뒤에 괄호로 붙일 설명 (구성 변화는 문구에 이미 들어 있어 뺀다)."""
    fol = follow_text(c)
    return "" if fol in COMPOSITION_NOTES else fol


def change_counts(changes: list[dict]) -> str:
    """요약표용: '▲1 ▼2' (변동 없으면 '0'). 최저 호가가 바뀐 변동만 센다."""
    changes = [stat_pair(c) for c in changes if counted(c)]
    up = sum(1 for old, new in changes if (new or 0) > (old or 0))
    down = len(changes) - up
    return " ".join(x for x in (f"▲{up}" if up else "", f"▼{down}" if down else "") if x) or "0"


def change_stats(changes: list[dict]) -> str:
    """'상승 2 · 하락 3 · 평균 ▼1,200만 (-1.8%)' — 상승·하락을 합친 평균 변동.

    다른 중개사가 이미 부르던 가격으로 뒤따라 바꾼 것은 집값 흐름이 아니라 빼고 '(뒤따라 N건 제외)' 로 적는다.
    """
    follows = sum(1 for c in changes if not counted(c))
    pairs = [stat_pair(c) for c in changes if counted(c)]
    diffs = [(new - old, old) for old, new in pairs if old and new and old != new]
    tail = f" (최저가 안 바뀐 {follows}건 제외)" if follows else ""
    if not diffs:
        return f"상승 0 · 하락 0{tail}" if follows else ""
    up = sum(1 for d, _ in diffs if d > 0)
    avg = sum(d for d, _ in diffs) / len(diffs)
    pct = sum(d / base * 100 for d, base in diffs) / len(diffs)
    avg_txt = f"{'▲' if avg > 0 else '▼'}{won_text(round(abs(avg)))} ({pct:+.1f}%)" if round(avg) else "0"
    return f"상승 {up} · 하락 {len(diffs) - up} · 평균 {avg_txt}{tail}"


def format_briefing(reports: list[dict], label: str, now: datetime,
                    since: datetime | None = None) -> tuple[str, str, str]:
    """단지별 브리핑 메일 (제목, 텍스트 본문, HTML 본문).

    since 는 이전 브리핑 시각. 이번 변동은 그 사이에 생긴 것이다.
    각 단지의 r['week'] 에 최근 7일 변동(log_events 기록)이 있으면 함께 보여준다.
    """
    ok = [r for r in reports if r["status"] != "failed"]
    n_new, n_chg, n_gone = (sum(len(r.get(k, [])) for r in ok) for k in ("new", "changes", "gone"))
    all_stats = change_stats([c for r in ok for c in r.get("changes", [])])
    day = time_label(now)  # 하루 여러 번 받아도 구분되게
    if since and not SHOW_TIME:
        window = f"{since:%m/%d} ~ {now:%m/%d}"
    elif since:
        same_day = since.date() == now.date()
        window = f"{time_label(since, with_date=not same_day)} ~ {time_label(now, with_date=False)}"
    else:
        window = ""
    subject = (f"[매물 브리핑] {day} {label} · 신규 {n_new} · 가격변동 {n_chg} · 사라짐 {n_gone}"
               if ok else f"[매물 브리핑] {day} 조회 실패")
    esc = html.escape

    def link(url: str, text: str) -> str:
        return f'<a href="{esc(url)}" style="color:#0b57d0;text-decoration:none">{esc(text)}</a>'

    red, blue = "#c5221f", "#1a56db"

    def diff_html(d: str) -> str:
        if not d:
            return ""
        return f' <b style="color:{red if "▲" in d else blue}">{esc(d)}</b>'

    text: list[str] = [f"{day} 매물 브리핑 ({label})", ""]
    h: list[str] = [
        '<div style="font-family:-apple-system,Roboto,\'Noto Sans KR\',sans-serif;font-size:14px;'
        'line-height:1.5;color:#1f1f1f;max-width:720px;word-break:keep-all">',
        f'<h2 style="font-size:19px;margin:0 0 4px">{esc(day)} 매물 브리핑</h2>',
        f'<div style="color:#5f6368;margin-bottom:12px">{esc(label)} · 이번 변동'
        + (f" ({esc(window)} 사이)" if window else "")
        + f': 신규 {n_new} · 가격변동 {n_chg}'
        + (f" ({esc(all_stats)})" if all_stats else "") + f' · 사라짐 {n_gone}<br>'
        '단지 이름을 누르면 네이버 부동산 매물 목록이 열립니다. 놓친 변동은 단지별 "최근 7일 변동"에 있습니다.</div>',
        '<table width="100%" cellpadding="4" style="border-collapse:collapse;font-size:12px;'
        'margin-bottom:8px;width:100%">',
        '<tr style="background:#f1f3f4;white-space:nowrap"><th align="left">단지</th><th>매물</th>'
        '<th>신규</th><th>변동</th><th>사라짐</th><th>7일</th></tr>',
    ]
    if window:
        text.insert(1, f"이번 변동: {window} 사이")
    if all_stats:
        text.insert(1, f"가격 변동: {all_stats}")
    for r in reports:
        week = str(len(r.get("week", [])))
        cells = (["조회 실패", "", "", "", week] if r["status"] == "failed" else
                 [str(len(r.get("listings", []))), str(len(r.get("new", []))),
                  change_counts(r.get("changes", [])), str(len(r.get("gone", []))), week])
        h.append(f'<tr style="border-top:1px solid #e0e0e0"><td>{link(complex_url(r["no"]), r["name"])}</td>'
                 + "".join(f'<td align="center" style="white-space:nowrap">{esc(c)}</td>'
                           for c in cells) + "</tr>")
    h.append("</table>")

    def section(title: str, items: list[str]) -> None:
        h.append(f'<div style="font-weight:600;margin:10px 0 2px">{esc(title)}</div>'
                 '<ul style="margin:0;padding-left:18px">' + "".join(f"<li>{i}</li>" for i in items)
                 + "</ul>")

    for r in reports:
        url = complex_url(r["no"])
        text += [f"■ {r['name']}", f"  {url}"]
        h.append(f'<h3 style="font-size:16px;margin:24px 0 2px;padding-top:12px;'
                 f'border-top:2px solid #1f1f1f">{link(url, r["name"])}</h3>')
        if r["status"] == "failed":
            text += [f"  조회 실패: {r.get('error', '')}", ""]
            h.append(f'<div style="color:{red}">조회 실패: {esc(r.get("error", ""))}</div>')
            continue
        summary = trade_summary(r.get("listings", []))
        text.append(f"  {label} {summary}")
        h.append(f'<div style="color:#5f6368">{esc(label)} {esc(summary)}</div>')
        if r["status"] == "first":
            msg = "첫 조회라 오늘 매물을 기준으로 저장했습니다. 다음 브리핑부터 변동을 알려드립니다."
            text.append(f"  {msg}")
            h.append(f"<div>{esc(msg)}</div>")
        if r.get("new"):
            text.append(f"  [신규 {len(r['new'])}건]")
            items = []
            for a in r["new"]:
                extra = " · ".join(x for x in (a.get("desc", ""), a.get("realtor", "")) if x)
                text += [f"  - [{a['trade']}] {a['price']} · {where_text(a)}{seen_at(a)}"
                         + (f" · {extra}" if extra else ""), f"    {article_url(a)}"]
                items.append(link(article_url(a), f"[{a['trade']}] {a['price']}")
                             + f" · {esc(where_text(a) + seen_at(a))}"
                             + (f'<br><span style="color:#5f6368">{esc(extra)}</span>' if extra else ""))
            section(f"🆕 이번 신규 {len(r['new'])}건", items)
        if r.get("changes"):
            text.append(f"  [가격 변동 {len(r['changes'])}건]"
                        + (f" {change_stats(r['changes'])}" if change_stats(r["changes"]) else ""))
            items = []
            for c in r["changes"]:
                d = change_diff(c)
                fol = note_text(c)
                text += [f"  - [{c['trade']}] {move_text(c)} ({d})"
                         + (f" [{fol}]" if fol else "") + f" · {where_text(c)}{seen_at(c)}",
                         f"    {article_url(c)}"]
                items.append(link(article_url(c), f"[{c['trade']}] {move_text(c)}")
                             + diff_html(d)
                             + (f' <span style="color:#5f6368">({esc(fol)})</span>' if fol else "")
                             + f" · {esc(where_text(c) + seen_at(c))}")
            stats = change_stats(r["changes"])
            section(f"💰 이번 가격 변동 {len(r['changes'])}건" + (f" · {stats}" if stats else ""), items)
        if r.get("gone"):
            text.append(f"  [사라짐 {len(r['gone'])}건] 거래 완료 또는 중개사가 내림")
            items = []
            for g in r["gone"]:
                days = days_between(g.get("first_seen"), g.get("gone_date"))
                posted = f"{days}일 게시" if days is not None else ""
                first = (f"처음 {g['first_price']}" if g.get("first_price")
                         and g["first_price"] != g.get("price") else "")
                tail = " · ".join(x for x in (where_text(g), first, posted) if x) + seen_at(g)
                text.append(f"  - [{g['trade']}] {g['price']} · {tail}")
                items.append(f"[{esc(g['trade'])}] {esc(g['price'])} · {esc(tail)}")
            section(f"📉 이번 사라짐 {len(r['gone'])}건 (거래 완료 또는 내림)", items)
        if r["status"] == "ok" and not (r.get("new") or r.get("changes") or r.get("gone")):
            text.append("  이번 변동 없음")
            h.append('<div style="color:#5f6368;margin-top:6px">이번 변동 없음</div>')
        week = r.get("week") or []
        if week:
            note = ("날짜는 변동을 확인한 날, 그 전날 조회 이후 생긴 변동" if not SHOW_TIME else
                    "시각은 변동을 확인한 브리핑, 그 전 브리핑 이후 생긴 변동")
            wstats = change_stats([e["item"] for e in week if e["kind"] == "change"])
            text.append(f"  [최근 7일 변동 {len(week)}건, 최신순 · {note}]"
                        + (f" 가격 {wstats}" if wstats else ""))
            h.append(f'<div style="font-weight:600;margin:10px 0 2px">🗓 최근 7일 변동 {len(week)}건 '
                     f'<span style="font-weight:400;color:#5f6368">(최신순 · {note})</span></div>'
                     + (f'<div style="font-size:13px;margin-bottom:2px">가격 {esc(wstats)}</div>'
                        if wstats else "") +
                     '<table width="100%" cellpadding="4" style="border-collapse:collapse;font-size:13px;'
                     'width:100%">')
            colors = {"new": "#188038", "change": "#b06000", "gone": "#5f6368"}
            for e in week:
                at = parse_time(e["at"])
                when = time_label(at) if at else e["at"]
                kind = EVENT_NAMES.get(e["kind"], e["kind"])
                a = e["item"]
                text.append(f"  - {when} [{kind}] {event_text(e)}")
                if e["kind"] == "change":
                    fol = note_text(a)
                    body = (link(article_url(a), f"[{a.get('trade')}] {move_text(a)}")
                            + diff_html(change_diff(a))
                            + (f' <span style="color:#5f6368">({esc(fol)})</span>' if fol else "")
                            + f" · {esc(where_text(a))}")
                elif e["kind"] == "gone":
                    body = esc(event_text(e))
                else:
                    body = (link(article_url(a), f"[{a.get('trade')}] {a.get('price')}")
                            + f" · {esc(where_text(a))}")
                h.append(f'<tr style="border-top:1px solid #eee;vertical-align:top">'
                         f'<td style="white-space:nowrap;color:#5f6368">{esc(when)}</td>'
                         f'<td style="white-space:nowrap;color:{colors.get(e["kind"], "#1f1f1f")};'
                         f'font-weight:600">{esc(kind)}</td><td>{body}</td></tr>')
            h.append("</table>")
        rows = sale_rows(r)
        if rows:
            text.append(f"  [현재 매매 {len(rows)}건, 정렬 {sort_label()} · 변동은 처음 본 가격 대비]")
            h.append(f'<div style="font-weight:600;margin:10px 0 2px">현재 매매 {len(rows)}건 '
                     f'<span style="font-weight:400;color:#5f6368">(정렬 {esc(sort_label())} · '
                     '변동은 처음 본 가격 대비, 등록은 처음 본 날)</span></div>'
                     '<table width="100%" cellpadding="4" style="border-collapse:collapse;font-size:13px;'
                     'width:100%">'
                     '<tr style="background:#f1f3f4;white-space:nowrap"><th align="left">가격</th>'
                     '<th align="left">동·층</th><th>평</th><th>변동</th><th>등록</th></tr>')
            for a in rows:
                d = diff_text(a.get("first_price_won"), a.get("price_won"))
                seen = (a.get("first_seen") or "")[5:].replace("-", "/")
                place = " ".join(x for x in (a.get("building", ""), a.get("floor", "")) if x)
                place += "층" if a.get("floor") else ""
                p = pyeong_of(a)
                text.append(f"  - {a['price']} · {place} · {p or ''}평"
                            + (f" {type_text(a)}" if type_text(a) else "") + (f" · 처음 대비 {d}" if d else "")
                            + (f" · {dup_text(a)}" if dup_text(a) else ""))
                hist = recent_price_changes(a, 6)  # 최근 1개 + 펼치면 이전 5개
                text += [f"      ↳ {x}" for x in hist[:1]]
                if len(hist) > 1:
                    text.append(f"      (이전 변동 {len(hist) - 1}건)")
                    text += [f"        {x}" for x in hist[1:]]
                h.append(f'<tr style="border-top:1px solid #eee">'
                         f'<td><span style="white-space:nowrap">{link(article_url(a), a["price"])}</span>'
                         + (f'<br><span style="font-size:11px;color:#b06000">{esc(dup_text(a))}</span>'
                            if dup_text(a) else "") + '</td>'
                         f"<td>{esc(place)}</td><td align=\"center\">{p or ''}"
                         + (f'<br><span style="font-size:11px;color:#5f6368">{esc(type_code(a))}</span>'
                            if type_code(a) else "") + "</td>"
                         f'<td align="center" style="white-space:nowrap">{diff_html(d) or "-"}</td>'
                         f'<td align="center" style="white-space:nowrap">{esc(seen)}</td></tr>')
                if hist:
                    def change_html(x: str) -> str:
                        m = re.search(r" ([▲▼][^ ]+)$", x)
                        return esc(x[:m.start()] if m else x) + (diff_html(m.group(1)) if m else "")
                    cell = f"↳ {change_html(hist[0])}"
                    if len(hist) > 1:
                        # 누르면 펼쳐짐 (삼성 이메일·아이폰 메일). Gmail 은 지원하지 않아 펼쳐진 채로 보인다.
                        cell += ('<details style="margin:2px 0 0 12px"><summary style="cursor:pointer;'
                                 f'color:#0b57d0">이전 변동 {len(hist) - 1}건 ▾</summary>'
                                 + "<br>".join(change_html(x) for x in hist[1:]) + "</details>")
                    h.append('<tr><td colspan="5" style="font-size:12px;color:#5f6368;padding:0 4px 6px 14px">'
                             + cell + "</td></tr>")
            h.append("</table>")
        text.append("")
    h.append('<p style="color:#5f6368;font-size:13px;margin-top:20px">📎 첨부한 엑셀(CSV) 파일을 열면 '
             '전체 매매 매물을 동·층·가격 등 원하는 열로 정렬해 볼 수 있습니다.</p>')
    h.append('<p style="color:#9aa0a6;font-size:12px;margin-top:24px">사라짐은 두 번 연속 조회에서 목록에 없던 매물입니다. '
             '거래 완료인지 중개사가 내린 것인지는 구분할 수 없습니다.</p></div>')
    return subject, "\n".join(text), "".join(h)


def mail_configured() -> bool:
    return bool(setting("SMTP_USER") and setting("SMTP_PASSWORD"))


def send_mail(subject: str, text: str, html_body: str | None = None,
              attachments: list[tuple[str, bytes, str]] | None = None, to_self: bool = False) -> str:
    """Gmail 등 SMTP 로 메일을 보내고 받는 주소를 돌려준다.

    local.json(또는 환경변수)의 SMTP_USER, SMTP_PASSWORD(Gmail 앱 비밀번호),
    MAIL_TO(없으면 SMTP_USER, 쉼표로 여러 주소), SMTP_HOST(기본 smtp.gmail.com), SMTP_PORT(기본 465) 를 쓴다.
    """
    user, password = setting("SMTP_USER"), setting("SMTP_PASSWORD").replace(" ", "")
    # 받는 주소는 쉼표로 여러 개 적을 수 있다
    # to_self 면 보내는 주소로만 (테스트용)
    to = ", ".join(x.strip() for x in ((user if to_self else setting("MAIL_TO")) or user).split(",")
                   if x.strip())
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for name, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    with smtplib.SMTP_SSL(setting("SMTP_HOST", "smtp.gmail.com"),
                          int(setting("SMTP_PORT", "465")), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return to


def tracked_info(state: dict, a: dict) -> dict:
    """현재 매물에 추적 기록(처음 본 날·처음 가격)을 붙인다."""
    ids = {a["articleNo"], *a.get("aliases", [])}
    for key, t in (state.get("tracked") or {}).items():
        if ids & {key, t.get("rep"), *t.get("aliases", [])}:
            first_won = t.get("first_price_won")
            out = dict(a, first_seen=t.get("first_seen"),
                       first_price=won_text(first_won) if first_won else t.get("first_price"),
                       first_price_won=first_won, price_history=t.get("price_history", []))
            # 여러 중개사면 그 집의 최저 호가를 보여준다 (링크도 그 중개사 매물로)
            prices = a.get("member_prices") or {}
            if prices and not a.get("rent_won"):
                won, no = min((w, n) for n, w in prices.items())
                out.update(link_no=no, price_won=won, price=won_text(won))
            return out
    return a


# ---------------------------------------------------------------- Main

def resolve_complexes(naver: NaverLand, keyword: str, region: str) -> dict:
    """키워드로 단지를 검색해 {단지번호: 단지명} 을 돌려준다."""
    queries = [keyword]
    if " " in keyword:
        queries.append(re.sub(r"\s+", "", keyword))
    for q in queries:
        found = [c for c in naver.search_complexes(q)
                 if matches_keyword(c["complexName"], keyword)
                 and (not region or region in c["address"])]
        if found:
            for c in found:
                print(f"단지 발견 [{keyword}]: {c['complexName']} ({c['complexNo']}) {c['address']}")
            return {c["complexNo"]: c["complexName"] for c in found}
    return {}


def main() -> int:
    if sys.argv[1:2] == ["--history"]:
        return print_history(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    if sys.argv[1:2] == ["--status"]:
        return print_status()
    if sys.argv[1:2] == ["--mail-preview"]:
        return mail_preview(to_self="--all" not in sys.argv[2:])
    if sys.argv[1:2] == ["--check-brokers"]:
        return check_brokers(sys.argv[2] if len(sys.argv) > 2 else None)
    if sys.argv[1:2] == ["--mail-test"]:
        if not mail_configured():
            print("local.json 에 SMTP_USER, SMTP_PASSWORD 를 먼저 설정하세요.")
            return 1
        to = send_mail("[매물 브리핑] 메일 설정 확인", "메일 설정이 잘 됐습니다. 매일 브리핑이 이 주소로 옵니다.")
        print(f"테스트 메일을 보냈습니다: {to}")
        return 0
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keywords: list[str] = config.get("keywords") or ([config["keyword"]] if config.get("keyword") else [])
    region = config.get("region", "")
    trade_types = config.get("trade_types", ["A1", "B1", "B2"])
    pyeongs = [int(p) for p in config.get("pyeong", [])]
    state = load_state()
    naver = NaverLand()

    # 단지 목록: config 의 complexes(수동 지정) + 키워드별 검색 결과(state 에 캐시)
    complexes: dict = {str(k): v for k, v in (config.get("complexes") or {}).items()}
    cache: dict = state.get("keyword_complexes") or {}
    new_cache: dict = {}
    searched = False
    for kw in keywords:
        found = cache.get(kw)
        if not found:
            if searched:
                time.sleep(3)  # 연속 검색으로 429(요청 과다)가 나지 않도록 간격을 둔다
            searched = True
            try:
                found = resolve_complexes(naver, kw, region)
            except Exception as e:  # noqa: BLE001
                print(f"경고: '{kw}' 단지 검색 실패: {e}", file=sys.stderr)
        if not found:
            print(f"경고: '{kw}' 단지를 찾지 못했습니다. config.json 의 complexes 에 단지번호를 직접 지정하세요.")
            continue
        new_cache[kw] = found
        complexes.update(found)
    state["keyword_complexes"] = new_cache
    if not complexes:
        print("감시할 단지가 없습니다.")
        return 1

    initialized = set(state.get("initialized_complexes") or [])

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    new: list[dict] = []
    gone: list[dict] = []
    reports: dict = {}  # 단지번호 → 브리핑 내용
    wanted = lambda a: matches_pyeong(a, pyeongs)  # noqa: E731
    ok = 0
    abort = ""
    # 감시 대상에서 빠진 단지·거래유형(예: 매매만 보기로 바꾼 뒤의 전세·월세)의 기록은 정리한다
    trades = {TRADE_NAMES.get(t, t) for t in trade_types}
    state["tracked"] = {k: t for k, t in (state.get("tracked") or {}).items()
                        if t["complexNo"] in complexes and t.get("trade") in trades}
    state["recent_gone"] = {k: g for k, g in (state.get("recent_gone") or {}).items()
                            if g.get("complexNo") in complexes and g.get("trade") in trades}
    state["events"] = [e for e in state.get("events") or []
                       if e["item"].get("trade", "매매") in trades]
    try:
        for i, (no, name) in enumerate(complexes.items()):
            if i:
                time.sleep(3)  # 단지 사이 간격 (429 방지)
            try:
                items = naver.articles(no, trade_types)
            except (BlockedError, BrowserError) as e:
                print(f"경고: {name} ({no}) 조회 실패, 나머지 단지도 건너뜁니다: {e}", file=sys.stderr)
                abort = str(e)
                break
            except Exception as e:  # noqa: BLE001  한 단지 실패가 전체를 막지 않도록
                print(f"경고: {name} ({no}) 조회 실패: {e}", file=sys.stderr)
                reports[no] = {"status": "failed", "error": str(e)}
                continue
            ok += 1
            fresh = diff_and_update(state, items, now)
            changes: list[dict] = []
            relisted: set = set()
            lost = track_listings(state, no, items, today, wanted, changes,
                                  stamp=now.isoformat(timespec="minutes"), relisted=relisted)
            fresh = [a for a in fresh if a["articleNo"] not in relisted]  # 다시 올린 집은 신규 아님
            gone += lost
            report = {"status": "ok", "gone": lost, "changes": changes,
                      "listings": [tracked_info(state, a) for a in items if wanted(a)]}
            if no in initialized:
                matched = [a for a in fresh if wanted(a)]
                print(f"{name} ({no}): 매물 {len(items)}건, 새 매물 {len(fresh)}건 (조건 일치 {len(matched)}건)")
                new += matched
                report["new"] = matched
            else:
                # 처음 보는 단지: 현재 매물은 기준으로만 저장하고 알림은 보내지 않는다.
                initialized.add(no)
                print(f"{name} ({no}): 매물 {len(items)}건 기준 저장 (첫 조회, 알림 없음)")
                report["status"] = "first"
            reports[no] = report
    finally:
        naver.close()
    state["initialized_complexes"] = sorted(initialized)

    if not ok:
        print("매물을 조회한 단지가 없습니다.")
    elif new:
        title, body = format_message(new, complexes, pyeongs)
        print(title + "\n" + body)
        sent = [n for n, f in (("telegram", notify_telegram), ("github", notify_github))
                if f(title, body)]
        print(f"알림 전송: {', '.join(sent) or '(설정된 채널 없음)'}")
    else:
        print("조건에 맞는 새 매물 없음")

    if gone:
        append_history(gone, complexes)
        title, body = format_gone(gone, complexes, pyeongs)
        print(title + "\n" + body)
        for f in (notify_telegram, notify_github):
            f(title, body)

    since = parse_time(state.get("last_run"))
    log_events(state, reports, now, since)
    global SHOW_TIME, SORT_ORDER
    SHOW_TIME = bool(config.get("show_time", True))
    SORT_ORDER = list(config.get("sort") or ["price", "dong", "-floor"])
    mail_hours = [int(x) for x in config.get("mail_hours") or []]
    if mail_configured() and mail_hours and now.hour not in mail_hours:
        print(f"메일 전송 생략: 브리핑 시간({', '.join(map(str, mail_hours))}시)이 아님")
    elif mail_configured():
        last_mail = parse_time(state.get("last_mail"))
        briefing = []
        for no, name in complexes.items():
            r = dict(reports.get(no) or {"status": "failed", "error": abort or "조회하지 못했습니다"},
                     no=no, name=name, week=recent_events(state, no, now))
            # 지난 메일 이후 여러 번 조회했으면 아직 메일로 안 보낸 변동을 모두 담는다
            pending = [e for e in r["week"] if not e.get("mailed")][::-1]
            multi = len({e["at"] for e in pending}) > 1
            for kind, key in (("new", "new"), ("change", "changes"), ("gone", "gone")):
                r[key] = [dict(e["item"], _at=e["at"] if multi else None)
                          for e in pending if e["kind"] == kind]
            briefing.append(r)
        subject, text, html_body = format_briefing(briefing, size_label(pyeongs), now,
                                                   last_mail or since)
        try:
            csv_name = f"매매매물_{now:%Y%m%d}.csv"
            to = send_mail(subject, text, html_body, [(csv_name, listings_csv(briefing), "text/csv")])
            print(f"메일 전송 완료: {to}")
            state["last_mail"] = now.isoformat(timespec="minutes")
            shown = {r["no"] for r in briefing if r["status"] != "failed"}  # 실패 단지는 다음 메일에
            for e in state.get("events") or []:
                if e["complexNo"] in shown:
                    e["mailed"] = True
        except Exception as e:  # noqa: BLE001
            print(f"메일 전송 실패: {e}", file=sys.stderr)

    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
