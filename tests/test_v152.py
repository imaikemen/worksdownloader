import tempfile
import threading
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import Mock, patch

import app_runtime
import engine

CORE = engine.get_mod()


class DownloaderTests(unittest.TestCase):
    def test_preview_requests_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            barrier = threading.Barrier(2)
            finished = threading.Event()
            received = []
            items = [{"id": str(1234567890123456789 + i)} for i in range(2)]

            def fetch(item):
                barrier.wait(timeout=4)
                received.append(item["id"])
                if len(received) == 2:
                    finished.set()
                return b"\xff\xd8\xff" + b"data" * 50, "image/jpeg"

            with patch.object(engine, "fetch_remote_preview", side_effect=fetch):
                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(engine, "_PREVIEW_POOL", pool):
                    engine.background_generate_covers(items, root=root, fetch_remote=True)
                    self.assertTrue(finished.wait(timeout=5), "The two network requests should run concurrently")
            self.assertCountEqual(received, [item["id"] for item in items])

    def test_managed_platform_paths_override_previous_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = base / "settings.json"
            app_runtime.atomic_json(settings, {"platform_folders": {"douyin": str(base / "旧抖音")}})
            destination = base / "作品提取"
            (destination / "rednote").mkdir(parents=True)
            with patch.object(engine, "_EXPLICIT_ROOT", ""), patch.object(engine, "_CONFIG_PATH", settings), \
                    patch.object(app_runtime, "CONFIG_PATH", settings), patch.object(engine, "_DESTINATION", destination):
                self.assertEqual(engine.get_archive_root("douyin"), destination / "抖音")
                self.assertEqual(engine.get_archive_root("xiaohongshu"), destination / "rednote")
                new_base = base / "新下载库"
                engine.set_archive_root(str(new_base))
                self.assertEqual(engine.get_archive_root("douyin"), new_base / "抖音")
                self.assertEqual(engine.get_archive_root("xiaohongshu"), new_base / "小红书")
                self.assertEqual(engine.get_archive_root("weibo"), new_base / "微博")

    def test_douyin_uses_highest_frozen_bitrate(self):
        aweme = {"aweme_id": "1234567890123456789", "video": {"bit_rate": [
            {"bit_rate": 100, "play_addr": {"url_list": ["https://v.douyinvod.com/low"]}},
            {"bit_rate": 900, "play_addr": {"url_list": ["https://v.douyinvod.com/high"]}},
        ]}}
        self.assertEqual(CORE.douyin_aweme_media(aweme)["video_urls"], ["https://v.douyinvod.com/high"])

    def test_xhs_auth_error_is_actionable(self):
        completed = Mock(returncode=1, stderr="Session expired — please re-login\nCode: -100")
        auth = CORE._redbook_auth_error(completed, "rednote")
        self.assertEqual(auth.code, "reauthentication_required")
        self.assertIn("www.rednote.com", str(auth))
        self.assertIn("Firefox", str(auth))

    def test_rednote_profile_keeps_its_backend(self):
        url, platform = CORE.submission_url("https://www.rednote.com/user/profile/5e4414b6000000000100142c")
        self.assertEqual(platform, "xiaohongshu")
        self.assertTrue(CORE.is_creator_profile_url(url, platform))
        self.assertEqual(CORE._redbook_backend(url), "rednote")

    def test_rednote_notes_keep_their_backend_and_preview(self):
        note_id = "5e4414b6000000000100142c"
        profile = "https://www.rednote.com/user/profile/63bed214000000002702b862"
        cover = "http://sns-web-i10.rednotecdn.com/demo!preview.webp"
        data = {"notes": [{
            "note_id": note_id,
            "webUrl": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=sample&xsec_source=pc_user",
            "type": "normal",
            "cover": {"url_pre": cover},
        }]}
        response = Mock(returncode=0, stdout=json.dumps(data))
        with patch.object(CORE, "_redbook_command", return_value=["redbook"]), \
                patch.object(CORE, "_firefox_xiaohongshu_cookie_file", return_value=None), \
                patch.object(CORE.subprocess, "run", return_value=response):
            creator, [item] = CORE._xiaohongshu_full_inventory(profile, "firefox")
        self.assertEqual(creator["xiaohongshu_backend"], "rednote")
        self.assertEqual(engine._platform_for_item(item), "xiaohongshu")
        parsed = urlsplit(item["url"])
        self.assertEqual(parsed.hostname, "www.rednote.com")
        self.assertEqual(parse_qs(parsed.query)["xsec_token"], ["sample"])
        self.assertEqual(item["preview_urls"], [cover])
        self.assertIn("https://ci.xiaohongshu.com/demo?imageView2/2/w/0/format/jpg/v3", CORE._xhs_original_image_urls(cover))
        self.assertEqual(CORE._xhs_safe_media_url("https://other-rednotecdn.com/demo"), "")

    def test_failed_partial_work_is_not_marked_downloaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "content/小红书/示例--5e4414b6000000000100142c/配图"
            content.mkdir(parents=True)
            item = {
                "id": "5e4414b6000000000100142c",
                "url": "https://www.rednote.com/explore/5e4414b6000000000100142c",
                "title": "示例",
                "media_kind": "image_gallery",
            }
            self.assertFalse(engine._has_already_done(item, root=root))

    def test_valid_api_fallback_is_complete_and_reusable(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "content/小红书/示例"
            folder.mkdir(parents=True)
            image = folder / "01.webp"
            Image.new("RGB", (32, 32), "red").save(image)
            child = {
                "platform": "xiaohongshu", "content_type": "image_text", "status": "completed",
                "output_dir": folder.relative_to(root).as_posix(), "expected_image_count": 1,
                "image_quality": "api_fallback",
                "outputs": [{"role": "image", "path": image.relative_to(root).as_posix()}],
            }
            self.assertTrue(CORE._child_output_files_exist(root, child))
            self.assertFalse(CORE._xhs_image_child_needs_quality_upgrade(child))
            self.assertFalse(CORE._child_output_files_exist(root, {**child, "expected_image_count": 2}))
