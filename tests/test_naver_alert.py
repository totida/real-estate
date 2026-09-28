import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import naver_alert as na  # noqa: E402


def art(no, complex_no="1"):
    return {"articleNo": str(no), "complexNo": complex_no, "name": "서면아이파크1단지",
            "trade": "매매", "price": "8억", "building": "101동", "floor": "10/30",
            "area": "83/59㎡", "supply": 83.0, "exclusive": 59.0, "direction": "남향", "desc": "", "realtor": "OO공인",
            "confirmed": "20260928"}


class DiffTest(unittest.TestCase):
    def test_new_articles_detected(self):
        now = datetime(2026, 9, 28, tzinfo=na.KST)
        state = {"seen": {}}
        self.assertEqual(len(na.diff_and_update(state, [art(1), art(2)], now)), 2)
        new = na.diff_and_update(state, [art(1), art(2), art(3)], now)
        self.assertEqual([a["articleNo"] for a in new], ["3"])

    def test_temporarily_missing_not_realerted(self):
        now = datetime(2026, 9, 28, tzinfo=na.KST)
        state = {"seen": {}}
        na.diff_and_update(state, [art(1)], now)
        na.diff_and_update(state, [], now + timedelta(days=3))
        self.assertEqual(na.diff_and_update(state, [art(1)], now + timedelta(days=4)), [])

    def test_representative_change_not_realerted(self):
        # 같은 집을 여러 중개사가 올린 경우 대표 매물 번호가 바뀌어도 새 매물이 아니다
        now = datetime(2026, 9, 28, tzinfo=na.KST)
        state = {"seen": {}}
        na.diff_and_update(state, [dict(art(1), aliases=["2", "3"])], now)
        self.assertEqual(na.diff_and_update(state, [dict(art(3), aliases=["1"])], now), [])

    def test_prune_old(self):
        now = datetime(2026, 9, 28, tzinfo=na.KST)
        state = {"seen": {"old": "2026-09-01"}}
        na.diff_and_update(state, [], now)
        self.assertNotIn("old", state["seen"])


class PyeongTest(unittest.TestCase):
    def test_supply_area(self):
        self.assertEqual(na.pyeong_of({"supply": 83.0, "exclusive": 59.9}), 25)
        self.assertEqual(na.pyeong_of({"supply": 112.0, "exclusive": 84.9}), 34)

    def test_filter(self):
        self.assertTrue(na.matches_pyeong({"supply": 82.5, "exclusive": 59.0}, [25]))
        self.assertFalse(na.matches_pyeong({"supply": 112.0, "exclusive": 84.0}, [25]))
        self.assertTrue(na.matches_pyeong({"supply": 112.0}, []))

    def test_exclusive_only_estimate(self):
        self.assertTrue(na.matches_pyeong({"supply": None, "exclusive": 59.0}, [25]))
        self.assertFalse(na.matches_pyeong({"supply": None, "exclusive": 84.0}, [25]))

    def test_missing_area_included(self):
        self.assertTrue(na.matches_pyeong({"supply": None, "exclusive": None}, [25]))

    def test_normalize_parses_area(self):
        a = na.normalize_fin({"representativeArticleInfo": {
            "articleNumber": 1, "spaceInfo": {"supplySpace": "83", "exclusiveSpace": "59.97"}}}, "9")
        self.assertTrue(na.matches_pyeong(a, [25]))
        a = na.normalize_fin({"representativeArticleInfo": {
            "articleNumber": 2, "spaceInfo": {"supplySpace": 112, "exclusiveSpace": 84}}}, "9")
        self.assertFalse(na.matches_pyeong(a, [25]))


