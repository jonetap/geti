"""信源表的一致性。

一个源如果只在 RSS_FEEDS 里、不在 REGISTRY 里，程序不会报错——它会静悄悄
按"自媒体 0.35"计分。这种错没有任何提示，只能靠测试挡住。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import fetch                      # noqa: E402
from pipeline.model import REGISTRY, source     # noqa: E402

UTC = timezone.utc


def _rss(pub: datetime, title: str = "Some headline") -> bytes:
    stamp = pub.strftime("%a, %d %b %Y %H:%M:%S +0000")
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>{title}</title><link>https://example.org/a</link>
<description>d</description><pubDate>{stamp}</pubDate></item>
</channel></rss>"""
    return body.encode("utf-8")


class TestSourceRegistry(unittest.TestCase):
    def test_every_feed_has_a_registry_entry(self):
        missing = [sid for sid in fetch.RSS_FEEDS if sid not in REGISTRY]
        self.assertEqual(missing, [], f"这些源没登记，会被当成自媒体 0.35：{missing}")

    def test_unknown_source_degrades_loudly(self):
        # 退化本身是对的（不崩），但不该悄悄发生在已配置的源上
        self.assertLess(source("no_such_source").prior, 0.5)


class TestStaleItems(unittest.TestCase):
    def test_old_items_are_dropped_and_reported(self):
        old = datetime.now(UTC) - timedelta(days=9)
        with mock.patch.object(fetch, "http_get", return_value=_rss(old)):
            items, health = fetch.fetch_feeds({"xinhua_world": ("http://x", "zh")})
        self.assertEqual(items, [])
        self.assertFalse(health[0]["ok"])
        self.assertIn("48 小时", health[0]["error"])

    def test_fresh_items_are_kept(self):
        fresh = datetime.now(UTC) - timedelta(hours=2)
        with mock.patch.object(fetch, "http_get", return_value=_rss(fresh)):
            items, health = fetch.fetch_feeds({"chinanews": ("http://x", "zh")})
        self.assertEqual(len(items), 1)
        self.assertTrue(health[0]["ok"])
        self.assertEqual(items[0].source_id, "chinanews")


if __name__ == "__main__":
    unittest.main()
