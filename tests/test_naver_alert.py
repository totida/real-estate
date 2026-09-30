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
        self.assertEqual(state["tracked"]["1"]["price_history"][-1], ["2026-09-29", "7억 5,000만", 750_000_000, "8억", 800_000_000, "", ""])
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

    def test_member_price_change_when_representative_switches(self):
        # 같은 집을 두 중개사가 올림: 1번 8억, 2번 7억 9천. 다음 날 대표가 2번으로 바뀌고 1번이 7억 8천으로 내림
        state, changes = {}, []
        g1 = dict(art(1), aliases=["2"], price_won=800_000_000,
                  member_prices={"1": 800_000_000, "2": 790_000_000})
        na.track_listings(state, "1", [g1], "2026-09-28", changes=changes)
        g2 = dict(art(2), aliases=["1"], price="7억 9,000만", price_won=790_000_000,
                  member_prices={"1": 780_000_000, "2": 790_000_000})
        na.track_listings(state, "1", [g2], "2026-09-29", changes=changes)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual((c["articleNo"], c["old_price"], c["price"]), ("1", "8억", "7억 8,000만"))
        # 표에는 그 집의 최저 호가(1번 7억8천), 처음 대비는 처음 봤을 때 최저 호가(7억9천)와 비교
        info = na.tracked_info(state, g2)
        self.assertEqual((info["link_no"], info["price"]), ("1", "7억 8,000만"))
        self.assertEqual(info["first_price_won"], 790_000_000)
        self.assertEqual(na.recent_price_changes(info), ["09/29 8억 → 7억 8,000만 ▼2,000만"])

    def test_broker_price_scenarios(self):
        def grp(prices, rep="1"):
            return {"articleNo": rep, "aliases": [k for k in prices if k != rep], "complexNo": "1",
                    "trade": "매매", "price": na.won_text(prices[rep]), "price_won": prices[rep],
                    "member_prices": prices}
        base = {"1": 600_000_000, "2": 600_000_000, "3": 600_000_000}
        cases = {
            "올림": ({"1": 600_000_000, "2": 620_000_000, "3": 600_000_000}, [("2", "6억", "6억 2,000만")]),
            "내림": ({"1": 600_000_000, "2": 600_000_000, "3": 580_000_000}, [("3", "6억", "5억 8,000만")]),
            "동시": ({"1": 600_000_000, "2": 620_000_000, "3": 580_000_000},
                   [("2", "6억", "6억 2,000만"), ("3", "6억", "5억 8,000만")]),
            "재등록": ({"1": 600_000_000, "2": 600_000_000, "4": 580_000_000}, [("4", "6억", "5억 8,000만")]),
            "그대로 재등록": ({"1": 600_000_000, "2": 600_000_000, "4": 600_000_000}, []),
        }
        for name, (day2, want) in cases.items():
            state, ch = {}, []
            na.track_listings(state, "1", [grp(base)], "2026-10-01", changes=ch)
            na.track_listings(state, "1", [grp(day2)], "2026-10-02", changes=ch)
            self.assertEqual([(c["articleNo"], c["old_price"], c["price"]) for c in ch], want, name)

    def test_lowest_price_shown_and_min_based_stats(self):
        def grp(prices):
            return {"articleNo": "1", "aliases": ["2", "3"], "complexNo": "1", "trade": "매매",
                    "price": na.won_text(prices["1"]), "price_won": prices["1"], "member_prices": prices,
                    "member_brokers": {"1": "C공인", "2": "A부동산", "3": "B부동산"}}
        state, ch = {}, []
        na.track_listings(state, "1", [grp({"1": 600_000_000, "2": 600_000_000, "3": 600_000_000})],
                          "2026-10-01", changes=ch, stamp="2026-10-01T10:00+09:00")
        # 10/02 A 6억2천으로 올림: 최저가(6억) 그대로 → 목록엔 있지만 통계 제외
        g = grp({"1": 600_000_000, "2": 620_000_000, "3": 600_000_000})
        na.track_listings(state, "1", [g], "2026-10-02", changes=ch, stamp="2026-10-02T10:00+09:00")
        self.assertEqual(na.tracked_info(state, g)["price"], "6억")
        self.assertEqual((ch[0]["broker"], na.follow_text(ch[0]), na.counted(ch[0])), ("A부동산", "최저가 그대로", False))
        # 10/03 B 5억8천으로 내림: 최저가 6억 → 5억8천 → 통계에 하락 1
        g = grp({"1": 600_000_000, "2": 620_000_000, "3": 580_000_000})
        na.track_listings(state, "1", [g], "2026-10-03", changes=ch, stamp="2026-10-03T10:00+09:00")
        info = na.tracked_info(state, g)
        self.assertEqual((info["price"], info["link_no"]), ("5억 8,000만", "3"))
        self.assertIn("articles/3", na.article_url(info))
        self.assertEqual(na.diff_text(info["first_price_won"], info["price_won"]), "▼2,000만")
        self.assertTrue(na.counted(ch[1]))
        self.assertEqual(na.change_stats(ch), "상승 0 · 하락 1 · 평균 ▼2,000만 (-3.3%) (최저가 안 바뀐 1건 제외)")
        self.assertEqual(na.dup_text(info), "중개사 3곳 · 5억 8,000만~6억 2,000만")
        self.assertEqual(na.recent_price_changes(info, 1), ["10/03 오전 10시 B부동산 6억 → 5억 8,000만 ▼2,000만"])

    def test_second_cut_above_lowest(self):
        # 모두 6억 → 10/02 A 5억8천 → 10/03 B 5억9천: 표는 최저 5억8천 유지, B 변동은 통계 제외
        def grp(p):
            return {"articleNo": "1", "aliases": ["2", "3"], "complexNo": "1", "trade": "매매",
                    "price": na.won_text(p["1"]), "price_won": p["1"], "member_prices": p,
                    "member_brokers": {"1": "C공인", "2": "A부동산", "3": "B부동산"}}
        state, ch = {}, []
        for d, p in [("01", {"1": 600_000_000, "2": 600_000_000, "3": 600_000_000}),
                     ("02", {"1": 600_000_000, "2": 580_000_000, "3": 600_000_000}),
                     ("03", {"1": 600_000_000, "2": 580_000_000, "3": 590_000_000})]:
            g = grp(p)
            na.track_listings(state, "1", [g], f"2026-10-{d}", changes=ch, stamp=f"2026-10-{d}T10:00+09:00")
        info = na.tracked_info(state, g)
        self.assertEqual(info["price"], "5억 8,000만")
        self.assertEqual(na.diff_text(info["first_price_won"], info["price_won"]), "▼2,000만")
        self.assertEqual([na.counted(c) for c in ch], [True, False])
        self.assertEqual(na.follow_text(ch[1]), "최저가 그대로")
        self.assertEqual(na.change_counts(ch), "▼1")
        # 최저가를 부르던 중개사가 올리면 최저가가 오른 만큼 상승으로 센다
        state2, ch2 = {}, []
        for d, p in [("01", {"1": 600_000_000, "2": 580_000_000}), ("02", {"1": 600_000_000, "2": 590_000_000})]:
            g = {"articleNo": "1", "aliases": ["2"], "complexNo": "1", "trade": "매매", "price": "x",
                 "price_won": p["1"], "member_prices": p}
            na.track_listings(state2, "1", [g], f"2026-10-{d}", changes=ch2)
        self.assertEqual(na.change_stats(ch2), "상승 1 · 하락 0 · 평균 ▲1,000만 (+1.7%)")

    def test_change_stats(self):
        cs = [{"old_price_won": 600_000_000, "price_won": 620_000_000},
              {"old_price_won": 500_000_000, "price_won": 480_000_000},
              {"old_price_won": 400_000_000, "price_won": 390_000_000}]
        self.assertEqual(na.change_stats(cs), "상승 1 · 하락 2 · 평균 ▼333만 (-1.1%)")
        self.assertEqual(na.change_counts(cs), "▲1 ▼2")
        self.assertEqual(na.change_counts([]), "0")
        self.assertEqual(na.change_stats([]), "")

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
        att = [p for p in msgs[0].iter_attachments()]
        self.assertEqual(att[0].get_filename(), "매매매물_20261003.csv")
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

    def test_status_broker_check(self):
        import contextlib
        import io
        self.cfg.write_text(json.dumps({"keywords": [], "complexes": {"1": "A"}, "pyeong": [25]},
                                       ensure_ascii=False))
        g = dict(art(1), aliases=["2"], realtor_count=2, price_won=800_000_000,
                 member_prices={"1": 800_000_000, "2": 790_000_000},
                 member_brokers={"1": "OO공인", "2": "XX부동산"})
        self.run_main(FakeNaver({"1": [g]}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            na.print_status()
        self.assertIn("여러 중개사 매물 1건 중 중개사별 호가가 들어온 매물 1건", out.getvalue())
        self.assertIn("XX부동산 7억 9,000만 / OO공인 8억", out.getvalue())

    def test_mail_preview_uses_saved_state(self):
        orig = (na.LOCAL_PATH, na.smtplib.SMTP_SSL)
        na.LOCAL_PATH = self.tmp / "local.json"
        na.LOCAL_PATH.write_text(json.dumps({"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw",
                                             "MAIL_TO": "a@example.com, b@example.com"}))
        na.smtplib.SMTP_SSL = FakeSMTP
        FakeSMTP.sent = []
        try:
            self.cfg.write_text(json.dumps({"keywords": [], "complexes": {"1": "서면아이파크1단지"},
                                            "pyeong": [25]}, ensure_ascii=False))
            self.run_main(FakeNaver({"1": [dict(art(1), price_won=800_000_000)]}))
            before = self.state.read_text(encoding="utf-8")
            self.assertEqual(na.mail_preview(to_self=False), 0)
            self.assertEqual([m[1] for m in FakeSMTP.sent if m[0] == "msg"][-1]["To"],
                             "a@example.com, b@example.com")  # --all 이면 받는 사람 전체
            self.assertEqual(na.mail_preview(), 0)
            self.assertEqual(self.state.read_text(encoding="utf-8"), before)  # 기록은 그대로
        finally:
            na.LOCAL_PATH, na.smtplib.SMTP_SSL = orig
        msg = [m[1] for m in FakeSMTP.sent if m[0] == "msg"][-1]
        self.assertTrue(msg["Subject"].startswith("[매물 브리핑 미리보기]"))
        self.assertEqual(msg["To"], "me@example.com")  # 기본은 보내는 주소로만
        self.assertIn("현재 매매 1건", msg.get_body(("html",)).get_content())

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
        self.assertIn("가격변동 1 (상승 0 · 하락 1 · 평균 ▼5,000만 (-5.9%))", body)
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

    def test_sort_listings(self):
        rows = [dict(art(1), building="102동", floor="5/25", price_won=500),
                dict(art(2), building="101동", floor="고/25", price_won=500),
                dict(art(3), building="101동", floor="3/25", price_won=500),
                dict(art(4), building="101동", floor="1/25", price_won=400)]
        ids = lambda rs: [a["articleNo"] for a in rs]  # noqa: E731
        self.assertEqual(ids(na.sort_listings(rows, ["price", "dong", "-floor"])), ["4", "2", "3", "1"])
        self.assertEqual(ids(na.sort_listings(rows, ["dong", "floor"])), ["4", "3", "2", "1"])
        self.assertEqual(na.sort_label(["price", "dong", "-floor"]), "가격↑ → 동↑ → 층↓")
        self.assertEqual(na.floor_num({"floor": "고/25"}), 20)
        self.assertEqual(na.floor_num({"floor": "12/25"}), 12)

    def test_listings_csv(self):
        a = dict(art(1), price_won=620_000_000, price="6억 2,000만", first_price_won=630_000_000,
                 first_price="6억 3,000만", first_seen="2026-09-29")
        data = na.listings_csv([{"name": "서면아이파크2단지", "listings": [a]}])
        self.assertTrue(data.startswith("\ufeff".encode()))
        import csv as _csv
        rows = list(_csv.DictReader(data.decode("utf-8-sig").splitlines()))
        self.assertEqual(rows[0]["가격(만원)"], "62000")
        self.assertEqual(rows[0]["처음대비(만원)"], "-1000")
        self.assertEqual(rows[0]["링크"], "https://fin.land.naver.com/articles/1")
        b = dict(a, member_prices={"1": 620_000_000, "7": 615_000_000},
                 member_brokers={"1": "OO공인", "7": "XX부동산"})
        rows = list(_csv.DictReader(na.listings_csv([{"name": "A", "listings": [b]}]).decode("utf-8-sig").splitlines()))
        self.assertEqual(rows[0]["중개사 수"], "2")
        self.assertEqual((rows[0]["최저호가(만원)"], rows[0]["최고호가(만원)"]), ("61500", "62000"))
        self.assertEqual(rows[0]["중개사별 호가"], "XX부동산 6억 1,500만 / OO공인 6억 2,000만")

    def test_normalize_fin_member_brokers(self):
        item = {"representativeArticleInfo": {"articleNumber": 1, "priceInfo": {"dealPrice": 620_000_000},
                                              "brokerInfo": {"brokerageName": "OO공인"}},
                "duplicatedArticleInfo": {"realtorCount": 2, "articleInfoList": [
                    {"articleNumber": 7, "priceInfo": {"dealPrice": 615_000_000},
                     "brokerInfo": {"brokerageName": "XX부동산"}}]}}
        a = na.normalize_fin(item, "9")
        self.assertEqual(a["member_prices"], {"1": 620_000_000, "7": 615_000_000})
        self.assertEqual(a["member_brokers"], {"1": "OO공인", "7": "XX부동산"})
        self.assertEqual(a["realtor_count"], 2)
        self.assertEqual(na.dup_text(a), "중개사 2곳 · 6억 1,500만~6억 2,000만")
        self.assertIn("중개사 2곳", na.where_text(a))
        r = {"no": "9", "name": "A", "status": "ok", "new": [], "changes": [], "gone": [],
             "listings": [dict(a, trade="매매", price="6억 2,000만")]}
        _, text, body = na.format_briefing([r], "25평", datetime(2026, 10, 3, tzinfo=na.KST))
        self.assertIn("중개사 2곳 · 6억 1,500만~6억 2,000만</span>", body)
        self.assertEqual(na.dup_text(dict(a, realtor_count=1, member_prices={})), "")

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
                if payload["complexNumber"] == "dup":
                    items[0]["duplicatedArticleInfo"] = {"realtorCount": 2, "articleInfoList": [
                        {"articleNumber": 2000, "priceInfo": {"dealPrice": 790_000_000},
                         "brokerInfo": {"brokerageName": "XX부동산"}}]}
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

    def test_check_brokers(self):
        import contextlib
        import io
        import tempfile
        cfg = Path(tempfile.mkdtemp()) / "config.json"
        cfg.write_text(json.dumps({"complexes": {"dup": "A"}, "trade_types": ["A1"]}))
        orig = (na.CONFIG_PATH, na.NaverLand.FIN)
        na.CONFIG_PATH, na.NaverLand.FIN = cfg, self.base
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                na.check_brokers()
        finally:
            na.CONFIG_PATH, na.NaverLand.FIN = orig
        self.assertIn("여러 중개사 매물 1건", out.getvalue())
        self.assertIn("XX부동산 7억 9,000만", out.getvalue())
        self.assertIn("✅", out.getvalue())

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


class ReviewScenarioTest(unittest.TestCase):
    """검토에서 찾은 경우들 (가격 단위: 억)."""

    @staticmethod
    def grp(prices, brokers=None, rep=None, **kw):
        prices = {k: round(v * 100_000_000) for k, v in prices.items()}
        rep = rep or next(iter(prices))
        low = min(prices.values())
        return dict({"articleNo": rep, "aliases": [k for k in prices if k != rep], "complexNo": "1",
                     "trade": "매매", "price": na.won_text(low), "price_won": low, "member_prices": prices,
                     "member_brokers": brokers or {k: f"중개{k}" for k in prices},
                     "building": "101동", "floor": "10/30", "supply": 83.0, "exclusive": 59.0}, **kw)

    def run_days(self, days, wanted=lambda a: True):
        state, out = {}, []
        for i, g in enumerate(days, 1):
            ch, relisted = [], set()
            gone = na.track_listings(state, "1", g, f"2026-10-{i:02d}", wanted, ch,
                                     stamp=f"2026-10-{i:02d}T10:00+09:00", relisted=relisted)
            out.append((ch, gone, relisted))
        return state, out

    def test_1_cut_not_counted_as_rise_when_cheapest_leaves(self):
        _, out = self.run_days([[self.grp({"A": 5.8, "B": 6.0})], [self.grp({"B": 5.9})]])
        ch = out[1][0]
        self.assertEqual([(c["broker"], c["note"], c["counts"]) for c in ch],
                         [("중개B", "통계 제외", False), ("중개A", "최저가 중개사 빠짐", False)])
        self.assertIn("articles/B", na.article_url(ch[1]))  # 빠진 A 대신 지금 최저가 매물로 연결
        self.assertEqual(na.change_counts(ch), "0")
        self.assertEqual(na.change_stats(ch), "상승 0 · 하락 0 (최저가 안 바뀐 2건 제외)")

    def test_2_composition_changes_recorded(self):
        _, out = self.run_days([[self.grp({"A": 5.8, "B": 6.0})], [self.grp({"B": 6.0})]])
        c = out[1][0]
        self.assertEqual([(x["note"], x["old_price"], x["price"], x["counts"]) for x in c],
                         [("최저가 중개사 빠짐", "5억 8,000만", "6억", False)])
        _, out = self.run_days([[self.grp({"A": 5.8, "B": 6.0})], [self.grp({"A": 5.8, "B": 6.0, "C": 5.6})]])
        self.assertEqual([(x["note"], x["broker"], x["price"]) for x in out[1][0]],
                         [("더 싼 중개사 추가", "중개C", "5억 6,000만")])

    def test_3_no_pairing_across_brokers(self):
        _, out = self.run_days([[self.grp({"A": 5.8, "B": 6.0})], [self.grp({"A": 5.8, "C": 5.7})]])
        self.assertEqual([(x["note"], x["broker"], x["old_price"]) for x in out[1][0]],
                         [("더 싼 중개사 추가", "중개C", "5억 8,000만")])
        # 같은 중개사가 새 번호로 다시 올리면 짝지어 가격 변동(최저가 바뀌면 통계에 셈)
        b = {"A": "가공인", "B": "나공인", "B2": "나공인"}
        _, out = self.run_days([[self.grp({"A": 6.0, "B": 5.9}, b)], [self.grp({"A": 6.0, "B2": 5.7}, b)]])
        self.assertEqual([(x["note"], x["old_price"], x["price"], x["counts"]) for x in out[1][0]],
                         [("재등록", "5억 9,000만", "5억 7,000만", True)])

    def test_4_prices_are_lowest(self):
        item = {"representativeArticleInfo": {"articleNumber": 1, "priceInfo": {"dealPrice": 600_000_000},
                                              "brokerInfo": {"brokerageName": "대표공인"}},
                "duplicatedArticleInfo": {"realtorCount": 2, "articleInfoList": [
                    {"articleNumber": 7, "priceInfo": {"dealPrice": 580_000_000},
                     "brokerInfo": {"brokerageName": "싼공인"}}]}}
        a = na.normalize_fin(item, "9")
        self.assertEqual((a["articleNo"], a["price"], a["link_no"], a["realtor"]),
                         ("1", "5억 8,000만", "7", "싼공인 외 1곳"))
        self.assertIn("articles/7", na.article_url(a))
        # 사라짐 줄도 최저가
        _, out = self.run_days([[self.grp({"A": 6.0, "B": 5.8})], [], []])
        self.assertEqual(out[2][1][0]["price"], "5억 8,000만")

    def test_5_merged_groups_not_double_counted(self):
        g1, g2 = self.grp({"X": 6.0}), self.grp({"Y": 6.0})
        merged = self.grp({"X": 6.0, "Y": 5.8})
        state, out = self.run_days([[g1, g2], [merged]])
        self.assertEqual(len(state["tracked"]), 1)
        self.assertEqual([x["broker"] for x in out[1][0]], ["중개Y"])

    def test_6_split_group_tracks_disappearance(self):
        state, out = self.run_days([[self.grp({"X": 6.0, "Y": 6.0})], [self.grp({"X": 6.0}), self.grp({"Y": 6.0})]])
        self.assertEqual(state["tracked"]["X"]["aliases"], [])  # 갈라지면 묶음 정보도 교체
        self.assertIn("Y", state["tracked"])
        for d in ("03", "04"):
            gone = na.track_listings(state, "1", [self.grp({"Y": 6.0})], f"2026-10-{d}")
        self.assertEqual([g["articleNo"] for g in gone], ["X"])  # X 가 팔리면 사라짐으로 잡힘

    def test_7_filtered_out_not_reported(self):
        big = lambda p: self.grp({"Z": p}, supply=112.0, exclusive=84.0)  # noqa: E731
        want = lambda a: na.matches_pyeong(a, [25])  # noqa: E731
        state, _ = self.run_days([[big(9.0)]], want)
        self.assertNotIn("Z", state["tracked"])
        # 조건을 좁히기 전부터 추적하던 매물: 변동·사라짐을 알리지 않음
        state = {"tracked": {}}
        na.track_listings(state, "1", [big(9.0)], "2026-10-01")
        ch = []
        na.track_listings(state, "1", [big(8.5)], "2026-10-02", want, ch)
        self.assertEqual(ch, [])
        for d in ("03", "04"):
            gone = na.track_listings(state, "1", [], f"2026-10-{d}", want)
        self.assertEqual(gone, [])

    def test_8_reappear_and_relist(self):
        g = self.grp({"A": 6.0})
        state, out = self.run_days([[g], [], [], [self.grp({"A": 5.9})]])
        self.assertEqual(len(out[2][1]), 1)                     # 3일째 사라짐
        self.assertEqual(out[3][2], {"A"})                     # 4일째 다시 올라옴 → 신규 아님
        self.assertEqual(state["tracked"]["A"]["first_seen"], "2026-10-01")
        self.assertEqual([(x["note"], x["counts"]) for x in out[3][0]], [("다시 올라옴", False)])
        # 중개사 한 곳이 같은 집을 새 번호로 다시 올림(끌어올리기) → 이어서 추적, 가격 변동으로
        b1, b2 = {"A": "가공인"}, {"A2": "가공인"}
        state, out = self.run_days([[self.grp({"A": 6.0}, b1)], [self.grp({"A2": 5.9}, b2)]])
        self.assertEqual(out[1][2], {"A2"})
        self.assertEqual([(x["note"], x["counts"], x["old_price"], x["price"]) for x in out[1][0]],
                         [("재등록", True, "6억", "5억 9,000만")])
        self.assertEqual(list(state["tracked"]), ["A"])

    def test_9_legacy_first_price_fixed_to_min(self):
        state = {"tracked": {"A": {"complexNo": "1", "rep": "A", "aliases": ["B"], "first_seen": "2026-09-29",
                                   "first_price": "6억", "first_price_won": 600_000_000,
                                   "member_prices": {"A": 600_000_000, "B": 580_000_000},
                                   "member_first": {"A": 600_000_000, "B": 580_000_000}, "missed": 0}}}
        g = self.grp({"A": 6.0, "B": 5.8})
        na.track_listings(state, "1", [g], "2026-10-01")
        info = na.tracked_info(state, g)
        self.assertEqual(na.diff_text(info["first_price_won"], info["price_won"]), "")

    def test_10_change_shows_current_range(self):
        _, out = self.run_days([[self.grp({"A": 5.8, "B": 6.0})], [self.grp({"A": 5.7, "B": 6.0, "C": 6.2})]])
        self.assertEqual(na.dup_text(out[1][0][0]), "중개사 3곳 · 5억 7,000만~6억 2,000만")


class ReviewScenario2Test(unittest.TestCase):
    """두 번째 검토에서 찾은 경우들."""
    grp = staticmethod(ReviewScenarioTest.grp)
    run_days = ReviewScenarioTest.run_days

    def one(self, no, price, broker="가공인", **kw):
        g = self.grp({no: price}, {no: broker}, **kw)
        g["realtor_count"] = 1
        return g

    def test_trade_type_must_match(self):
        state, out = self.run_days([[self.one("X", 6.0)], [], [self.one("Y", 5.9, trade="전세")]])
        self.assertEqual(out[2][2], set())            # 전세는 매매 재등록이 아님
        self.assertEqual([g["articleNo"] for g in out[2][1]], ["X"])

    def test_no_relist_when_floor_is_range(self):
        _, out = self.run_days([[self.one("X", 6.0, floor="중/29")], [self.one("Y", 5.9, floor="중/29")]])
        self.assertEqual(out[1][2], set())

    def test_no_relist_into_multi_broker_group(self):
        g = self.grp({"P": 5.0, "Q": 5.1}, {"P": "다공인", "Q": "가공인"})
        _, out = self.run_days([[self.one("X", 6.0)], [g]])
        self.assertEqual(out[1][2], set())

    def test_no_relist_when_ambiguous_or_price_far(self):
        _, out = self.run_days([[self.one("X1", 6.0), self.one("X2", 6.0)], [self.one("Y", 6.0)]])
        self.assertEqual(out[1][2], set())            # 후보가 둘이면 잇지 않음
        _, out = self.run_days([[self.one("X", 6.0)], [self.one("Y", 5.0)]])
        self.assertEqual(out[1][2], set())            # 가격 차이 10% 넘으면 다른 집으로

    def test_no_gone_and_relist_same_run(self):
        _, out = self.run_days([[self.one("X", 6.0)], [], [self.one("Y", 5.9)]])
        self.assertEqual(out[2][1], [])               # 같은 실행에서 이어지면 사라짐 아님
        self.assertEqual(out[2][2], {"Y"})
        self.assertEqual([x["note"] for x in out[2][0]], ["재등록"])

    def test_merge_without_fake_changes(self):
        state, out = self.run_days([[self.grp({"X": 6.0}), self.grp({"Y": 5.8})],
                                    [self.grp({"X": 6.0, "Y": 5.8})]])
        self.assertEqual(out[1][0], [])
        (t,) = state["tracked"].values()
        self.assertEqual([h for h in t["price_history"] if len(h) > 3], [])


class ReviewScenario3Test(unittest.TestCase):
    """세 번째 검토에서 찾은 경우들."""
    grp = staticmethod(ReviewScenarioTest.grp)
    run_days = ReviewScenarioTest.run_days
    one = ReviewScenario2Test.one

    def test_1_two_new_listings_one_old_record(self):
        _, out = self.run_days([[self.one("X", 6.0)], [], [self.one("T1", 6.0), self.one("T2", 6.05)]])
        self.assertEqual(out[2][2], set())             # 둘 다 신규 (어느 쪽도 X 에 붙이지 않음)
        self.assertEqual([g["articleNo"] for g in out[2][1]], ["X"])

    def test_2_ambiguous_across_tracked_and_gone(self):
        state, out = self.run_days([[self.one("R", 6.0)], [], [], [self.one("T1", 6.2), self.one("T2", 6.2)],
                                    [self.one("Y", 6.3)]])
        self.assertEqual(out[4][2], set())             # R(사라짐)·T1·T2(안 보임) 후보가 여럿이면 잇지 않음

    def test_3_direction_must_match(self):
        _, out = self.run_days([[self.one("A", 6.0, direction="남향")], [self.one("B", 6.1, direction="동향")]])
        self.assertEqual(out[1][2], set())
        _, out = self.run_days([[self.one("A", 6.0, direction="남향")], [self.one("B", 6.1, direction="남향")]])
        self.assertEqual(out[1][2], {"B"})

    def test_4_old_record_multi_broker_by_count(self):
        x = self.one("X", 6.0)
        x["realtor_count"] = 2                          # 가격 아는 중개사는 한 곳이지만 실제로는 두 곳
        _, out = self.run_days([[x], [self.one("Y", 5.9)]])
        self.assertEqual(out[1][2], set())

    def test_5_relist_after_gone_is_counted(self):
        _, out = self.run_days([[self.one("X", 6.0)], [], [], [self.one("Y", 5.9)]])
        self.assertEqual([(c["note"], c["counts"]) for c in out[3][0]], [("재등록", True)])

    def test_6_merge_record_without_member_prices(self):
        state = {"tracked": {
            "X": {"complexNo": "1", "rep": "X", "aliases": [], "price_won": 600_000_000, "missed": 0,
                  "member_prices": {"X": 600_000_000}, "first_seen": "2026-10-01"},
            "Y": {"complexNo": "1", "rep": "Y", "aliases": [], "price_won": 580_000_000, "missed": 0,
                  "first_seen": "2026-10-01"}}}
        ch = []
        na.track_listings(state, "1", [self.grp({"X": 6.0, "Y": 5.8})], "2026-10-02", changes=ch)
        self.assertEqual(ch, [])                        # 가짜 '더 싼 중개사 추가' 없음
        self.assertEqual(len(state["tracked"]), 1)


class CompositionWordingTest(unittest.TestCase):
    def test_left_and_added_wording(self):
        _, out = ReviewScenarioTest.run_days(ReviewScenarioTest, [
            [ReviewScenarioTest.grp({"A": 5.1, "B": 5.3}, {"A": "양정공인", "B": "나공인"})],
            [ReviewScenarioTest.grp({"B": 5.3}, {"B": "나공인"})]])
        c = out[1][0][0]
        self.assertEqual(na.move_text(c), "양정공인 빠짐 · 최저가 5억 1,000만 → 5억 3,000만")
        self.assertEqual(na.note_text(c), "")
        r = {"no": "1", "name": "A", "status": "ok", "listings": [], "new": [], "changes": [c], "gone": [],
             "week": [{"at": "2026-10-02T10:00+09:00", "kind": "change", "item": c}]}
        _, text, body = na.format_briefing([r], "25평", datetime(2026, 10, 2, 10, tzinfo=na.KST))
        self.assertIn("[매매] 양정공인 빠짐 · 최저가 5억 1,000만 → 5억 3,000만 (▲2,000만) ·", text)
        self.assertNotIn("(최저가 중개사 빠짐)", body)
        self.assertEqual(na.move_text({"broker": "다공인", "old_price": "6억", "price": "5억 6,000만",
                                       "note": "더 싼 중개사 추가"}), "다공인 추가 · 최저가 6억 → 5억 6,000만")
        hist = [["2026-10-01", "5억 1,000만", 510_000_000],
                ["2026-10-02", "5억 3,000만", 530_000_000, "5억 1,000만", 510_000_000, "양정공인", "최저가 중개사 빠짐"]]
        self.assertEqual(na.recent_price_changes({"price_history": hist}),
                         ["10/02 양정공인 빠짐 · 최저가 5억 1,000만 → 5억 3,000만 ▲2,000만"])


class UnitTypeTest(unittest.TestCase):
    def test_unit_type_shown(self):
        item = {"representativeArticleInfo": {"articleNumber": 1, "priceInfo": {"dealPrice": 600_000_000},
                                              "spaceInfo": {"supplySpace": 86.23, "exclusiveSpace": 59.78,
                                                            "nameType": "A"}}}
        a = na.normalize_fin(item, "9")
        self.assertEqual(a["unit_type"], "A")
        self.assertIn("26평 A타입", na.where_text(a))
        self.assertEqual(na.type_text({"unit_type": "59B타입"}), "59B타입")
        self.assertEqual(na.type_text({}), "")
        r = {"no": "9", "name": "X", "status": "ok", "new": [], "changes": [], "gone": [],
             "listings": [dict(a, trade="매매")]}
        _, text, body = na.format_briefing([r], "25평", datetime(2026, 10, 3, tzinfo=na.KST))
        self.assertIn("26평 A타입", text)
        self.assertIn(">A</span>", body)
        import csv as _csv
        rows = list(_csv.DictReader(na.listings_csv([r]).decode("utf-8-sig").splitlines()))
        self.assertEqual(rows[0]["타입"], "A")