class ParseTest(unittest.TestCase):
    def test_keyword_matches_both_complexes(self):
        for name in ("서면아이파크1단지", "서면 아이파크 2단지", "서면아이파크"):
            self.assertTrue(na.matches_keyword(name, "서면아이파크"), name)
        self.assertFalse(na.matches_keyword("서면롯데캐슬", "서면아이파크"))

    def test_keyword_tokens_any_order(self):
        self.assertTrue(na.matches_keyword("문현롯데캐슬인피니엘", "롯데캐슬 인피니엘"))
        self.assertTrue(na.matches_keyword("양정포레힐즈스위첸", "양정포레힐즈 스위첸"))
        self.assertFalse(na.matches_keyword("롯데캐슬골드", "롯데캐슬 인피니엘"))

    def test_normalize_fin(self):
        item = {
            "representativeArticleInfo": {
                "articleNumber": 2500000001, "complexName": "서면아이파크1단지", "tradeType": "B2",
                "dongName": "101동", "priceInfo": {"warrantyPrice": 50_000_000, "rentPrice": 1_500_000},
                "spaceInfo": {"supplySpace": 83.5, "exclusiveSpace": 59.9},
                "articleDetail": {"floorInfo": "10/30", "direction": "SS",
                                  "articleFeatureDescription": "올수리"},
                "brokerInfo": {"brokerageName": "OO공인중개사"},
                "verificationInfo": {"articleConfirmDate": "2026-09-28"}},
            "duplicatedArticleInfo": {"realtorCount": 3, "articleInfoList": [
                {"articleNumber": 2500000001}, {"articleNumber": 2500000002},
                {"articleNumber": 2500000003}]},
        }
        a = na.normalize_fin(item, "119101")
        self.assertEqual(a["articleNo"], "2500000001")
        self.assertEqual(a["aliases"], ["2500000002", "2500000003"])
        self.assertEqual((a["trade"], a["price"]), ("월세", "5,000만/150만"))
        self.assertEqual(na.pyeong_of(a), 25)
        self.assertEqual(a["realtor"], "OO공인중개사 외 2곳")
        self.assertEqual(na.article_url(a), "https://fin.land.naver.com/articles/2500000001")

    def test_won_text(self):
        self.assertEqual(na.won_text(850_000_000), "8억 5,000만")
        self.assertEqual(na.won_text(1_000_000_000), "10억")
        self.assertEqual(na.won_text(1_500_000), "150만")
        self.assertEqual(na.fin_price({"dealPrice": 612_000_000}), "6억 1,200만")

    def test_http_json_rejects_non_object(self):
        orig = na.http_get
        try:
            for body in ("null", "<html>abuse</html>"):
                na.http_get = lambda url, headers=None, b=body: b
                with self.assertRaisesRegex(RuntimeError, "예상 밖 응답"):
                    na.http_json("https://x")
        finally:
            na.http_get = orig

    def test_format_message(self):
        cx = {"7": "서면아이파크2단지", "8": "연산더샵"}
        title, body = na.format_message([art(1, "7"), art(2, "8"), art(3, "7")], cx, [25])
        self.assertIn("25평 새 매물 3건 · 서면아이파크2단지 외 1곳", title)
        self.assertIn("### 서면아이파크2단지", body)
        self.assertIn("### 연산더샵", body)
        self.assertIn("25평 (83/59㎡)", body)
        self.assertIn("서면아이파크2단지", body)


class RetryDelayTest(unittest.TestCase):
    def http_error(self, code, headers=None):
        import email.message
        import urllib.error
        msg = email.message.Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        return urllib.error.HTTPError("https://x", code, "err", msg, None)

    def test_429_waits_longer(self):
        self.assertEqual(na.retry_delay(self.http_error(429), 0), 20)
        self.assertEqual(na.retry_delay(self.http_error(429), 1), 40)

    def test_429_honors_retry_after(self):
        self.assertEqual(na.retry_delay(self.http_error(429, {"Retry-After": "7"}), 0), 7)

    def test_other_errors_short(self):
        self.assertEqual(na.retry_delay(self.http_error(500), 0), 2)


class FakeNaver:
    def __init__(self, listings):
        self.listings = listings  # {complexNo: [articles]}
        self.searches = []

    def search_complexes(self, kw):
        self.searches.append(kw)
        db = [{"complexNo": "1", "complexName": "서면아이파크1단지", "address": "부산시 부산진구 전포동"},
              {"complexNo": "2", "complexName": "서면아이파크2단지", "address": "부산시 부산진구 전포동"},
              {"complexNo": "3", "complexName": "연산더샵", "address": "부산시 연제구 연산동"},
              {"complexNo": "9", "complexName": "연산더샵", "address": "서울시 어딘가"}]
        return [c for c in db if na.matches_keyword(c["complexName"], kw)]

    def articles(self, no, trade_types):
        if no == "fail":
            raise RuntimeError("boom")
        if no == "blocked":
            raise na.BlockedError("429")
        return self.listings.get(no, [])

    def close(self):
        self.closed = True


