"""物理征兆：空域份额。

这里最容易错的地方是"把昼夜当成战争"——所以只测两件事：
份额算得对、份额塌下去时压力是往上的。
"""

import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import physical          # noqa: E402


def _st(lat, lon):
    return ["abc123", "CALL01 ", "Nowhere", None, None, lon, lat, None, False, None]


class TestShares(unittest.TestCase):
    def test_region_share_of_world(self):
        # 4 架飞机：3 架在欧洲框内，1 架在亚洲
        states = [
            _st(50.0, 10.0), _st(48.0, 2.0), _st(52.0, 20.0),
            _st(35.0, 120.0),
        ]
        sh = physical.shares(states)
        self.assertAlmostEqual(sh["EUROPE"], 0.75, places=6)
        self.assertAlmostEqual(sh["EAST_ASIA"], 0.25, places=6)
        self.assertAlmostEqual(sh["AFRICA"], 0.0, places=6)

    def test_rows_without_position_are_ignored(self):
        states = [_st(50.0, 10.0), ["x", "no-pos", "", None, None, None, None, None],
                  "not-a-row"]
        sh = physical.shares(states)
        self.assertAlmostEqual(sh["EUROPE"], 1.0, places=6)

    def test_empty_is_empty_not_zero(self):
        # 全球一架都没拍到 = 数据缺失，不是"空域全关"
        self.assertEqual(physical.shares([]), {})


class TestSeries(unittest.TestCase):
    def test_share_is_negated_so_a_collapse_raises_pressure(self):
        day = date(2026, 11, 3)
        s = physical.series_from({"EUROPE": 0.3}, day)
        self.assertAlmostEqual(s[("EUROPE", "physical")][day], -30.0, places=6)

    def test_fetch_failure_returns_empty_series(self):
        with mock.patch.object(physical, "fetch_states", side_effect=OSError("boom")):
            frag, health = physical.fetch_day(date(2026, 11, 3))
        self.assertEqual(frag, {})
        self.assertFalse(health["ok"])
        self.assertIn("boom", health["error"])

    def test_fetch_ok(self):
        with mock.patch.object(physical, "fetch_states",
                               return_value=[_st(50.0, 10.0), _st(35.0, 120.0)]):
            frag, health = physical.fetch_day(date(2026, 11, 3))
        self.assertTrue(health["ok"])
        self.assertEqual(health["items"], 2)
        self.assertIn(("EUROPE", "physical"), frag)


if __name__ == "__main__":
    unittest.main()
