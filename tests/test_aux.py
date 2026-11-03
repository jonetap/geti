"""灾害层、疫情层、末日指数的单元测试。

这里只测"能不能把原始数据正确变成数"，以及"缺数据时会不会假装有数据"。
不测真实网络——那部分只能在 CI 上验证。
"""
import json
import unittest
from datetime import date, timedelta

from pipeline import composite, epidemic, hazard


class TestHazardParsers(unittest.TestCase):
    def test_energy_prefers_big_quake(self):
        """"一次 7 级"必须压过"十次 5 级"，否则只会在数次数。"""
        self.assertGreater(hazard.energy_of([7.0]), hazard.energy_of([5.0] * 10))

    def test_parse_usgs(self):
        doc = {"features": [
            {"properties": {"mag": 6.4}},
            {"properties": {"mag": 4.8}},
            {"properties": {"mag": None}},
        ]}
        q = hazard.parse_usgs(json.dumps(doc))
        self.assertEqual(q["count"], 2)
        self.assertEqual(q["m6"], 1)
        self.assertEqual(q["max"], 6.4)
        self.assertGreater(q["energy"], 0)

    def test_parse_usgs_bad_json(self):
        self.assertEqual(hazard.parse_usgs("not json"), {})

    def test_parse_eonet_counts_categories(self):
        doc = {"events": [
            {"categories": [{"title": "Wildfires"}]},
            {"categories": [{"title": "Wildfires"}]},
            {"categories": [{"title": "Volcanoes"}]},
        ]}
        e = hazard.parse_eonet(json.dumps(doc))
        self.assertEqual(e["total"], 3)
        self.assertEqual(e["by_cat"]["wildfires"], 2)

    def test_parse_gdacs_levels(self):
        xml = """<rss xmlns:gdacs="http://www.gdacs.org"><channel>
        <item><gdacs:alertlevel>Red</gdacs:alertlevel></item>
        <item><alertlevel>orange</alertlevel></item>
        <item><alertlevel>green</alertlevel></item>
        </channel></rss>"""
        g = hazard.parse_gdacs(xml)
        self.assertEqual((g["red"], g["orange"], g["green"]), (1, 1, 1))

    def test_parse_kp_two_formats(self):
        products = json.dumps([["time_tag", "Kp"], ["2026-11-02 00:00", "3.33"],
                               ["2026-11-02 03:00", "5.67"]])
        self.assertAlmostEqual(hazard.parse_kp(products)["max"], 5.67)
        rows = json.dumps([{"kp_index": 2.0}, {"kp_index": 4.0}])
        self.assertAlmostEqual(hazard.parse_kp(rows)["max"], 4.0)


class TestEpidemicParsers(unittest.TestCase):
    def test_disease_regex_ignores_generic_words(self):
        self.assertTrue(epidemic.DISEASE.search("H5N1 detected in dairy herd"))
        self.assertTrue(epidemic.DISEASE.search("霍乱疫情在北部蔓延"))
        # "后疫情时代"不该被当成新增疫情
        self.assertFalse(epidemic.DISEASE.search("后疫情时代消费降级"))

    def test_policy_regex(self):
        self.assertTrue(epidemic.POLICY.search("WHO declares PHEIC"))
        self.assertTrue(epidemic.POLICY.search("边境关闭与旅行限制"))
        self.assertFalse(epidemic.POLICY.search("卫生部长发表讲话"))

    def test_rdf_feed_titles(self):
        xml = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/">
        <item><title>Cholera outbreak in Yemen</title></item>
        </rdf:RDF>"""
        self.assertEqual(epidemic.parse_feed_titles(xml), ["Cholera outbreak in Yemen"])


class TestComposite(unittest.TestCase):
    def _series(self, kind, comp, values, end):
        out = {}
        for i, v in enumerate(values):
            d = end - timedelta(days=len(values) - i)
            out.setdefault((kind, comp), {})[d] = v
        return out

    def test_subindex_needs_history(self):
        end = date(2026, 11, 2)
        series = {(("hazard"), "quake_energy"): {end: 18.0}}
        sub = composite.subindex(series, "hazard", end)
        self.assertIsNone(sub["index"])
        self.assertEqual(sub["components_ok"], 0)

    def test_subindex_spike_raises_index(self):
        end = date(2026, 11, 2)
        series = self._series("hazard", "quake_energy", [15.0] * 16, end)
        series[("hazard", "quake_energy")][end] = 18.5
        sub = composite.subindex(series, "hazard", end)
        self.assertIsNotNone(sub["index"])
        self.assertGreater(sub["index"], 50.0)

    def test_subindex_flat_is_neutral(self):
        end = date(2026, 11, 2)
        series = self._series("hazard", "quake_energy", [15.0] * 16, end)
        series[("hazard", "quake_energy")][end] = 15.0
        sub = composite.subindex(series, "hazard", end)
        self.assertAlmostEqual(sub["index"], 50.0, places=1)

    def test_doom_partial_when_missing(self):
        d = composite.doom(68.3, None, None)
        self.assertTrue(d["partial"])
        self.assertEqual(d["available"], ["military"])
        self.assertIsNotNone(d["index"])

    def test_doom_resonance_beats_mean(self):
        """两个子系统同时冲高，必须比三个都温和更高（多重危机加成）。"""
        calm_both = composite.doom(55.0, 55.0, 55.0)
        hot_two = composite.doom(80.0, 82.0, 50.0)
        self.assertGreater(hot_two["index"], calm_both["index"])
        self.assertEqual(hot_two["resonance"], 2)

    def test_doom_all_missing(self):
        self.assertIsNone(composite.doom(None, None, None)["index"])


if __name__ == "__main__":
    unittest.main()
