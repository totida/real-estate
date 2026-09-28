#!/usr/bin/env python3
"""네이버 부동산 신규 매물 알림.

config.json 에 지정한 단지들의 매물 목록을 네이버 부동산에서
조회하고, 이전 실행 때 없던 매물이 생기면 GitHub 이슈 / 텔레그램으로 알린다.
표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"

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


def http_get(url: str, headers: dict | None = None, retries: int = 3) -> str:
    hdrs = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
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


def http_json(url: str, headers: dict | None = None):
    return json.loads(http_get(url, headers))


# ---------------------------------------------------------------- Naver

class NaverLand:
    """new.land.naver.com API, 실패 시 m.land.naver.com API 로 대체."""

    NEW = "https://new.land.naver.com"
    MOBILE = "https://m.land.naver.com"

    def __init__(self) -> None:
        self._token: str | None = None

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

    def articles(self, complex_no: str, trade_types: list[str]) -> list[dict]:
        try:
            return self._articles_new(complex_no, trade_types)
        except Exception as e:  # noqa: BLE001
            print(f"  new.land 조회 실패({e}), m.land 로 재시도", file=sys.stderr)
            return self._articles_mobile(complex_no, trade_types)

    def _articles_new(self, complex_no: str, trade_types: list[str]) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 51):
            params = {
                "realEstateType": "APT:ABYG:JGC:PRE",
                "tradeType": ":".join(trade_types),
                "priceType": "RETAIL",
                "sameAddressGroup": "false",
                "showArticle": "false",
                "order": "dateDesc",
                "type": "list",
                "complexNo": complex_no,
                "page": page,
            }
            url = (f"{self.NEW}/api/articles/complex/{complex_no}?"
                   + urllib.parse.urlencode(params))
            data = http_json(url, self._new_headers())
            out += [normalize_new(a, complex_no) for a in data.get("articleList", [])]
            if not data.get("isMoreData"):
                break
            time.sleep(1)
        return out

    def _articles_mobile(self, complex_no: str, trade_types: list[str]) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 51):
            params = {
                "hscpNo": complex_no,
                "tradTpCd": ":".join(trade_types),
                "order": "date_",
                "showR0": "N",
                "page": page,
            }
            url = f"{self.MOBILE}/complex/getComplexArticleList?" + urllib.parse.urlencode(params)
            data = http_json(url, {"Referer": f"{self.MOBILE}/"})
            result = data.get("result") or {}
            out += [normalize_mobile(a, complex_no) for a in result.get("list") or []]
            if result.get("moreDataYn") != "Y":
                break
            time.sleep(1)
        return out


def normalize_new(a: dict, complex_no: str) -> dict:
    price = a.get("dealOrWarrantPrc", "")
    if a.get("rentPrc"):
        price = f"{price}/{a['rentPrc']}"
    return {
        "articleNo": str(a["articleNo"]),
        "complexNo": complex_no,
        "name": a.get("articleName", ""),
        "trade": a.get("tradeTypeName", ""),
        "price": price,
        "building": a.get("buildingName", ""),
        "floor": a.get("floorInfo", ""),
        "area": f"{a.get('area1', '')}/{a.get('area2', '')}㎡",
        "supply": to_float(a.get("area1")),
        "exclusive": to_float(a.get("area2")),
        "direction": a.get("direction", ""),
        "desc": a.get("articleFeatureDesc", ""),
        "realtor": a.get("realtorName", ""),
        "confirmed": a.get("articleConfirmYmd", ""),
    }


def normalize_mobile(a: dict, complex_no: str) -> dict:
    return {
        "articleNo": str(a["atclNo"]),
        "complexNo": complex_no,
        "name": a.get("atclNm", ""),
        "trade": a.get("tradTpNm", ""),
        "price": a.get("prcInfo", ""),
        "building": a.get("bildNm", ""),
        "floor": a.get("flrInfo", ""),
        "area": f"{a.get('spc1', '')}/{a.get('spc2', '')}㎡",
        "supply": to_float(a.get("spc1")),
        "exclusive": to_float(a.get("spc2")),
        "direction": a.get("direction", ""),
        "desc": a.get("atclFetrDesc", ""),
        "realtor": a.get("rltrNm", ""),
        "confirmed": a.get("cfmYmd", ""),
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


def article_url(a: dict) -> str:
    return f"https://new.land.naver.com/complexes/{a['complexNo']}?articleNo={a['articleNo']}"


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
    """
    seen: dict = state.setdefault("seen", {})
    today = now.strftime("%Y-%m-%d")
    new = []
    for a in articles:
        if a["articleNo"] not in seen:
            new.append(a)
        seen[a["articleNo"]] = today
    cutoff = (now - timedelta(days=prune_days)).strftime("%Y-%m-%d")
    for no in [k for k, d in seen.items() if d < cutoff]:
        del seen[no]
    return new


# ---------------------------------------------------------------- Notify

def format_message(new: list[dict], complexes: dict,
                   pyeongs: list[int] | None = None) -> tuple[str, str]:
    size = f" {'/'.join(map(str, pyeongs))}평" if pyeongs else ""
    names = list(dict.fromkeys(complexes.get(a["complexNo"], a["name"]) for a in new))
    where = names[0] + (f" 외 {len(names) - 1}곳" if len(names) > 1 else "")
    title = f"🏠{size} 새 매물 {len(new)}건 · {where} ({datetime.now(KST):%m/%d %H:%M})"
    sections = []
    for cname in names:
        lines = [f"### {cname}"]
        for a in new:
            if complexes.get(a["complexNo"], a["name"]) != cname:
                continue
            p = pyeong_of(a)
            size_txt = f"{p}평 ({a['area']})" if p else a["area"]
            head = f"[{a['trade']}] {a['price']} · {a['building']} {a['floor']}층 · {size_txt}"
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
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keywords: list[str] = config.get("keywords") or [config["keyword"]]
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
    new: list[dict] = []
    ok = 0
    for i, (no, name) in enumerate(complexes.items()):
        if i:
            time.sleep(3)  # 단지 사이 간격 (429 방지)
        try:
            items = naver.articles(no, trade_types)
        except Exception as e:  # noqa: BLE001  한 단지 실패가 전체를 막지 않도록
            print(f"경고: {name} ({no}) 조회 실패: {e}", file=sys.stderr)
            continue
        ok += 1
        fresh = diff_and_update(state, items, now)
        if no in initialized:
            matched = [a for a in fresh if matches_pyeong(a, pyeongs)]
            print(f"{name} ({no}): 매물 {len(items)}건, 새 매물 {len(fresh)}건 (조건 일치 {len(matched)}건)")
            new += matched
        else:
            # 처음 보는 단지: 현재 매물은 기준으로만 저장하고 알림은 보내지 않는다.
            initialized.add(no)
            print(f"{name} ({no}): 매물 {len(items)}건 기준 저장 (첫 조회, 알림 없음)")
    state["initialized_complexes"] = sorted(initialized)

    if new:
        title, body = format_message(new, complexes, pyeongs)
        print(title + "\n" + body)
        sent = [n for n, f in (("telegram", notify_telegram), ("github", notify_github))
                if f(title, body)]
        print(f"알림 전송: {', '.join(sent) or '(설정된 채널 없음)'}")
    else:
        print("조건에 맞는 새 매물 없음")

    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
