#!/usr/bin/env python3
"""네이버 부동산 신규 매물 알림.

config.json 에 지정한 단지(기본: 서면아이파크)의 매물 목록을 네이버 부동산에서
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TRADE_NAMES = {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}


# ---------------------------------------------------------------- HTTP

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
            time.sleep(2 * (attempt + 1))
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
        "direction": a.get("direction", ""),
        "desc": a.get("atclFetrDesc", ""),
        "realtor": a.get("rltrNm", ""),
        "confirmed": a.get("cfmYmd", ""),
    }


def article_url(a: dict) -> str:
    return f"https://new.land.naver.com/complexes/{a['complexNo']}?articleNo={a['articleNo']}"


def matches_keyword(name: str, keyword: str) -> bool:
    squash = lambda s: re.sub(r"\s+", "", s).lower()  # noqa: E731
    return squash(keyword) in squash(name)


# ---------------------------------------------------------------- State

def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"initialized": False, "complexes": {}, "seen": {}}


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

def format_message(keyword: str, new: list[dict], complexes: dict) -> tuple[str, str]:
    title = f"🏠 {keyword} 새 매물 {len(new)}건 ({datetime.now(KST):%m/%d %H:%M})"
    lines = []
    for a in new:
        cname = complexes.get(a["complexNo"], a["name"])
        head = f"[{a['trade']}] {a['price']} · {cname} {a['building']} {a['floor']}층 · {a['area']}"
        extra = " · ".join(x for x in (a["direction"], a["desc"], a["realtor"]) if x)
        lines.append(f"- **{head}**\n  {extra}\n  {article_url(a)}")
    return title, "\n".join(lines)


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
    text = f"{title}\n\n{body.replace('**', '')}"
    for i in range(0, len(text), 4000):  # 텔레그램 메시지 길이 제한
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 4000],
                                       "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20):
            pass
    return True


# ---------------------------------------------------------------- Main

def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    keyword = config["keyword"]
    trade_types = config.get("trade_types", ["A1", "B1", "B2"])
    state = load_state()
    naver = NaverLand()

    complexes: dict = {str(k): v for k, v in (config.get("complexes") or {}).items()}
    if not complexes:
        complexes = state.get("complexes") or {}
    if not complexes:
        found = [c for c in naver.search_complexes(keyword)
                 if matches_keyword(c["complexName"], keyword)]
        if not found:
            print(f"'{keyword}' 단지를 찾지 못했습니다. config.json 의 complexes 를 직접 지정하세요.")
            return 1
        complexes = {c["complexNo"]: c["complexName"] for c in found}
        for c in found:
            print(f"단지 발견: {c['complexName']} ({c['complexNo']}) {c['address']}")
    state["complexes"] = complexes

    articles: list[dict] = []
    for no, name in complexes.items():
        items = naver.articles(no, trade_types)
        print(f"{name} ({no}): 매물 {len(items)}건")
        articles += items

    now = datetime.now(KST)
    new = diff_and_update(state, articles, now)

    if not state.get("initialized"):
        # 첫 실행: 현재 매물은 기준으로만 저장하고 알림은 보내지 않는다.
        state["initialized"] = True
        print(f"초기화 완료: 기존 매물 {len(articles)}건 저장 (알림 없음)")
    elif new:
        title, body = format_message(keyword, new, complexes)
        print(title + "\n" + body)
        sent = [n for n, f in (("telegram", notify_telegram), ("github", notify_github))
                if f(title, body)]
        print(f"알림 전송: {', '.join(sent) or '(설정된 채널 없음)'}")
    else:
        print("새 매물 없음")

    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