class MainTest(unittest.TestCase):
    def setUp(self):
        import json, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.state = self.tmp / "seen.json"
        self.write_cfg = lambda kws: self.cfg.write_text(json.dumps(
            {"keywords": kws, "region": "부산", "pyeong": [25]}, ensure_ascii=False))
        self.sent = []
        self.patches = [
            (na, "CONFIG_PATH", self.cfg), (na, "STATE_PATH", self.state),
            (na, "notify_github", lambda t, b: self.sent.append((t, b)) or True),
            (na, "notify_telegram", lambda t, b: False),
            (na.time, "sleep", lambda s: None),
            (na, "NaverLand", na.NaverLand),
        ]
        self.orig = [(m, n, getattr(m, n)) for m, n, _ in self.patches]
        for m, n, v in self.patches:
            setattr(m, n, v)
        # load_state/save_state 기본 인자는 정의 시점에 고정되므로 래핑
        self._load, self._save = na.load_state, na.save_state
        na.load_state = lambda: self._load(self.state)
        na.save_state = lambda st: self._save(st, self.state)

    def tearDown(self):
        for m, n, v in self.orig:
            setattr(m, n, v)
        na.load_state, na.save_state = self._load, self._save

    def run_main(self, fake):
        na.NaverLand = lambda: fake
        return na.main()

    def test_blocked_stops_remaining_complexes(self):
        import json
        self.cfg.write_text(json.dumps(
            {"keywords": [], "complexes": {"blocked": "A", "1": "B"}}, ensure_ascii=False))
        fake = FakeNaver({"1": [art(1)]})
        self.assertEqual(self.run_main(fake), 1)  # 한 단지도 조회 못 함
        self.assertTrue(fake.closed)
        self.assertEqual(na.load_state()["initialized_complexes"], [])

    def test_complexes_only_skips_search(self):
        import json
        self.cfg.write_text(json.dumps(
            {"keywords": [], "complexes": {"1": "서면아이파크1단지"}, "pyeong": [25]},
            ensure_ascii=False))
        fake = FakeNaver({"1": [art(1)]})
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(fake.searches, [])
        fake.listings["1"].append(art(2))
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(len(self.sent), 1)

    def test_flow(self):
        big = dict(art(99), supply=112.0, exclusive=84.0)
        self.write_cfg(["서면아이파크"])
        fake = FakeNaver({"1": [art(1)], "2": [art(2)]})
        self.assertEqual(self.run_main(fake), 0)
        self.assertEqual(self.sent, [])  # 첫 조회는 알림 없음

        fake.listings["2"].append(art(3, "2"))
        fake.listings["1"].append(dict(big, complexNo="1"))  # 34평은 제외
        self.run_main(fake)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("새 매물 1건", self.sent[0][0])
        self.assertIn("fin.land.naver.com/articles/3", self.sent[0][1])

        # 단지 추가: 새 단지의 기존 매물은 알림 없이 기준 저장, 서울 동명 단지는 제외
        self.write_cfg(["서면아이파크", "연산더샵"])
        fake.listings["3"] = [art(10, "3")]
        fake.searches.clear()
        self.run_main(fake)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(fake.searches, ["연산더샵"])  # 기존 키워드는 캐시 사용
        st = na.load_state()
        self.assertEqual(st["initialized_complexes"], ["1", "2", "3"])

        fake.listings["3"].append(art(11, "3"))
        self.run_main(fake)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("연산더샵", self.sent[1][0])


class ChromiumIntegrationTest(unittest.TestCase):
    """실제 크로미움으로 가짜 fin.land 서버에 붙어 매물 조회 전 과정을 확인한다."""

    def setUp(self):
        try:
            na.find_chromium()
        except RuntimeError:
            self.skipTest("크로미움 없음")
        import http.server
        import threading
        requests = self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"<html><body>map</body></html>"
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                requests.append((self.path, payload, self.headers.get("user-agent", "")))
                page = len(requests)
                if payload["complexNumber"] == "bad":
                    body = b'{"isSuccess": false, "detailCode": "ERR"}'
                    self.send_response(200)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                items = [{"representativeArticleInfo": {
                    "articleNumber": 1000 + page, "tradeType": "A1",
                    "priceInfo": {"dealPrice": 800_000_000},
                    "spaceInfo": {"supplySpace": 83, "exclusiveSpace": 59}}}]
                result = {"list": items, "hasNextPage": page == 1, "lastInfo": ["p", page]}
                body = json.dumps({"isSuccess": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()

    def test_articles_via_browser(self):
        self.check_articles()

    def test_failed_response_is_error_not_empty(self):
        naver = na.NaverLand(fin=self.base)
        try:
            with self.assertRaisesRegex(RuntimeError, "예상 밖 응답"):
                naver.articles("bad", ["A1"])
        finally:
            naver.close()

    def test_articles_via_xvfb(self):
        import shutil
        if not shutil.which("Xvfb"):
            self.skipTest("Xvfb 없음")
        orig = na.os.environ.get("CHROMIUM_XVFB")
        na.os.environ["CHROMIUM_XVFB"] = "1"
        try:
            self.check_articles()
        finally:
            if orig is None:
                del na.os.environ["CHROMIUM_XVFB"]
            else:
                na.os.environ["CHROMIUM_XVFB"] = orig

    def check_articles(self):
        naver = na.NaverLand(fin=self.base)
        try:
            got = naver.articles("119101", ["A1", "B1"])
        finally:
            naver.close()
        self.assertEqual([a["articleNo"] for a in got], ["1001", "1002"])
        self.assertEqual(got[0]["price"], "8억")
        path, payload, ua = self.requests[0]
        self.assertEqual(path, na.NaverLand.ARTICLE_API)
        self.assertEqual((payload["complexNumber"], payload["tradeTypes"]), ("119101", ["A1", "B1"]))
        self.assertEqual(self.requests[1][1]["lastInfo"], ["p", 1])
        self.assertNotIn("Headless", ua)


if __name__ == "__main__":
    unittest.main()
