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

    def test_track_gone_after_two_misses(self):
        state = {}
        self.assertEqual(na.track_listings(state, "1", [art(1), art(2)], "2026-09-28"), [])
        self.assertEqual(na.track_listings(state, "1", [art(1)], "2026-09-29"), [])  # 한 번 빠짐
        gone = na.track_listings(state, "1", [art(1)], "2026-09-30")
        self.assertEqual([g["articleNo"] for g in gone], ["2"])
        self.assertEqual(gone[0]["gone_date"], "2026-09-29")  # 처음 빠진 날
        self.assertEqual(gone[0]["first_seen"], "2026-09-28")
        self.assertEqual(list(state["tracked"]), ["1"])

    def test_track_reappear_resets(self):
        state = {}
        na.track_listings(state, "1", [art(1)], "2026-09-28")
        na.track_listings(state, "1", [], "2026-09-29")
        self.assertEqual(na.track_listings(state, "1", [dict(art(1), price="7억")], "2026-09-30"), [])
        t = state["tracked"]["1"]
        self.assertEqual((t["missed"], t["price"], t["first_price"]), (0, "7억", "8억"))
        self.assertEqual(na.track_listings(state, "1", [], "2026-10-01"), [])

    def test_track_other_complex_untouched(self):
        state = {}
        na.track_listings(state, "1", [art(1)], "2026-09-28")
        for day in ("2026-09-29", "2026-09-30"):
            self.assertEqual(na.track_listings(state, "2", [], day), [])
        self.assertIn("1", state["tracked"])

    def test_track_price_change(self):
        state, changes = {}, []
        na.track_listings(state, "1", [dict(art(1), price_won=800_000_000)], "2026-09-28", changes=changes)
        cut = dict(art(1), price="7억 5,000만", price_won=750_000_000)
        na.track_listings(state, "1", [cut], "2026-09-29", changes=changes)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual((c["old_price"], c["price"]), ("8억", "7억 5,000만"))
        self.assertEqual(na.change_diff(c), "▼5,000만")
        self.assertEqual(state["tracked"]["1"]["price_history"][-1], ["2026-09-29", "7억 5,000만", 750_000_000])
        # 같은 가격이면 변동 없음
        na.track_listings(state, "1", [cut], "2026-09-30", changes=changes)
        self.assertEqual(len(changes), 1)

    def test_track_representative_switch_not_price_change(self):
        # 같은 집을 다른 중개사가 다른 호가로 올린 것으로 대표 매물이 바뀐 경우
        state, changes = {}, []
        na.track_listings(state, "1", [dict(art(1), aliases=["2"], price_won=800_000_000)],
                          "2026-09-28", changes=changes)
        na.track_listings(state, "1", [dict(art(2), aliases=["1"], price_won=780_000_000)],
                          "2026-09-29", changes=changes)
        self.assertEqual(changes, [])
        na.track_listings(state, "1", [dict(art(2), aliases=["1"], price_won=770_000_000)],
                          "2026-09-30", changes=changes)
        self.assertEqual(len(changes), 1)  # 그 뒤 같은 중개사 매물의 변동은 잡는다

    def test_rent_only_change(self):
        c = {"old_price_won": 50_000_000, "price_won": 50_000_000,
             "old_rent_won": 1_500_000, "rent_won": 1_400_000}
        self.assertEqual(na.change_diff(c), "월세 ▼10만")

    def test_track_filter_change_not_gone(self):
        state = {}
        na.track_listings(state, "1", [art(1)], "2026-09-28")
        no_match = lambda a: False  # 조건이 바뀌어 더 이상 맞지 않아도 목록에 있으면 사라진 게 아님
        for day in ("2026-09-29", "2026-09-30"):
            self.assertEqual(na.track_listings(state, "1", [art(1), art(2)], day, no_match), [])
        self.assertEqual(list(state["tracked"]), ["1"])  # 조건에 안 맞는 새 매물은 추적 안 함

    def test_track_representative_change(self):
        state = {}
        na.track_listings(state, "1", [dict(art(1), aliases=["2"])], "2026-09-28")
        for day in ("2026-09-29", "2026-09-30"):
            self.assertEqual(na.track_listings(state, "1", [dict(art(2), aliases=["1"])], day), [])
        self.assertEqual(list(state["tracked"]), ["1"])

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

    def test_pyeong_range(self):
        # 서면아이파크2단지 실제 면적: 86.23/59.78㎡ → 26평, 75.24/52.6㎡ → 23평
        self.assertTrue(na.matches_pyeong({"supply": 86.23, "exclusive": 59.78}, [25, 26]))
        self.assertFalse(na.matches_pyeong({"supply": 75.24, "exclusive": 52.6}, [25, 26]))
        title, _ = na.format_message([art(1, "7")], {"7": "A"}, [26, 25])
        self.assertIn("25~26평 새 매물 1건", title)

    def test_dong_suffix(self):
        a = na.normalize_fin({"representativeArticleInfo": {"articleNumber": 1, "dongName": "201"}}, "9")
        self.assertEqual(a["building"], "201동")
        a = na.normalize_fin({"representativeArticleInfo": {"articleNumber": 1, "dongName": "A동"}}, "9")
        self.assertEqual(a["building"], "A동")

    def test_floor_text(self):
        self.assertEqual(na.floor_text("중/29"), "중층 (총 29층)")
        self.assertEqual(na.floor_text("10/29"), "10층 (총 29층)")
        self.assertEqual(na.floor_text("5"), "5층")
        self.assertEqual(na.floor_text(""), "")

    def test_format_gone(self):
        g = dict(art(5, "7"), price="7억 5,000만", first_price="8억", first_seen="2026-09-18",
                 gone_date="2026-09-30")
        title, body = na.format_gone([g], {"7": "서면아이파크2단지"}, [25, 26])
        self.assertIn("25~26평 사라진 매물 1건 · 서면아이파크2단지", title)
        self.assertIn("[매매] 7억 5,000만 · 101동 10층 (총 30층)", body)
        self.assertIn("처음 8억", body)
        self.assertIn("09/18부터 12일 게시", body)

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
            (na, "HISTORY_PATH", self.tmp / "history.csv"),
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


    def test_briefing_mail_sent(self):
        orig = (na.LOCAL_PATH, na.smtplib.SMTP_SSL)
        na.LOCAL_PATH = self.tmp / "local.json"
        na.LOCAL_PATH.write_text(json.dumps({"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw"}))
        na.smtplib.SMTP_SSL = FakeSMTP
        FakeSMTP.sent = []
        try:
            self.cfg.write_text(json.dumps(
                {"keywords": [], "complexes": {"1": "서면아이파크1단지", "blocked": "양정"},
                 "pyeong": [25]}, ensure_ascii=False))
            fake = FakeNaver({"1": [dict(art(1), price_won=800_000_000)]})
            self.run_main(fake)
            fake.listings["1"] = [dict(art(1), price="7억", price_won=700_000_000),
                                  dict(art(2), price_won=810_000_000)]
            self.run_main(fake)
        finally:
            na.LOCAL_PATH, na.smtplib.SMTP_SSL = orig
        msgs = [m[1] for m in FakeSMTP.sent if m[0] == "msg"]
        self.assertEqual(len(msgs), 2)  # 매일 한 통
        self.assertIn("신규 1 · 가격변동 1", msgs[1]["Subject"])
        html_body = msgs[1].get_body(("html",)).get_content()
        self.assertIn("8억 → 7억", html_body)
        self.assertIn("조회 실패", html_body)  # 차단된 단지도 브리핑에 표시
        self.assertIn("최근 7일 변동 2건", html_body)  # 신규 1 + 가격 1, 확인 시각과 함께
        self.assertEqual(len(na.load_state()["events"]), 2)

    def run_at(self, fake, hour):
        class FixedDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 3, hour, tzinfo=na.KST)
        orig = na.datetime
        na.datetime = FixedDT
        try:
            return self.run_main(fake)
        finally:
            na.datetime = orig

    def test_mail_hours_and_pending(self):
        orig = (na.LOCAL_PATH, na.smtplib.SMTP_SSL)
        na.LOCAL_PATH = self.tmp / "local.json"
        na.LOCAL_PATH.write_text(json.dumps({"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw",
                                             "MAIL_TO": "a@example.com, b@example.com"}))
        na.smtplib.SMTP_SSL = FakeSMTP
        FakeSMTP.sent = []
        try:
            self.cfg.write_text(json.dumps(
                {"keywords": [], "complexes": {"1": "서면아이파크1단지"}, "pyeong": [25],
                 "mail_hours": [10]}, ensure_ascii=False))
            fake = FakeNaver({"1": [dict(art(1), price_won=800_000_000)]})
            self.run_at(fake, 8)       # 기준 저장, 메일 시간 아님
            fake.listings["1"].append(dict(art(2), price_won=810_000_000))
            self.run_at(fake, 9)       # 신규 확인, 메일 시간 아님
            fake.listings["1"][0] = dict(art(1), price="7억", price_won=700_000_000)
            self.run_at(fake, 10)      # 가격 변동 확인 + 메일
            self.run_at(fake, 11)      # 메일 시간 아님
        finally:
            na.LOCAL_PATH, na.smtplib.SMTP_SSL = orig
        msgs = [m[1] for m in FakeSMTP.sent if m[0] == "msg"]
        self.assertEqual(len(msgs), 1)  # 10시에만
        self.assertEqual(msgs[0]["To"], "a@example.com, b@example.com")
        self.assertIn("신규 1 · 가격변동 1", msgs[0]["Subject"])  # 9시 신규도 10시 메일에
        body = msgs[0].get_body(("html",)).get_content()
        self.assertIn("10/03 오전 9시 확인", body)
        self.assertTrue(all(e.get("mailed") for e in na.load_state()["events"]))

    def test_trade_type_removed_not_gone(self):
        self.cfg.write_text(json.dumps({"keywords": [], "complexes": {"1": "A"}, "pyeong": [25],
                                        "trade_types": ["A1", "B2"]}, ensure_ascii=False))
        fake = FakeNaver({"1": [art(1), dict(art(2), trade="월세")]})
        self.run_main(fake)
        self.assertEqual(sorted(na.load_state()["tracked"]), ["1", "2"])
        # 매매만 보기로 바꾸면 월세 매물은 목록에 없어도 '사라짐' 이 아니라 기록에서 정리
        self.cfg.write_text(json.dumps({"keywords": [], "complexes": {"1": "A"}, "pyeong": [25],
                                        "trade_types": ["A1"]}, ensure_ascii=False))
        fake.listings["1"] = [art(1)]
        self.run_main(fake)
        self.run_main(fake)
        self.assertEqual(sorted(na.load_state()["tracked"]), ["1"])
        self.assertFalse(self.sent)
        self.assertFalse((self.tmp / "history.csv").exists())

    def test_gone_history(self):
        import contextlib
        import io
        self.cfg.write_text(json.dumps(
            {"keywords": [], "complexes": {"1": "서면아이파크1단지"}, "pyeong": [25]},
            ensure_ascii=False))
        big = dict(art(99), supply=112.0, exclusive=84.0)
        fake = FakeNaver({"1": [art(1), art(2), big]})
        self.run_main(fake)
        self.assertEqual(sorted(na.load_state()["tracked"]), ["1", "2"])  # 34평은 추적 안 함
        fake.listings["1"] = [art(1)]
        self.run_main(fake)
        self.assertEqual(self.sent, [])  # 한 번 빠진 것은 아직 사라진 것으로 보지 않음
        self.run_main(fake)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("사라진 매물 1건", self.sent[0][0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            na.print_history()
        self.assertIn("서면아이파크1단지 · [매매] 8억 · 101동 10층 (총 30층) · 25평", out.getvalue())
        self.assertNotIn("2", na.load_state()["tracked"])


class BriefingTest(unittest.TestCase):
    def test_format_briefing(self):
        listing = dict(art(5, "7"), price_won=800_000_000, first_price_won=850_000_000,
                       first_seen="2026-09-28")
        change = dict(listing, old_price="8억 5,000만", old_price_won=850_000_000, old_rent_won=None)
        reports = [
            {"no": "7", "name": "서면아이파크2단지", "status": "ok", "listings": [listing],
             "new": [dict(art(6, "7"), price_won=790_000_000)], "changes": [change], "gone": []},
            {"no": "8", "name": "연산더샵", "status": "first", "listings": [], "gone": [], "changes": []},
            {"no": "9", "name": "양정", "status": "failed", "error": "429"},
        ]
        subject, text, body = na.format_briefing(reports, "25~26평", datetime(2026, 10, 3, 13, tzinfo=na.KST))
        self.assertEqual(subject, "[매물 브리핑] 10/03 오후 1시 25~26평 · 신규 1 · 가격변동 1 · 사라짐 0")
        _, text, _ = na.format_briefing(reports, "25~26평", datetime(2026, 10, 3, 8, tzinfo=na.KST))
        self.assertTrue(text.startswith("10/03 오전 8시 매물 브리핑"))
        self.assertIn('href="https://fin.land.naver.com/complexes/7?tab=article"', body)  # 단지 링크
        self.assertIn('href="https://fin.land.naver.com/articles/6"', body)  # 신규 매물 링크
        self.assertIn("8억 5,000만 → 8억", body)
        self.assertIn("▼5,000만", body)
        self.assertIn("첫 조회", text)
        self.assertIn("조회 실패: 429", text)

    def test_events_window(self):
        state = {}
        t0 = datetime(2026, 10, 1, 8, tzinfo=na.KST)
        na.log_events(state, {"7": {"new": [dict(art(1, "7"), price_won=1)]}}, t0, None)
        t1 = datetime(2026, 10, 9, 13, tzinfo=na.KST)
        chg = dict(art(2, "7"), old_price="8억", price="7억", price_won=700_000_000,
                   old_price_won=800_000_000)
        na.log_events(state, {"7": {"changes": [chg]}, "8": {"gone": [art(3, "8")]}}, t1,
                      datetime(2026, 10, 9, 8, tzinfo=na.KST))
        week = na.recent_events(state, "7", t1)
        self.assertEqual([e["kind"] for e in week], ["change"])  # 8일 전 신규는 7일 밖
        self.assertEqual(week[0]["since"], "2026-10-09T08:00+09:00")
        self.assertEqual(len(state["events"]), 3)  # 14일까지는 보관
        na.log_events(state, {}, datetime(2026, 10, 20, 8, tzinfo=na.KST), t1)
        self.assertEqual([e["complexNo"] for e in state["events"]], ["7", "8"])

    def test_briefing_week_and_window(self):
        now = datetime(2026, 10, 3, 13, tzinfo=na.KST)
        week = [{"at": "2026-10-03T08:00+09:00", "kind": "change", "complexNo": "7",
                 "item": {"articleNo": "5", "trade": "매매", "old_price": "6억 2,000만",
                          "price": "5억 9,000만", "old_price_won": 620_000_000,
                          "price_won": 590_000_000, "building": "201동", "floor": "중/29"}},
                {"at": "2026-10-02T20:00+09:00", "kind": "gone", "complexNo": "7",
                 "item": {"articleNo": "4", "trade": "매매", "price": "5억 7,000만",
                          "first_seen": "2026-09-20", "gone_date": "2026-10-02"}}]
        r = {"no": "7", "name": "서면아이파크2단지", "status": "ok", "listings": [],
             "new": [], "changes": [], "gone": [], "week": week}
        _, text, body = na.format_briefing([r], "25~26평", now, datetime(2026, 10, 3, 8, tzinfo=na.KST))
        self.assertIn("이번 변동: 오전 8시 ~ 오후 1시 사이", text)  # 같은 날이면 날짜 생략
        self.assertIn("이번 변동 없음", text)
        self.assertIn("최근 7일 변동 2건", body)
        self.assertIn("10/03 오전 8시 [가격] [매매] 6억 2,000만 → 5억 9,000만 (▼3,000만)", text)
        self.assertIn("10/02 오후 8시 [사라짐] [매매] 5억 7,000만", text)
        self.assertIn('href="https://fin.land.naver.com/articles/5"', body)
        _, text, _ = na.format_briefing([r], "25~26평", now, datetime(2026, 10, 2, 20, tzinfo=na.KST))
        self.assertIn("10/02 오후 8시 ~ 오후 1시 사이", text)

    def test_recent_price_changes(self):
        state, changes = {}, []
        for i, (won, stamp) in enumerate([(800_000_000, None), (780_000_000, "2026-10-01T20:00+09:00"),
                                          (760_000_000, "2026-10-02T08:00+09:00"),
                                          (770_000_000, "2026-10-03T13:00+09:00")]):
            na.track_listings(state, "1", [dict(art(1), price=na.won_text(won), price_won=won)],
                              f"2026-10-0{i}", changes=changes, stamp=stamp)
        a = na.tracked_info(state, art(1))
        self.assertEqual(na.recent_price_changes(a), [
            "10/03 오후 1시 7억 6,000만 → 7억 7,000만 ▲1,000만",
            "10/02 오전 8시 7억 8,000만 → 7억 6,000만 ▼2,000만"])
        r = {"no": "1", "name": "A", "status": "ok", "new": [], "changes": [], "gone": [],
             "listings": [dict(a, trade="매매", price="7억 7,000만", price_won=770_000_000)]}
        _, text, body = na.format_briefing([r], "25평", datetime(2026, 10, 3, 13, tzinfo=na.KST))
        self.assertIn("↳ 10/03 오후 1시 7억 6,000만 → 7억 7,000만 ▲1,000만", text)
        self.assertIn("(이전 변동 2건)", text)
        # HTML: 최근 1개는 바로 보이고 나머지는 펼치기 안에
        self.assertIn("이전 변동 2건 ▾</summary>10/02 오전 8시 7억 8,000만 → 7억 6,000만", body)
        self.assertLess(body.index("10/03 오후 1시 7억 6,000만"), body.index("<details"))

    def test_date_only(self):
        na.SHOW_TIME = False
        try:
            r = {"no": "7", "name": "A", "status": "ok", "listings": [], "new": [], "changes": [], "gone": []}
            subject, text, _ = na.format_briefing([r], "25~26평", datetime(2026, 10, 3, 10, tzinfo=na.KST),
                                                  datetime(2026, 10, 2, 10, tzinfo=na.KST))
            self.assertTrue(subject.startswith("[매물 브리핑] 10/03 25~26평"))
            self.assertIn("이번 변동: 10/02 ~ 10/03 사이", text)
            self.assertNotIn("오전", text)
            self.assertEqual(na.stamp_label("2026-10-03T10:00+09:00"), "10/03")
        finally:
            na.SHOW_TIME = True

    def test_html_escaped(self):
        r = {"no": "1", "name": "<b>단지</b>", "status": "failed", "error": "<script>"}
        _, _, body = na.format_briefing([r], "25평", datetime(2026, 10, 3, tzinfo=na.KST))
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;b&gt;단지", body)


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.login_args = (user, password)
        FakeSMTP.sent.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(("msg", msg))


class MailTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = (na.LOCAL_PATH, na.smtplib.SMTP_SSL)
        na.LOCAL_PATH = self.tmp / "local.json"
        na.smtplib.SMTP_SSL = FakeSMTP
        FakeSMTP.sent = []

    def tearDown(self):
        na.LOCAL_PATH, na.smtplib.SMTP_SSL = self.orig

    def test_not_configured(self):
        self.assertFalse(na.mail_configured())

    def test_send_mail_from_local_json(self):
        na.LOCAL_PATH.write_text(json.dumps(
            {"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "abcd efgh ijkl mnop"}))
        self.assertTrue(na.mail_configured())
        self.assertEqual(na.send_mail("제목", "본문", "<p>본문</p>"), "me@example.com")
        self.assertEqual(FakeSMTP.sent[0], ("login", "me@example.com", "abcdefghijklmnop"))
        msg = FakeSMTP.sent[1][1]
        self.assertEqual((msg["To"], msg["Subject"]), ("me@example.com", "제목"))
        self.assertIn("<p>본문</p>", msg.get_body(("html",)).get_content())

    def test_env_overrides_local_json(self):
        na.LOCAL_PATH.write_text(json.dumps({"MAIL_TO": "a@example.com"}))
        na.os.environ["MAIL_TO"] = "b@example.com"
        try:
            self.assertEqual(na.setting("MAIL_TO"), "b@example.com")
        finally:
            del na.os.environ["MAIL_TO"]
        self.assertEqual(na.setting("MAIL_TO"), "a@example.com")


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
