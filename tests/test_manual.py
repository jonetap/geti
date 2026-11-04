"""人工线索层：解析 issue、标"未收录"、失败不阻断。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import manual  # noqa: E402


def _issue(body: str, title: str = "线索：敖德萨", number: int = 7, **over) -> dict:
    data = {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/jonetap/geti/issues/{number}",
        "user": {"login": "friend"},
        "created_at": "2026-11-03T21:10:00Z",
    }
    data.update(over)
    return data


BODY = """### 链接
https://tass.ru/example/12345

### 一句话说明
敖德萨港夜里出现排队长龙，本地俄语频道在播。

### 截图
（直接把图片拖进来，或者从剪贴板粘贴）
![shot](https://github.com/user-attachments/assets/abc-123)
"""


class TestParseIssue(unittest.TestCase):
    def test_extracts_links_note_and_image(self):
        row = manual.parse_issue(_issue(BODY))
        self.assertIsNotNone(row)
        self.assertEqual(row["author"], "friend")
        self.assertEqual(row["kind"], "link")
        self.assertIn("https://tass.ru/example/12345", row["links"])
        self.assertEqual(row["images"],
                         ["https://github.com/user-attachments/assets/abc-123"])
        self.assertIn("排队长龙", row["note"])

    def test_attachment_url_counts_as_image_not_link(self):
        body = "### 截图\nhttps://github.com/user-attachments/assets/xyz\n"
        row = manual.parse_issue(_issue(body))
        self.assertEqual(row["kind"], "image")
        self.assertEqual(row["links"], [])
        self.assertEqual(row["images"], ["https://github.com/user-attachments/assets/xyz"])

    def test_pull_request_is_ignored(self):
        self.assertIsNone(manual.parse_issue(_issue(BODY, pull_request={"url": "x"})))

    def test_empty_issue_is_ignored(self):
        self.assertIsNone(manual.parse_issue(_issue("   ", title="")))

    def test_bad_payload_never_raises(self):
        self.assertEqual(manual.parse_issues({"message": "API rate limit exceeded"}), [])
        self.assertEqual(manual.parse_issues(None), [])
        self.assertEqual(manual.parse_issues([]), [])
        self.assertEqual(manual.parse_issues(["not a dict"]), [])

    def test_note_falls_back_to_first_line(self):
        row = manual.parse_issue(_issue("https://example.com/a\n只有这一行说明"))
        self.assertIn("只有这一行说明", row["note"])


class TestMarkSeen(unittest.TestCase):
    def test_link_already_fetched_is_seen(self):
        class It:
            url = "https://www.tass.ru/example/12345?utm=x"

        rows = [{"links": ["https://tass.ru/example/12345"]}]
        manual.mark_seen(rows, [It()])
        self.assertTrue(rows[0]["seen"])

    def test_link_missing_from_fetch_is_unseen(self):
        class It:
            url = "https://other.example/story"

        rows = [{"links": ["https://tass.ru/example/12345"]}]
        manual.mark_seen(rows, [It()])
        self.assertFalse(rows[0]["seen"])


class TestBuildBlock(unittest.TestCase):
    def test_failure_degrades_to_empty_block(self):
        with mock.patch.object(manual, "fetch_issues", side_effect=RuntimeError("boom")):
            block, health = manual.build_block([], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertEqual(block["items"], [])
        self.assertEqual(block["mode"], "failed")
        self.assertFalse(health["ok"])
        self.assertIn("boom", health["error"])

    def test_counts_unseen_and_sorts_newest_first(self):
        older = _issue(BODY, number=1)
        older["created_at"] = "2026-11-01T10:00:00Z"
        newer = _issue("### 链接\nhttps://example.org/new\n说明一下", number=2)
        with mock.patch.object(manual, "fetch_issues", return_value=[older, newer]):
            block, health = manual.build_block([], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertTrue(health["ok"])
        self.assertEqual(len(block["items"]), 2)
        self.assertEqual(block["items"][0]["number"], 2)
        self.assertEqual(block["unseen"], 2)

    def test_old_items_are_dropped(self):
        old = _issue(BODY, number=3)
        old["created_at"] = "2026-01-01T00:00:00Z"
        with mock.patch.object(manual, "fetch_issues", return_value=[old]):
            block, _ = manual.build_block([], now=datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertEqual(block["items"], [])


if __name__ == "__main__":
    unittest.main()
