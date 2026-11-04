"""GDELT 2.0 实时窗口：去重与回退。

这两个行为决定了首页到底是不是"实时"：
- 不去重 → 同一事件在 36 小时里重复几十次，指数顶死
- 没回退 → lastupdate 一挂，整个 run 直接 returned 2
"""

import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import fetch                      # noqa: E402
from pipeline.build import VOLUME_KEY, pressure_from_signals   # noqa: E402
from pipeline.model import EventSignal          # noqa: E402
from pipeline.parse import gdelt_rows           # noqa: E402


def _row(eid: str, code: str, mentions: int, fips: str = "CH") -> str:
    cols = ["-"] * 61
    cols[0] = eid
    cols[1] = "20261102"
    cols[26] = code
    cols[30] = "-8.0"
    cols[31] = str(mentions)
    cols[53] = fips
    cols[60] = "https://example.org/x"
    return "\t".join(cols)


class TestEventId(unittest.TestCase):
    def test_rows_carry_global_event_id(self):
        rows = gdelt_rows(_row("1234567", "190", 4), min_mentions=1, min_severity=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].eid, "1234567")

    def test_same_event_counted_once(self):
        # 同一条空袭在不同窗口里出现，报道数从 1 涨到 9
        a = EventSignal(day=date(2026, 11, 2), fips="CH", region="EAST_ASIA",
                        cameo="190", component="military", severity=5,
                        goldstein=-8.0, mentions=9, eid="999")
        b = EventSignal(day=date(2026, 11, 2), fips="CH", region="EAST_ASIA",
                        cameo="190", component="military", severity=5,
                        goldstein=-8.0, mentions=1, eid="999")
        s = pressure_from_signals([b, a])
        cell = s[("EAST_ASIA", "military")]
        # 只按 mentions=9 那条算一次；如果两条相加，值会明显更大
        self.assertAlmostEqual(cell[date(2026, 11, 2)],
                               1.0 + 0.15 * __import__("math").log1p(9), places=6)
        self.assertEqual(s[VOLUME_KEY][date(2026, 11, 2)], 1.0)

    def test_distinct_events_both_count(self):
        a = EventSignal(day=date(2026, 11, 2), fips="CH", region="EAST_ASIA",
                        cameo="190", component="military", severity=5,
                        goldstein=-8.0, mentions=1, eid="1")
        b = EventSignal(day=date(2026, 11, 2), fips="CH", region="EAST_ASIA",
                        cameo="190", component="military", severity=5,
                        goldstein=-8.0, mentions=1, eid="2")
        s = pressure_from_signals([a, b])
        self.assertEqual(s[VOLUME_KEY][date(2026, 11, 2)], 2.0)


class TestRecentWindows(unittest.TestCase):
    def test_no_lastupdate_returns_none(self):
        with mock.patch.object(fetch, "gdelt_urls", return_value=[]):
            signals, health, resolved = fetch.fetch_gdelt_recent(hours=1)
        self.assertEqual(signals, [])
        self.assertIsNone(resolved)
        self.assertFalse(health[0]["ok"])
        self.assertIn("lastupdate", health[0]["error"])

    def test_parses_windows_and_reports_latest_day(self):
        blob = (_row("1", "190", 3) + "\n").encode("utf-8")

        def fake_get(url, timeout=40):
            return blob

        with mock.patch.object(fetch, "gdelt_urls", return_value=["u1", "u2"]), \
             mock.patch.object(fetch, "http_get", side_effect=fake_get), \
             mock.patch.object(fetch, "gdelt_from_zip",
                               side_effect=lambda data, kr=None, **kw: gdelt_rows(
                                   data.decode("utf-8"), min_mentions=1, min_severity=1)):
            signals, health, resolved = fetch.fetch_gdelt_recent(hours=1, workers=2)
        self.assertTrue(health[0]["ok"])
        self.assertEqual(health[0]["files"], 2)
        self.assertEqual(resolved, date(2026, 11, 2))
        self.assertEqual(len(signals), 2)   # 两个窗口各一条，去重发生在 build

    def test_all_windows_fail_is_not_ok(self):
        with mock.patch.object(fetch, "gdelt_urls", return_value=["u1"]), \
             mock.patch.object(fetch, "http_get", side_effect=OSError("boom")):
            signals, health, resolved = fetch.fetch_gdelt_recent(hours=1)
        self.assertEqual(signals, [])
        self.assertIsNone(resolved)
        self.assertFalse(health[0]["ok"])


if __name__ == "__main__":
    unittest.main()
