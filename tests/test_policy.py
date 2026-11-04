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

    def test_mention_is_not_action(self):
        """标题里没有管控名词、正文只是"提到"某个词——不算政策。"""
        title = ("'The point was to dehumanise': London Holocaust archive examines "
                 "world's first 'viral' antisemitism scandal")
        body = ("Historians say the archive documents how censorship and lockdown "
                "rhetoric spread online; the museum examines its legacy.")
        self.assertIsNone(policy.classify(title + " " + body, title))

    def test_control_needs_a_verb(self):
        """管控名词在标题里但没动词（"讨论/研究"）——不成立。"""
        title = "Report studies censorship in wartime archives"
        self.assertIsNone(policy.classify(title, title))
        title2 = "EU imposes travel ban on officials"
        self.assertEqual(policy.classify(title2, title2), "control")


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


class TestRealFalsePositives(unittest.TestCase):
    """线上 v1 的三条误判，逐条钉成回归测试。"""

    def test_anti_corruption_investigation_is_not_policy(self):
        self.assertIsNone(policy.classify("湖南省政协经济科技委员会原副主任张建军接受审查调查"))

    def test_prosecution_is_not_policy(self):
        self.assertIsNone(policy.classify("检察机关依法对杨宏勇涉嫌受贿、利用影响力受贿案提起公诉"))

    def test_monetary_policy_is_not_social_policy(self):
        self.assertIsNone(policy.classify("菲律宾央行：货币政策仍处紧缩周期 通胀预计数月后见顶"))

    def test_policy_word_alone_is_not_enough(self):
        # 有"政策"两个字，但没有任何强制性，仍然不算
        self.assertIsNone(policy.classify("专家解读产业政策走向"))


class TestNotable(unittest.TestCase):
    """个案不剔除，但也不归类：它只是客观事件。"""

    def test_corruption_case_goes_to_notable(self):
        out = policy.scan([_cl("检察机关依法对杨宏勇涉嫌受贿案提起公诉")], date(2026, 11, 3))
        self.assertEqual(out["items"], [])
        self.assertEqual(len(out["notable"]), 1)
        self.assertEqual(out["notable"][0]["kind"], "judicial")
        self.assertEqual(out["counts"]["notable"], 1)

    def test_monetary_is_dropped_entirely(self):
        out = policy.scan([_cl("菲律宾央行：货币政策仍处紧缩周期")], date(2026, 11, 3))
        self.assertEqual(out["items"], [])
        self.assertEqual(out["notable"], [])

    def test_notable_not_mixed_into_typed_items(self):
        cls = [_cl("英国强制所有公民缴纳社保，违者重罚"),
               _cl("检察机关对某官员涉嫌受贿提起公诉")]
        out = policy.scan(cls, date(2026, 11, 3))
        self.assertEqual([i["type"] for i in out["items"]], ["extractive"])
        self.assertEqual(len(out["notable"]), 1)


if __name__ == "__main__":
    unittest.main()
