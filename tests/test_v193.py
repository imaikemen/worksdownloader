import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import favorite_authors

PROJECT = Path(__file__).resolve().parents[1]


class V193Tests(unittest.TestCase):
    def test_partial_time_budgeted_weibo_inventory_can_be_added_to_favorites_and_merges_baseline(self):
        creator = {
            "id": "5838703552",
            "name": "草莓不吃柚子",
            "account": "5838703552",
            "avatar": "https://tvax4.sinaimg.cn/crop.0.0.100.100/demo.jpg",
            "url": "https://weibo.com/u/5838703552",
        }
        partial_manifest = {
            "platform": "weibo",
            "creator": creator,
            "inventory": {
                "complete": False,
                "captured_at": "2026-11-22T10:00:00Z",
                "items": [{"id": "5367407885156501"}, {"id": "5363959835396543"}],
            },
        }
        with tempfile.TemporaryDirectory() as td, patch.object(app_runtime, "DATA_DIR", Path(td)):
            key = favorite_authors.keep("batch-wb-partial-1", partial_manifest, "firefox")
            self.assertEqual(key, "weibo:5838703552")
            authors = favorite_authors.view()["authors"]
            self.assertEqual(len(authors), 1)
            self.assertEqual(authors[0]["name"], "草莓不吃柚子")

            # A subsequent partial re-inventory merges new IDs without dropping older known baseline IDs.
            newer_partial = {
                "platform": "weibo",
                "creator": creator,
                "inventory": {
                    "complete": False,
                    "captured_at": "2026-11-22T11:00:00Z",
                    "items": [{"id": "5369999999999999"}],
                },
            }
            favorite_authors.record_inventory("batch-wb-partial-2", "weibo", creator, lambda _: newer_partial, "firefox")
            stored = favorite_authors._read()[key]
            self.assertEqual(
                stored["baseline_ids"],
                ["5363959835396543", "5367407885156501", "5369999999999999"],
            )

            # Single-work and empty-partial manifests are still rejected.
            with self.assertRaises(ValueError):
                favorite_authors.keep("single", {**partial_manifest, "creator": {**creator, "single_work": True}})
            with self.assertRaises(ValueError):
                favorite_authors.keep("empty", {**partial_manifest, "inventory": {"complete": False, "captured_at": "2026-11-22T10:00:00Z", "items": []}})

    def test_v193_single_char_favorite_search_and_version_markers(self):
        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn(f"<h1>作品鉴赏downloader V{app_runtime.VERSION}</h1>", html)
        self.assertIn("if (!q || !favoriteAuthors.length)", html)
        self.assertNotIn("if (q.length < 2) {\n      lastMatches = [];", html)
        self.assertIn('class="disclaimer-banner"', html)
        self.assertIn("仅用于个人学习", html)
        self.assertIn("24 小时内", html)
        self.assertIn('id="disclaimer-dialog"', html)
        self.assertIn('value="agree" disabled', html)
        self.assertIn('value="disagree" disabled', html)
        self.assertIn("let remaining = 5;", html)
        self.assertIn("'/api/exit'", html)


if __name__ == "__main__":
    unittest.main()
