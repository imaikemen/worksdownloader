import ctypes
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import download_history
import engine

CORE = engine.get_mod()


class DownloadHistoryTests(unittest.TestCase):
    def test_failed_work_moves_to_success_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app_runtime, "DATA_DIR", Path(folder)):
            root = Path(folder)
            child = {"platform": "douyin", "content_id": "42", "title": "示例", "status": "failed",
                     "updated_at": "2026-11-19T11:00:00Z", "error": {"message": "网络异常"}}
            failure = download_history.from_manifest(root, child)
            download_history.save([failure])
            self.assertEqual(download_history.page()["failure"]["total"], 1)
            done = {**child, "status": "completed", "completed_at": "2026-11-19T12:00:00Z", "output_dir": "content/作品"}
            download_history.save([download_history.from_manifest(root, done)])
            download_history.save([failure])
            result = download_history.page()
            self.assertEqual(result["failure"]["total"], 0)
            self.assertEqual(result["success"]["total"], 1)
            self.assertEqual(result["success"]["items"][0]["folder"], str(root / "content/作品"))


class TransferTests(unittest.TestCase):
    def test_background_process_has_no_console_on_windows(self):
        if sys.platform != "win32" or not hasattr(ctypes, "windll"):
            self.skipTest("Windows console behavior")
        result = CORE.run_background_process(
            [sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_range_integrity_and_fallback(self):
        total = 12 * 1024 ** 2
        headers = {"Accept-Ranges": "bytes", "ETag": '"known"'}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out.mp4"
            barrier = threading.Barrier(3, timeout=5)

            def fetch(_url, _etag, _total, interval, path, _referer, _observer, **kwargs):
                barrier.wait()
                first, last = interval
                path.write_bytes(bytes([first // (4 * 1024 ** 2)]) * (last - first + 1))

            with patch.object(CORE, "_download_media_range", side_effect=fetch):
                self.assertTrue(CORE.download_video_ranges("https://media.example/video", headers, total, out, ""))
            data = out.read_bytes()
            self.assertEqual(len(data), total)
            self.assertEqual([data[i * 4 * 1024 ** 2] for i in range(3)], [0, 1, 2])
            out.unlink()
            with patch.object(CORE, "_download_media_range", side_effect=CORE.ArchiveError("bad", "range")):
                self.assertFalse(CORE.download_video_ranges("https://media.example/video", headers, total, out, ""))
            self.assertFalse(out.exists())
        with self.assertRaises(CORE.ArchiveError):
            CORE.whole_media_size(206, {"Content-Range": "bytes 0-499/1000"})

    def test_public_work_uses_guest_extractor_and_wraps_download_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            url = "https://www.douyin.com/video/1234567890123456789"
            info = {"id": "1234567890123456789", "title": "公开视频", "formats": [{"url": "https://v.douyinvod.com/demo.mp4"}]}
            with patch.object(CORE, "inspect_with_transparent_core", return_value=info) as probe:
                result = CORE.inspect_single_work(url, root, public_only=True)
            probe.assert_called_once_with(url, "douyin", use_cookies=False)
            manifest = CORE.read_json_if_valid(root / result["manifest"])
            self.assertTrue(manifest["inventory"]["items"][0]["public_video"])
            self.assertTrue(manifest["creator"]["single_work"])
