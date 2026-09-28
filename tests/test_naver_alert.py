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
        a = na.normalize_mobile({"atclNo": "1", "spc1": "83", "spc2": "59.97"}, "9")
        self.assertTrue(na.matches_pyeong(a, [25]))
        a = na.normalize_new({"articleNo": 1, "area1": 112, "area2": 84}, "9")
        self.assertFalse(na.matches_pyeong(a, [25]))


class ParseTest(unittest.TestCase):
    def test_keyword_matches_both_complexes(self):
        for name in ("서면아이파크1단지", "서면 아이파크 2단지", "서면아이파크"):
            self.assertTrue(na.matches_keyword(name, "서면아이파크"), name)
        self.assertFalse(na.matches_keyword("서면롯데캐슬", "서면아이파크"))

    def test_normalize_new(self):
        a = na.normalize_new({"articleNo": 123, "tradeTypeName": "월세",
                              "dealOrWarrantPrc": "5,000", "rentPrc": "150"}, "9")
        self.assertEqual(a["articleNo"], "123")
        self.assertEqual(a["price"], "5,000/150")
        self.assertIn("articleNo=123", na.article_url(a))

    def test_normalize_mobile(self):
        a = na.normalize_mobile({"atclNo": "456", "tradTpNm": "전세", "prcInfo": "5억"}, "9")
        self.assertEqual((a["articleNo"], a["price"]), ("456", "5억"))

    def test_format_message(self):
        title, body = na.format_message("서면아이파크", [art(1, "7")], {"7": "서면아이파크2단지"}, [25])
        self.assertIn("25평 새 매물 1건", title)
        self.assertIn("25평 (83/59㎡)", body)
        self.assertIn("서면아이파크2단지", body)


if __name__ == "__main__":
    unittest.main()
