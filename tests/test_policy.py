"""政策脉冲：分类、符号、汇总。

最关键的一条：**再分配型必须是负号**。如果"政府发钱"和"政府强制扣钱"
都算压力上升，这个指标就只是在测"政府有没有说话"，不是在测"社会紧不紧"。
"""

import os
import sys
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import policy          # noqa: E402

UTC = timezone.utc


def _cl(title, body="", url="https://example.org/a", published=None, origins=2):
    return SimpleNamespace(title=title, body=body, url=url,
                           published=published or datetime(2026, 11, 3, 9, tzinfo=UTC),
                           n_origin=origins)


class TestClassify(unittest.TestCase):
    def test_uk_forced_social_insurance(self):
        t = ("英国通过新社保政策，强迫所有英国公民强制缴纳大额社保与保险费用，"
             "无社保将视为违法")
        self.assertEqual(policy.classify(t), "extractive")
        self.assertGreater(policy.intensity(t, "extractive"), 0.8)

    def test_curfew_is_control(self):
        self.assertEqual(policy.classify("Ukraine imposes nationwide curfew and mobilisation order"),
                         "control")

    def test_subsidy_is_redistributive_not_extractive(self):
        # "取消补贴"是压力，"发放补贴"是缓和——两个词都含"补贴"，词序决定符号
        self.assertEqual(policy.classify("Government to boost fuel subsidy for all households"),
                         "redistributive")
        self.assertEqual(policy.classify("Government to cut fuel subsidy"),
                         "extractive")

    def test_procedural_is_dropped(self):
        self.assertEqual(policy.classify("Pension bill passes first reading in parliament"), None)

    def test_plain_news_is_not_policy(self):
        self.assertEqual(policy.classify("Earthquake strikes northern Japan"), None)


class TestScan(unittest.TestCase):
    def test_signed_pressure_by_country(self):
        cls = [
            _cl("英国强制所有公民缴纳社保，违者重罚"),
            _cl("Germany to boost child benefit subsidy for all families"),
        ]
        out = policy.scan(cls, date(2026, 11, 3))
        self.assertGreater(out["pressure"]["GB"], 0)
        self.assertLess(out["pressure"]["DE"], 0)      # 发钱把压力往下拉
        self.assertEqual(out["counts"]["extractive"], 1)
        self.assertEqual(out["mode"], "shadow")

    def test_no_country_still_listed(self):
        out = policy.scan([_cl("Mandatory insurance scheme approved, no country mentioned")],
                          date(2026, 11, 3))
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["country"], "")
        self.assertEqual(out["pressure"], {})

    def test_items_sorted_by_magnitude(self):
        cls = [_cl("curfew imposed"), _cl("英国强制所有公民缴纳社保，违者重罚")]
        out = policy.scan(cls, date(2026, 11, 3))
        self.assertGreaterEqual(abs(out["items"][0]["signed"]), abs(out["items"][-1]["signed"]))


if __name__ == "__main__":
    unittest.main()
