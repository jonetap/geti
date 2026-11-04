"""离线测试：不联网，全部用合成/构造数据。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    backtest, build, cluster, epidemic, hazard, market, score, store,
)          # noqa: E402
from pipeline.parse import gdelt_rows, parse_feed          # noqa: E402
from pipeline.synth import synth_items, synth_signals      # noqa: E402

UTC = timezone.utc


def _gdelt_row(**over) -> str:
    cols = [""] * 61
    cols[0] = "123456789"
    cols[1] = "20261103"
    cols[6] = "RUSSIA"
    cols[16] = "UKRAINE"
    cols[26] = "190"
    cols[30] = "-9.0"
    cols[31] = "25"
    cols[53] = "UP"
    cols[60] = "https://example.invalid/a"
    for k, v in over.items():
        cols[int(k[1:])] = v
    return "\t".join(cols)


class TestParse(unittest.TestCase):
    def test_gdelt_row(self) -> None:
        sigs = gdelt_rows(_gdelt_row())
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s.component, "military")
        self.assertEqual(s.severity, 5)
        self.assertEqual(s.region, "RUSSIA_CIS")
        self.assertEqual(s.mentions, 25)
        self.assertEqual(s.actors, "RUSSIA → UKRAINE")

    def test_gdelt_short_row_skipped(self) -> None:
        self.assertEqual(gdelt_rows("a\tb\tc"), [])

    def test_rss(self) -> None:
        xml = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Missile strike reported near border</title>
        <link>https://x.invalid/1</link>
        <description>&lt;p&gt;Details&lt;/p&gt;</description>
        <pubDate>Mon, 03 Nov 2026 08:00:00 GMT</pubDate></item>
        <item><title>Talks resume</title><link>https://x.invalid/2</link></item>
        </channel></rss>"""
        items = parse_feed(xml, "bbc", now=datetime(2026, 11, 3, 12, tzinfo=UTC))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Missile strike reported near border")
        self.assertEqual(items[0].published.hour, 8)
        self.assertEqual(items[0].source_id, "bbc")

    def test_atom(self) -> None:
        xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Border shelling</title>
        <link rel="alternate" href="https://y.invalid/1"/>
        <updated>2026-11-03T07:30:00Z</updated></entry></feed>"""
        items = parse_feed(xml, "dw")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://y.invalid/1")
        self.assertEqual(items[0].published.hour, 7)

    def test_rss1_rdf(self) -> None:
        """RSS 1.0 的 item 在默认命名空间里，且时间用 dc:date。线上 DW 就是这么挂的。"""
        xml = """<?xml version="1.0"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                 xmlns="http://purl.org/rss/1.0/"
                 xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel rdf:about="https://z.invalid/"><title>World</title></channel>
          <item rdf:about="https://z.invalid/1">
            <title>Border shelling kills two</title>
            <link>https://z.invalid/1</link>
            <description>Troops clashed near the frontier.</description>
            <dc:date>2026-11-02T10:00:00Z</dc:date>
          </item>
        </rdf:RDF>"""
        items = parse_feed(xml, "dw")
        self.assertEqual(len(items), 1, "RSS 1.0 不许读成 0 条")
        self.assertEqual(items[0].url, "https://z.invalid/1")
        self.assertEqual(items[0].published.day, 2)
        self.assertEqual(items[0].published.hour, 10)


class TestCluster(unittest.TestCase):
    def test_same_event_merges(self) -> None:
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        items = [
            cluster.RawItem("reuters", "Missile strike near the border kills 12", "", "u1", now),
            cluster.RawItem("ap", "Missile strike near border kills 12", "", "u2", now),
            cluster.RawItem("bbc", "Missile strike near the border kills 12 people", "", "u3", now),
            cluster.RawItem("nhk", "Central bank raises interest rates", "", "u4", now),
        ]
        cs = cluster.cluster_items(items)
        big = max(cs, key=lambda c: c.n_origin)
        self.assertEqual(big.n_origin, 3, "三家转载同一事件必须归并为 3 个起源")
        self.assertNotIn("nhk", big.origins)

    def test_reprint_counts_once(self) -> None:
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        same = "Border crossing closed after shelling"
        items = [cluster.RawItem("reuters", same, "", f"u{i}", now) for i in range(4)]
        cs = cluster.cluster_items(items)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].n_origin, 1, "同一来源的 4 条重复只算 1 个起源")


class TestScore(unittest.TestCase):
    def test_confidence_monotonic(self) -> None:
        one = score.confidence([(0.9, 1.0)])
        three = score.confidence([(0.8, 1.0)] * 3)
        weak = score.confidence([(0.2, 1.0)] * 5)
        mediocre = score.confidence([(0.8, 1.0)])
        # 单源乘 0.8 的"不可全信"折扣
        self.assertAlmostEqual(one, 0.72, places=3)
        self.assertGreater(three, 0.95)
        self.assertGreater(weak, mediocre, "五个独立小道源汇聚应压过单一中等源")
        self.assertLess(weak, one, "但压不过一个高可信单源")

    def test_decay(self) -> None:
        self.assertEqual(score.recency_decay(1), 1.0)
        self.assertEqual(score.recency_decay(12), score.DECAY_6H)
        self.assertEqual(score.recency_decay(48), score.DECAY_24H)

    def test_robust_z(self) -> None:
        hist = [10.0, 11.0, 9.0, 10.5, 9.5, 10.0, 11.0, 9.0, 10.0, 10.2, 9.8, 10.1, 9.9, 10.0]
        z = score.robust_z(20.0, hist)
        self.assertIsNotNone(z)
        self.assertGreater(z, 1.9, "远超历史仍然要给强信号")
        self.assertLess(z, score.Z_SOFT, "软压缩不许触顶——触顶就意味着区分度丢失")
        self.assertIsNone(score.robust_z(20.0, hist[:5]), "历史不足必须返回 None")

    def test_flat_history_spike_is_max_surprise(self) -> None:
        flat = [0.0] * 20
        self.assertAlmostEqual(score.robust_z(0.0, flat), 0.0)
        self.assertAlmostEqual(score.robust_z(5.0, flat), score.Z_SOFT, places=3)

    def test_saturation_never_piles_up(self) -> None:
        """一批性质完全不同的历史，应给出不同强度的 z，而不是全撞上限。"""
        base = [1.0, 2.0, 1.5, 1.2, 1.8, 1.4, 1.1, 1.9, 1.3, 1.6, 1.7, 1.0, 1.5, 1.4]
        zs = [score.robust_z(base[0] * k, base) for k in (2.0, 4.0, 8.0)]
        self.assertEqual(len({round(z, 2) for z in zs}), 3, f"应当互不相同：{zs}")
        self.assertLess(zs[0], zs[1])
        self.assertLess(zs[1], zs[2])

    def test_strike_is_military_not_internal(self) -> None:
        """英文 strike 两个意思：军事打击 / 罢工。线上把空袭判成了内部动荡。"""
        self.assertEqual(
            score.component_from_text("US conducts series of ‘defensive strikes’ against Iran"),
            "military",
        )
        self.assertEqual(score.component_from_text("公交司机罢工进入第三天"), "internal")
        self.assertEqual(score.component_from_text("Nationwide walkout over pensions"), "internal")

    def test_index_neutral_at_50(self) -> None:
        self.assertAlmostEqual(score.to_index(0.0), 50.0)

    def test_index_saturates(self) -> None:
        self.assertLess(score.to_index(10.0), 100.0)
        self.assertGreater(score.to_index(10.0), 95.0)

    def test_reputation_learns(self) -> None:
        self.assertLess(score.update_reputation(0, 0), 0.5, "初始应偏悲观")
        self.assertGreater(score.update_reputation(20, 2), 0.80)
        self.assertGreater(score.update_reputation(20, 2), score.update_reputation(2, 20))


class TestBuild(unittest.TestCase):
    def _run(self, days: int, spike: int | None) -> dict:
        end = date(2026, 11, 3)
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        sig = synth_signals(days=days, end=end, spike_day=spike)
        items = synth_items(days=2, end=now)
        scored = build.score_clusters(cluster.cluster_items(items), now=now)
        series = build.merge_series(
            build.pressure_from_signals(sig),
            build.pressure_from_clusters(scored),
        )
        span = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
        cov = {"sources_ok": 16, "sources_failed": 2, "items": len(sig) + len(items)}
        return build.build_snapshot(series, end, span, scored, cov, now=now)

    def test_snapshot_schema(self) -> None:
        snap = self._run(60, None)
        for k in ("index_version", "date", "geti", "regions", "components",
                  "events", "breaking", "coverage", "series"):
            self.assertIn(k, snap)
        self.assertGreaterEqual(snap["geti"], 0.0)
        self.assertLessEqual(snap["geti"], 100.0)
        self.assertEqual(len(snap["series"]), 60)
        self.assertNotIn("OTHER", [r["code"] for r in snap["regions"]][:1])

    def test_spike_raises_index(self) -> None:
        calm = self._run(60, None)["geti"]
        hot = self._run(60, 0)["geti"]
        self.assertGreater(hot, calm, "最后一天的尖峰必须抬高指数")

    def test_constant_series_is_neutral(self) -> None:
        d0 = date(2026, 9, 1)
        series = {}
        for i in range(40):
            d = d0 + timedelta(days=i)
            for r in ("MIDDLE_EAST", "EUROPE"):
                for c in ("military", "diplomacy"):
                    series.setdefault((r, c), {})[d] = 5.0
        days = [d0 + timedelta(days=i) for i in range(40)]
        snap = build.build_snapshot(series, days[-1], days, [],
                                    {"sources_ok": 10, "sources_failed": 0, "items": 500})
        self.assertAlmostEqual(snap["geti"], 50.0, delta=1.0)

    def test_insufficient_history_refuses_z(self) -> None:
        d0 = date(2026, 11, 1)
        series = {("EUROPE", "military"): {d0: 100.0, d0 + timedelta(days=1): 100.0}}
        days = [d0, d0 + timedelta(days=1)]
        snap = build.build_snapshot(series, days[-1], days, [],
                                    {"sources_ok": 1, "sources_failed": 0, "items": 3})
        self.assertEqual(snap["regions"], [], "两天历史不足以产生区域分")
        self.assertAlmostEqual(snap["geti"], 50.0, delta=1.0)


class TestStore(unittest.TestCase):
    def test_idempotent_roundtrip(self) -> None:
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_store")
        os.makedirs(tmp, exist_ok=True)
        path = os.path.join(tmp, "series.jsonl")
        try:
            d = date(2026, 11, 3)
            s = {("EUROPE", "military"): {d: 3.5}}
            store.save_series(path, s, end=d)
            store.save_series(path, s, end=d)
            with open(path, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln]
            self.assertEqual(len(lines), 1, "重复写入同一天必须只有一行")
            back = store.load_series(path)
            self.assertAlmostEqual(back[("EUROPE", "military")][d], 3.5)
        finally:
            for name in os.listdir(tmp):
                os.remove(os.path.join(tmp, name))
            os.rmdir(tmp)

    def test_keep_existing_freezes_past_days(self) -> None:
        """已定格的日期不许被后来的重算改写——否则历史会自己抖动。"""
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_store2")
        os.makedirs(tmp, exist_ok=True)
        path = os.path.join(tmp, "series.jsonl")
        d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
        try:
            store.save_series(path, {("EUROPE", "military"): {d1: 3.0, d2: 4.0}}, end=d2)
            store.save_series(path, {("EUROPE", "military"): {d1: 99.0, d2: 98.0}}, end=d2,
                              keep_existing=True)
            back = store.load_series(path)
            self.assertAlmostEqual(back[("EUROPE", "military")][d1], 3.0)
            self.assertAlmostEqual(back[("EUROPE", "military")][d2], 4.0)
            # 新日期仍然可以写入
            d3 = date(2026, 11, 3)
            store.save_series(path, {("EUROPE", "military"): {d3: 5.0}}, end=d3,
                              keep_existing=True)
            self.assertAlmostEqual(store.load_series(path)[("EUROPE", "military")][d3], 5.0)
        finally:
            for name in os.listdir(tmp):
                os.remove(os.path.join(tmp, name))
            os.rmdir(tmp)


def _gdelt_row10(**over) -> str:
    """GDELT 1.0 日文件：58 列，Geo_CC=51 / SOURCEURL=57。"""
    cols = [""] * 58
    cols[1] = "20261103"
    cols[6] = "RUSSIA"
    cols[16] = "UKRAINE"
    cols[26] = "190"
    cols[30] = "-9.0"
    cols[31] = "25"
    cols[51] = "UP"
    cols[57] = "https://example.invalid/b"
    for k, v in over.items():
        cols[int(k[1:])] = v
    return "\t".join(cols)


class TestGdeltCompat(unittest.TestCase):
    """2.0（61 列）与 1.0 日文件（58 列）都必须能解析。

    这是"首次运行全盘失败"的根因：只按 61 列写死会在日文件上 IndexError，
    异常被吞掉后 0 事件，指数看上去照常生成，实际全是空窗口。
    """

    def test_v2_row(self) -> None:
        rows = gdelt_rows(_gdelt_row(), min_mentions=1, min_severity=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://example.invalid/a")
        self.assertEqual(rows[0].fips, "UP")
        self.assertEqual(rows[0].severity, 5)

    def test_v1_row(self) -> None:
        rows = gdelt_rows(_gdelt_row10(), min_mentions=1, min_severity=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://example.invalid/b")
        self.assertEqual(rows[0].fips, "UP")
        self.assertNotEqual(rows[0].region, "OTHER")

    def test_short_row_is_skipped_not_fatal(self) -> None:
        self.assertEqual(gdelt_rows("a\tb\tc", min_mentions=1, min_severity=1), [])

    def test_min_severity_drops_cooperation(self) -> None:
        coop = _gdelt_row(c26="010")          # MAKE PUBLIC STATEMENT -> sev 1
        rows = gdelt_rows(coop, min_mentions=1, min_severity=2)
        self.assertEqual(rows, [], "合作/表态类不该进入冲突压力")

    def test_min_mentions_drops_single_mention(self) -> None:
        faint = _gdelt_row(c31="1")
        self.assertEqual(gdelt_rows(faint, min_mentions=2, min_severity=1), [])
        self.assertEqual(len(gdelt_rows(faint, min_mentions=1, min_severity=1)), 1)

    def test_sanctions_are_economy(self) -> None:
        from pipeline.model import cameo_component
        self.assertEqual(cameo_component("172"), ("economy", 4))
        self.assertEqual(cameo_component("173"), ("economy", 4))
        self.assertEqual(cameo_component("171"), ("military", 4))
        self.assertEqual(cameo_component("190"), ("military", 5))


class TestMarket(unittest.TestCase):
    def test_parse_fred_skips_blank(self) -> None:
        text = "observation_date,VIXCLS\n2026-11-01,15.2\n2026-11-02,.\n2026-11-03,18.0\n"
        got = market.parse_fred(text)
        self.assertEqual(sorted(got), [date(2026, 11, 1), date(2026, 11, 3)])
        self.assertAlmostEqual(got[date(2026, 11, 3)], 18.0)

    def test_block_flags_stress_spike(self) -> None:
        d0 = date(2026, 8, 1)
        calm = {d0 + timedelta(days=i): 15.0 for i in range(60)}
        vix = dict(calm)
        vix[max(calm)] = 45.0
        block = market.build_block({"vix": vix})
        self.assertGreater(block["index"], 50.0)
        self.assertEqual(block["mode"], "live")
        self.assertEqual(block["rows"][0]["id"], "vix")
        self.assertIsNotNone(block["asof"])

    def test_block_empty_without_history(self) -> None:
        block = market.build_block({})
        self.assertEqual(block["mode"], "empty")
        self.assertEqual(block["index"], 50.0)


class TestBacktest(unittest.TestCase):
    def test_without_history_is_honest(self) -> None:
        row = backtest.run_anchor({}, date(2022, 2, 24), {"RUSSIA_CIS"}, "俄乌")
        self.assertEqual(row["status"], "no_data")
        self.assertEqual(backtest.summarize([row])["status"], "no_data")

    def test_spike_ahead_of_anchor_gives_lead(self) -> None:
        anchor = date(2022, 2, 24)
        start = anchor - timedelta(days=200)
        series: dict = {("RUSSIA_CIS", "military"): {}, ("RUSSIA_CIS", "diplomacy"): {}}
        for i in range(201):
            d = start + timedelta(days=i)
            base = 2.0 + (i % 5) * 0.2
            series[("RUSSIA_CIS", "military")][d] = base
            series[("RUSSIA_CIS", "diplomacy")][d] = base * 0.8
        for k in range(6, 10):                     # 锚点前 8 天开始飙升
            d = anchor - timedelta(days=k)
            series[("RUSSIA_CIS", "military")][d] = 26.0
            series[("RUSSIA_CIS", "diplomacy")][d] = 20.0

        row = backtest.run_anchor(series, anchor, {"RUSSIA_CIS"}, "俄乌")
        self.assertEqual(row["status"], "ok")
        self.assertTrue(row["warned"], "锚点前的尖峰必须被识别为预警")
        self.assertGreaterEqual(row["lead_days"], 5)
        self.assertLessEqual(row["lead_days"], 30)

    def test_verdict_flags_tiny_samples(self) -> None:
        fake = [{"status": "ok", "lead_days": 5, "warned": True, "fp_rate": 0.1}]
        out = backtest.summarize(fake)
        self.assertEqual(out["status"], "ok")
        self.assertIn("样本太小", out["verdict"])


class TestRunEndToEnd(unittest.TestCase):
    """不联网跑通 run：只把三个网络适配器换掉，其余全走真实代码路径。

    这个测试的价值在于它覆盖"用户第一次按下 Actions 之后会发生什么"——
    包括快照落盘、健康落盘、data_through 标注、market 块拼装。
    """

    def test_run_produces_snapshot(self) -> None:
        import pipeline.cli as cli
        from pipeline import fetch as fetcher
        from pipeline import physical

        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_run")
        os.makedirs(tmp, exist_ok=True)
        try:
            out = os.path.join(tmp, "snapshot.json")
            series = os.path.join(tmp, "series.jsonl")
            health = os.path.join(tmp, "health.json")
            target = date(2026, 11, 2)

            seeded = synth_signals(days=90, end=target - timedelta(days=1))
            store.save_series(series, build.pressure_from_signals(seeded),
                              keep_days=200, end=target - timedelta(days=1))
            today = synth_signals(days=1, end=target)
            items = synth_items(days=1, end=datetime(2026, 11, 2, 12, tzinfo=UTC))

            old_health = cli.DEFAULT_HEALTH
            cli.DEFAULT_HEALTH = health
            try:
                with mock.patch.object(fetcher, "fetch_gdelt_recent",
                                       return_value=(today, [{"source": "gdelt_recent", "ok": True}], target)), \
                     mock.patch.object(fetcher, "fetch_gdelt_daily",
                                       return_value=(today, [{"source": "gdelt", "ok": True}], target)), \
                     mock.patch.object(fetcher, "fetch_feeds",
                                       return_value=(items, [{"source": "bbc", "ok": True}])), \
                     mock.patch.object(market, "fetch_market",
                                       return_value={"index": 55.0, "rows": [], "health": []}), \
                     mock.patch.object(physical, "fetch_day",
                                       return_value=({("EUROPE", "physical"):
                                                      {date(2026, 11, 2): -0.25}},
                                                     {"source": "opensky", "ok": True,
                                                      "items": 5000, "error": ""})), \
                     mock.patch.object(hazard, "fetch_day",
                                       return_value=({"quake_energy": 15.0, "quake_count": 20.0},
                                                     [{"source": "usgs:quake", "ok": True}],
                                                     {"quake": [{"t": "M6.0 Somewhere", "v": 6.0,
                                                                 "ts": "2026-11-02T01:00+00:00",
                                                                 "u": "https://example.org/eq1"}]})), \
                     mock.patch.object(epidemic, "fetch_day",
                                       return_value=({"who_don": 1.0, "policy": 0.0},
                                                     [{"source": "who:don", "ok": True}],
                                                     {"who": [{"t": "Cholera outbreak", "v": "通报",
                                                               "ts": "", "u": "https://example.org/who1"}]})):
                    code = cli.main(["run", "--out", out, "--series", series])
            finally:
                cli.DEFAULT_HEALTH = old_health

            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                snap = json.load(fh)
            self.assertEqual(snap["coverage"]["data_through"], target.isoformat())
            self.assertEqual(snap["coverage"]["mode"], "live")
            self.assertIn("market", snap)
            self.assertEqual(snap["market"]["index"], 55.0)
            self.assertTrue(0.0 <= snap["geti"] <= 100.0)
            self.assertGreater(len(snap["series"]), 10)
            # 原始信源必须一路传进快照，不能被 try/except 悄悄吞掉
            self.assertEqual(snap["hazard"]["evidence"]["quake"][0]["v"], 6.0)
            self.assertEqual(snap["epidemic"]["evidence"]["who"][0]["t"], "Cholera outbreak")
            self.assertTrue(os.path.exists(health))
        finally:
            for name in os.listdir(tmp):
                os.remove(os.path.join(tmp, name))
            os.rmdir(tmp)


class TestFailedDays(unittest.TestCase):
    """抓取失败日必须被剔除，不能被当成"平静的一天"。"""

    def _series(self, vols, val=5.0):
        s = {build.VOLUME_KEY: {}}
        for i, n in enumerate(vols):
            d = date(2026, 9, 1) + timedelta(days=i)
            s[build.VOLUME_KEY][d] = n
            s.setdefault(("AFRICA", "military"), {})[d] = val
        return s

    def test_failed_days_are_dropped(self):
        vols = [20000] * 19 + [5]             # 最后一天只抓到 5 条
        series, bad = build.drop_failed_days(self._series(vols))
        self.assertEqual(bad, ["2026-09-20"])
        self.assertNotIn(date(2026, 9, 20), series[("AFRICA", "military")])
        self.assertEqual(len(series[("AFRICA", "military")]), 19)

    def test_normal_days_survive(self):
        vols = [20000, 18000, 22000, 15000, 21000, 19000]
        series, bad = build.drop_failed_days(self._series(vols))
        self.assertEqual(bad, [])
        self.assertEqual(len(series[("AFRICA", "military")]), 6)

    def test_no_volume_history_is_left_alone(self):
        plain = {("AFRICA", "military"): {date(2026, 9, 1): 3.0}}
        series, bad = build.drop_failed_days(plain)
        self.assertEqual(bad, [])
        self.assertEqual(series, plain)


class TestShareNormalize(unittest.TestCase):
    """抓取量差 300 倍的两天，折完之后必须可比。"""

    def _day(self, day, cells):
        return {k: {day: v} for k, v in cells.items()}

    def test_share_normalize_kills_volume_scale(self):
        d1, d2 = date(2026, 11, 2), date(2026, 10, 29)
        # 真实数据里的样子：好的一天抓两万六，坏的一天只抓几十条
        big = self._day(d1, {
            ("NA", "military"): 1646.14, ("NA", "diplomacy"): 1111.27,
            ("NA", "economy"): 771.55, ("NA", "internal"): 268.32,
            ("EU", "military"): 3000.0, ("EU", "diplomacy"): 5000.0,
        })
        small = self._day(d2, {
            ("NA", "military"): 12.06, ("NA", "diplomacy"): 8.92,
            ("NA", "economy"): 8.63, ("NA", "internal"): 2.45,
            ("EU", "military"): 9.0, ("EU", "diplomacy"): 6.0,
        })
        merged = {}
        for src in (big, small):
            for k, cell in src.items():
                merged.setdefault(k, {}).update(cell)
        out = build.share_normalize(merged)
        a = out[("NA", "military")][d1]
        b = out[("NA", "military")][d2]
        self.assertGreater(a, b)
        self.assertLess(a / b, 3.0)        # 原先是 137 倍

    def test_zero_total_is_safe(self):
        series = {("NA", "military"): {date(2026, 11, 1): 0.0}}
        out = build.share_normalize(series)
        self.assertEqual(out[("NA", "military")][date(2026, 11, 1)], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
