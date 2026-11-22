import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app_runtime
import download_history
import engine
import favorite_authors

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parents[1]


def _make_jpeg_bytes(color="red", size=(48, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _author_manifest(platform, uid, name, captured_at):
    url = {
        "weibo": f"https://weibo.com/u/{uid}",
        "douyin": f"https://www.douyin.com/user/{uid}",
        "xiaohongshu": f"https://www.xiaohongshu.com/user/profile/{uid}",
    }[platform]
    return {
        "platform": platform,
        "creator": {
            "id": uid,
            "name": name,
            "account": uid,
            "avatar": f"https://tvax4.sinaimg.cn/crop.0.0.100.100/{uid}.jpg",
            "url": url,
        },
        "inventory": {
            "complete": True,
            "captured_at": captured_at,
            "items": [{"id": f"{uid}-work-1"}],
        },
    }


class V191Tests(unittest.TestCase):
    def test_weibo_avatar_proxy_uses_referer_caches_locally_and_blocks_unknown_host(self):
        jpeg = _make_jpeg_bytes("orange")
        seen_headers = []

        class FakeResponse:
            def __init__(self, url, body):
                self._url = url
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self):
                return self._url

            def read(self, _limit=-1):
                return self._body

        class FakeOpener:
            def __init__(self):
                self.addheaders = []

            def open(self, url, timeout=8):
                seen_headers.append(dict(self.addheaders))
                return FakeResponse(url, jpeg)

        with tempfile.TemporaryDirectory() as td, patch.object(app_runtime, "DATA_DIR", Path(td)):
            avatar_url = "https://tvax4.sinaimg.cn/crop.18.0.1080.1080.1024/008IINbily8.jpg?KID=imgbed,tva&Expires=123&ssig=abc"
            with patch.object(engine, "build_opener", return_value=FakeOpener()):
                result = engine.fetch_author_avatar("weibo", avatar_url, key="weibo:7989896928")
            self.assertIsNotNone(result)
            body, mime = result
            self.assertEqual(mime, "image/jpeg")
            self.assertEqual(body, jpeg)
            self.assertEqual(seen_headers[0]["Referer"], "https://weibo.com/")

            # Second fetch hits local cache without making another network call.
            with patch.object(engine, "build_opener", side_effect=AssertionError("should use disk cache")):
                cached = engine.fetch_author_avatar("weibo", avatar_url, key="weibo:7989896928")
            self.assertEqual(cached, (jpeg, "image/jpeg"))

            # Even if a rotated/expired URL fails over the network, the cached avatar still serves.
            class FailingOpener:
                addheaders = []

                def open(self, _url, timeout=8):
                    raise OSError("403 expired")

            with patch.object(engine, "build_opener", return_value=FailingOpener()):
                fallback = engine.fetch_author_avatar("weibo", avatar_url + "&v=2", key="weibo:7989896928")
            self.assertEqual(fallback, (jpeg, "image/jpeg"))

            # Untrusted host is rejected cleanly.
            self.assertIsNone(engine.fetch_author_avatar("weibo", "https://evil.example.com/a.jpg", key="weibo:other"))

    def test_favorite_authors_unlimited_pinning_and_custom_drag_order_persist(self):
        with tempfile.TemporaryDirectory() as td, patch.object(app_runtime, "DATA_DIR", Path(td)):
            k1 = favorite_authors.keep("j1", _author_manifest("weibo", "1001", "作者一", "2026-11-22T01:00:00Z"))
            k2 = favorite_authors.keep("j2", _author_manifest("douyin", "1002", "作者二", "2026-11-22T02:00:00Z"))
            k3 = favorite_authors.keep("j3", _author_manifest("xiaohongshu", "1003", "作者三", "2026-11-22T03:00:00Z"))
            k4 = favorite_authors.keep("j4", _author_manifest("weibo", "1004", "作者四", "2026-11-22T04:00:00Z"))

            # Pin 3 authors (no cap on pinned count)
            favorite_authors.set_pin(k1, True)
            favorite_authors.set_pin(k2, True)
            favorite_authors.set_pin(k4, True)
            authors = favorite_authors.view()["authors"]
            self.assertEqual([a["key"] for a in authors], [k1, k2, k4, k3])
            self.assertEqual([a["pinned"] for a in authors], [True, True, True, False])

            # Custom drag reorder among pinned authors persists
            favorite_authors.reorder([k4, k1, k2, k3])
            self.assertEqual([a["key"] for a in favorite_authors.view()["authors"]], [k4, k1, k2, k3])

            # Re-inspecting an author preserves pinned state and custom order
            newer = _author_manifest("weibo", "1001", "作者一新名", "2026-11-22T09:00:00Z")
            favorite_authors.record_inventory("j1-new", "weibo", newer["creator"], lambda _: newer)
            authors = favorite_authors.view()["authors"]
            self.assertEqual([a["key"] for a in authors], [k4, k1, k2, k3])
            self.assertTrue(authors[1]["pinned"])
            self.assertEqual(authors[1]["name"], "作者一新名")

            # Unpinning moves author to top of unpinned group while keeping remaining pinned order
            favorite_authors.set_pin(k1, False)
            authors = favorite_authors.view()["authors"]
            self.assertEqual([a["key"] for a in authors], [k4, k2, k1, k3])
            self.assertEqual([a["pinned"] for a in authors], [True, True, False, False])

    def test_download_base_auto_platform_subfolders_and_persistent_history_across_switches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "app-data"
            data_dir.mkdir()
            settings_path = data_dir / "settings.json"
            base_a = root / "DiskA" / "作品库A"
            base_b = root / "DiskB" / "作品库B"

            with patch.object(app_runtime, "DATA_DIR", data_dir), \
                    patch.object(app_runtime, "CONFIG_PATH", settings_path), \
                    patch.object(engine, "_CONFIG_PATH", settings_path), \
                    patch.object(engine, "_EXPLICIT_ROOT", ""), \
                    patch.object(engine, "_DESTINATION", base_a):
                # Save base A and verify automatic platform subfolders
                res_a = engine.set_archive_root(str(base_a))
                self.assertEqual(Path(res_a["base_path"]), base_a.resolve())
                self.assertEqual(engine.get_archive_root("weibo"), (base_a / "微博").resolve())
                self.assertEqual(engine.get_archive_root("xiaohongshu"), (base_a / "小红书").resolve())
                self.assertEqual(engine.get_archive_root("douyin"), (base_a / "抖音").resolve())

                # Simulate downloaded Weibo work in Base A under 微博/作者（账号）
                wb_root_a = engine.get_archive_root("weibo")
                engine.prepare_archive_root(wb_root_a, "weibo")
                author_dir_a = wb_root_a / "白斤斤啊（7989896928）"
                author_dir_a.mkdir(parents=True)
                img_a = author_dir_a / "自拍--50001--图001.jpg"
                img_a.write_bytes(_make_jpeg_bytes("blue"))
                child_a = {
                    "job_id": "content-20261122T010000Z-aaaa1111",
                    "platform": "weibo",
                    "content_id": "50001",
                    "title": "自拍",
                    "status": "completed",
                    "direct_archive": True,
                    "author_folder": "白斤斤啊（7989896928）",
                    "output_dir": "白斤斤啊（7989896928）",
                    "completed_at": "2026-11-22T01:00:00Z",
                    "outputs": [{"role": "image", "path": "白斤斤啊（7989896928）/自拍--50001--图001.jpg"}],
                }
                CORE.write_json(wb_root_a / "jobs" / child_a["job_id"] / "manifest.json", child_a)
                row_a = download_history.from_manifest(wb_root_a, child_a)
                download_history.save([row_a], author=("weibo", "7989896928"))

                # Switch download path to Base B (simulating user changing drive/subfolder + restart)
                engine.set_archive_root(str(base_b))
                self.assertEqual(engine.get_destination_base(), base_b.resolve())
                self.assertEqual(engine.get_archive_root("weibo"), (base_b / "微博").resolve())
                self.assertEqual(engine.get_archive_root("xiaohongshu"), (base_b / "小红书").resolve())

                # Verify known_archive_roots still remembers Base A's platform root AND Base B's platform root
                known = engine.known_archive_roots()
                self.assertIn(wb_root_a, known)
                self.assertIn((base_b / "微博").resolve(), known)

                # Verify history remembers the work in Base A and resolves its exact local file
                entry = download_history.entry("weibo", "50001")
                self.assertIsNotNone(entry)
                self.assertEqual(Path(entry["folder"]), author_dir_a.resolve())
                hist_root, hist_child, _, _ = engine.history_work(entry)
                local = engine.history_local_path(entry, hist_root, hist_child)
                self.assertEqual(local, img_a.resolve())

                # Author-scoped history still finds work 50001 after switching to Base B
                author_page = download_history.page(author=("weibo", "7989896928"))
                self.assertEqual(author_page["success"]["total"], 1)
                self.assertEqual(author_page["success"]["items"][0]["work_id"], "50001")

    def test_domestic_xiaohongshu_domain_routing_and_v191_ui_markers(self):
        norm, plat = CORE.submission_url("https://www.xiaohongshu.com/user/profile/5b56f8d14eacab03b8ec5231")
        self.assertEqual(plat, "xiaohongshu")
        self.assertTrue(CORE.is_creator_profile_url(norm, plat))
        self.assertEqual(CORE._redbook_backend(norm), "xhs")
        self.assertEqual(CORE._redbook_domain("xhs"), "www.xiaohongshu.com")

        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn("/api/author-avatar", html)
        self.assertIn('data-action="pin"', html)
        self.assertIn("favorite-drag-handle", html)
        self.assertIn('id="archive-drive"', html)
        self.assertIn('id="archive-preview-chips"', html)
        self.assertIn("https://www.xiaohongshu.com/", html)


if __name__ == "__main__":
    unittest.main()
