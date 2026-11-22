import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import app_runtime
import engine

CORE = engine.get_mod()
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _FakeStreamResponse:
    def __init__(self, chunks, *, status=200, headers=None, url="https://p3-sign.douyinpic.com/sample.jpg"):
        self._chunks = list(chunks)
        self.status = status
        self.headers = headers or {"Content-Length": str(sum(len(c) for c in self._chunks))}
        self._url = url

    def read1(self, _size=262144):
        return self._chunks.pop(0) if self._chunks else b""

    def read(self, _size=262144):
        return self.read1(_size)

    def getheader(self, name, default=None):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def geturl(self):
        return self._url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class V188RegressionTests(unittest.TestCase):
    def test_copy_media_stream_reports_bytes_without_observer(self):
        reported = []
        CORE.TRANSFER_OBSERVER.callback = reported.append
        try:
            response = _FakeStreamResponse([b"hello ", b"world!"])
            output = io.BytesIO()
            total = CORE.copy_media_stream(response, output, 1024, time.monotonic())
        finally:
            CORE.TRANSFER_OBSERVER.callback = None
        self.assertEqual(total, 12)
        self.assertEqual(output.getvalue(), b"hello world!")
        self.assertEqual(reported, [6, 6])

    def test_copy_media_stream_uses_explicit_observer_when_provided(self):
        global_reported = []
        custom_reported = []
        CORE.TRANSFER_OBSERVER.callback = global_reported.append
        try:
            response = _FakeStreamResponse([b"abc", b"defg"])
            output = io.BytesIO()
            total = CORE.copy_media_stream(response, output, 1024, time.monotonic(), observer=custom_reported.append)
        finally:
            CORE.TRANSFER_OBSERVER.callback = None
        self.assertEqual(total, 7)
        self.assertEqual(output.getvalue(), b"abcdefg")
        self.assertEqual(custom_reported, [3, 4])
        self.assertEqual(global_reported, [])

    def test_douyin_live_photo_download_end_to_end_via_copy_media_stream(self):
        img_buf = io.BytesIO()
        Image.new("RGB", (64, 64), (240, 120, 60)).save(img_buf, format="JPEG")
        jpeg_bytes = img_buf.getvalue()
        mp4_bytes = (FIXTURES / "valid.mp4").read_bytes()

        image_url = "https://p3-sign.douyinpic.com/tos-cn-i-0813/test-still.jpeg"
        motion_url = "https://v3-web.douyinvod.com/tos-cn-v-0001/test-motion.mp4"

        def fake_opener_open(request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url == image_url:
                return _FakeStreamResponse([jpeg_bytes[:200], jpeg_bytes[200:]], url=url)
            if url == motion_url:
                return _FakeStreamResponse([mp4_bytes[:4096], mp4_bytes[4096:]], url=url)
            raise AssertionError(f"Unexpected URL: {url}")

        fake_opener = MagicMock()
        fake_opener.open.side_effect = fake_opener_open

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = root / "jobs" / "content-20261122T010000Z-12345678"
            job_dir.mkdir(parents=True)
            manifest_path = job_dir / "manifest.json"
            manifest = {
                "job_id": job_dir.name,
                "platform": "douyin",
                "source": "https://www.douyin.com/note/1795267693",
                "content_id": "1795267693",
                "title": "未命名作品",
                "status": "queued",
                "author_folder": "siusiu_（79013563372）",
                "direct_archive": True,
                "douyin_media": {
                    "kind": "live_photo",
                    "content_id": "1795267693",
                    "item_count": 1,
                    "clean_source_revision": CORE.DOUYIN_CLEAN_REVISION,
                    "items": [{
                        "width": 64,
                        "height": 64,
                        "has_still": True,
                        "has_motion": True,
                        "image_urls": [image_url],
                        "motion_urls": [motion_url],
                        "motion_variants": [{"urls": [motion_url], "clean_playback": True, "width": 64, "height": 64}],
                    }],
                },
            }
            CORE.write_json(manifest_path, manifest)

            transferred = []
            CORE.TRANSFER_OBSERVER.callback = transferred.append
            try:
                with (
                    patch.object(CORE, "_download_with_persistent_https", return_value=False),
                    patch.object(CORE, "validate_public_https_url", side_effect=lambda u: u),
                    patch.object(CORE, "build_opener", return_value=fake_opener),
                    patch.object(CORE, "fetch_douyin_aweme_detail", side_effect=CORE.ArchiveError("skip", "skip", 69)),
                ):
                    result = CORE.process_douyin_gallery(manifest, manifest_path, root)
            finally:
                CORE.TRANSFER_OBSERVER.callback = None

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["outputs"]), 2)
            self.assertEqual(sum(transferred), len(jpeg_bytes) + len(mp4_bytes))
            for rec in result["outputs"]:
                self.assertTrue((root / rec["path"]).is_file(), rec)

    def test_persistent_https_updates_timeout_when_pooled_socket_is_closed(self):
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"

        class FakeConn:
            def __init__(self):
                self.timeout = 999
                self.sock = None

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return _FakeStreamResponse([jpeg_bytes])

            def close(self):
                pass

        conn = FakeConn()
        CORE._DOWNLOAD_CONNECTIONS.pool = {("p3-sign.douyinpic.com", 443): conn}
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "sample.jpg"
                with (
                    patch("urllib.request.getproxies", return_value={}),
                    patch.object(CORE, "operation_wait", side_effect=lambda ceiling=None: 7 if ceiling else None),
                ):
                    ok = CORE._download_with_persistent_https(
                        "https://p3-sign.douyinpic.com/sample.jpg",
                        target,
                        referer="https://www.douyin.com/",
                        max_bytes=1024 * 1024,
                    )
                self.assertTrue(ok)
                self.assertEqual(conn.timeout, 7)
                self.assertEqual(target.read_bytes(), jpeg_bytes)
        finally:
            CORE._DOWNLOAD_CONNECTIONS.pool = {}

    def test_xiaohongshu_full_inventory_respects_include_profile_flag(self):
        seen_commands = []

        class Completed:
            returncode = 0
            stdout = json.dumps({
                "user": {"userId": "638b68b6000000001f015fdb", "nickname": "Author"},
                "notes": [{"noteId": "a" * 24, "title": "Note 1", "xsecToken": "tok"}],
            })
            stderr = ""

        def fake_run(cmd, **_kwargs):
            seen_commands.append(list(cmd))
            return Completed()

        profile_url = "https://www.xiaohongshu.com/user/profile/638b68b6000000001f015fdb"
        with (
            patch.object(CORE, "_redbook_command", return_value=["node", "cli.js"]),
            patch.object(CORE, "_firefox_xiaohongshu_cookie_file", return_value=None),
            patch.object(CORE, "run_background_process", side_effect=fake_run),
        ):
            CORE._xiaohongshu_full_inventory(profile_url, "firefox", include_profile=False)
            CORE._xiaohongshu_full_inventory(profile_url, "firefox", include_profile=True)

        self.assertNotIn("--with-profile", seen_commands[0])
        self.assertIn("--with-profile", seen_commands[1])

    def test_selection_disk_status_reports_item_failure_without_child_manifest(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app_runtime, "DATA_DIR", Path(directory)):
            root = Path(directory)
            job = "batch-20261122T010000Z-11223344"
            batch = {
                "platform": "douyin",
                "updated_at": "2026-11-22T01:00:00Z",
                "creator": {"id": 998877, "url": "https://www.douyin.com/user/998877"},
                "inventory": {
                    "items": [{
                        "id": "1795267693",
                        "title": "未命名作品",
                        "child_job_id": None,
                        "result": "failed",
                        "error_code": "submit_failed",
                        "error_message": "提交失败",
                    }]
                },
            }
            with patch.object(engine, "job_root", return_value=root), patch.object(engine, "read_manifest", return_value=batch):
                disk = engine.selection_disk_status(job, ["1795267693"])
            self.assertEqual(disk["failed"], 1)
            self.assertEqual(disk["pending"], 0)
            self.assertEqual(disk["author"], ("douyin", "998877"))
            self.assertEqual(len(disk["history"]), 1)


if __name__ == "__main__":
    unittest.main()
