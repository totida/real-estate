#!/usr/bin/env python3
"""네이버 부동산 신규 매물 알림.

config.json 에 지정한 단지들의 매물 목록을 네이버 부동산(fin.land)에서
조회하고, 이전 실행 때 없던 매물이 생기면 GitHub 이슈 / 텔레그램으로 알린다.
매물 조회에는 크로미움이 필요하고, 파이썬은 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"
HISTORY_PATH = ROOT / "state" / "history.csv"

KST = timezone(timedelta(hours=9))
M2_PER_PYEONG = 3.305785
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TRADE_NAMES = {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}


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

    def articles(self, complex_no: str, trade_types: list[str]) -> list[dict]:
        page = self._page()
        out: list[dict] = []
        last_info: list = []
        seed = f"alert-{int(time.time())}"
        for _ in range(50):
            payload = {
                "size": 30, "complexNumber": complex_no, "tradeTypes": trade_types,
                "pyeongTypes": [], "dongNumbers": [], "userChannelType": "PC",
                "articleSortType": "RANKING_DESC", "seed": seed, "lastInfo": last_info,
            }
            status, text = page.fetch_json(self.fin + self.ARTICLE_API, payload)
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
            result = data["result"]
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
    return {
        "articleNo": no,
        "aliases": aliases,
        "complexNo": complex_no,
        "name": a.get("complexName", ""),
        "trade": TRADE_NAMES.get(trade, trade),
        "price": fin_price(a.get("priceInfo") or {}),
        "building": dong + "동" if dong.isdigit() else dong,
        "floor": detail.get("floorInfo", ""),
        "area": f"{space.get('supplySpace', '')}/{space.get('exclusiveSpace', '')}㎡",
        "supply": to_float(space.get("supplySpace")),
        "exclusive": to_float(space.get("exclusiveSpace")),
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
    return f"https://fin.land.naver.com/articles/{a['articleNo']}"


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


TRACK_KEYS = ("complexNo", "name", "trade", "price", "building", "floor", "area",
              "supply", "exclusive", "realtor")


def track_listings(state: dict, complex_no: str, articles: list[dict], today: str,
                   wanted=lambda a: True, misses_to_gone: int = 2) -> list[dict]:
    """조건(wanted)에 맞는 매물을 추적하고, 목록에서 사라진 매물을 돌려준다.

    있는지 여부는 조건과 상관없이 단지의 전체 매물(articles)로 판단한다
    (평형 조건을 바꿔도 조건에서 빠진 매물이 '사라진 매물' 로 기록되지 않도록).
    조회에 성공한 단지에 대해서만 부른다. 한 번 빠진 것은 일시적 누락일 수 있어
    misses_to_gone 번 연속으로 안 보여야 사라진 것으로 본다 (사라진 날은 처음 빠진 날).
    같은 집의 다른 매물번호(aliases)가 보이면 계속 있는 것으로 본다.
    """
    tracked: dict = state.setdefault("tracked", {})
    by_id = {}
    for a in articles:
        for i in (a["articleNo"], *a.get("aliases", [])):
            by_id[i] = a
    matched_today = set()
    gone = []
    for key, t in list(tracked.items()):
        if t["complexNo"] != complex_no:
            continue
        cur = next((by_id[i] for i in (key, *t.get("aliases", [])) if i in by_id), None)
        if cur is not None:
            matched_today.add(id(cur))
            t.update({k: cur.get(k) for k in TRACK_KEYS}, complexNo=complex_no)
            t["aliases"] = sorted(set(t.get("aliases", [])) | {cur["articleNo"], *cur.get("aliases", [])}
                                  - {key})
            t["last_seen"], t["missed"] = today, 0
            t.pop("missing_since", None)
            continue
        t["missed"] = t.get("missed", 0) + 1
        t.setdefault("missing_since", today)
        if t["missed"] >= misses_to_gone:
            gone.append(dict(t, articleNo=key, gone_date=t["missing_since"]))
            del tracked[key]
    for a in articles:
        if (id(a) in matched_today or not wanted(a)
                or any(i in tracked for i in (a["articleNo"], *a.get("aliases", [])))):
            continue
        tracked[a["articleNo"]] = dict({k: a.get(k) for k in TRACK_KEYS}, complexNo=complex_no,
                                       aliases=a.get("aliases", []), first_seen=today,
                                       first_price=a.get("price"), last_seen=today, missed=0)
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
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
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
    ok = 0
    try:
        for i, (no, name) in enumerate(complexes.items()):
            if i:
                time.sleep(3)  # 단지 사이 간격 (429 방지)
            try:
                items = naver.articles(no, trade_types)
            except (BlockedError, BrowserError) as e:
                print(f"경고: {name} ({no}) 조회 실패, 나머지 단지도 건너뜁니다: {e}", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001  한 단지 실패가 전체를 막지 않도록
                print(f"경고: {name} ({no}) 조회 실패: {e}", file=sys.stderr)
                continue
            ok += 1
            fresh = diff_and_update(state, items, now)
            gone += track_listings(state, no, items, today, lambda a: matches_pyeong(a, pyeongs))
            if no in initialized:
                matched = [a for a in fresh if matches_pyeong(a, pyeongs)]
                print(f"{name} ({no}): 매물 {len(items)}건, 새 매물 {len(fresh)}건 (조건 일치 {len(matched)}건)")
                new += matched
            else:
                # 처음 보는 단지: 현재 매물은 기준으로만 저장하고 알림은 보내지 않는다.
                initialized.add(no)
                print(f"{name} ({no}): 매물 {len(items)}건 기준 저장 (첫 조회, 알림 없음)")
    finally:
        naver.close()
    state["initialized_complexes"] = sorted(initialized)
    # 감시 대상에서 빠진 단지의 추적 기록은 정리한다
    state["tracked"] = {k: t for k, t in (state.get("tracked") or {}).items()
                        if t["complexNo"] in complexes}

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

    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
