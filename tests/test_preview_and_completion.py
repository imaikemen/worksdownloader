import importlib.util
import io
import app_runtime
from PIL import Image
import json
import tempfile
import threading
import unittest
import http.client
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
CORE_PATH = PROJECT / "core" / "media_archive.py"
SPEC = importlib.util.spec_from_file_location("local_media_archive", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CORE)


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, format="JPEG")
    return buffer.getvalue()

def video_bytes():
    return (PROJECT / "tests/fixtures/valid.mp4").read_bytes()

def output(root: Path, path: Path, role: str) -> dict:
    return {"path": path.relative_to(root).as_posix(), "role": role}


class CompletionTests(unittest.TestCase):
    def test_xhs_legacy_image_child_is_not_reused_for_original_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "content" / "小红书" / "note--abc"
            image_dir = out / "配图"
            image_dir.mkdir(parents=True)
            image = image_dir / "01.jpg"
            image.write_bytes(image_bytes())
            child = {
                "platform": "xiaohongshu",
                "content_type": "image_text",
                "status": "completed",
                "output_dir": out.relative_to(root).as_posix(),
                "outputs": [output(root, image, "image")],
            }
            self.assertTrue(CORE._child_output_files_exist(root, {**child, "image_quality": "original_cdn"}))
            self.assertFalse(CORE._child_output_files_exist(root, child))
            self.assertTrue(
                CORE._xhs_image_child_needs_quality_upgrade(
                    {**child, "image_quality": "original_cdn", "outputs": [output(root, image_dir / "01.bin", "image")]}
                )
            )

            manifest_path = root / "jobs" / "content-20260914T000000Z-aaaaaaaa" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps({**child, "job_id": manifest_path.parent.name, "content_id": "note-id"}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertIsNone(CORE.existing_content(root, "xiaohongshu", "note-id"))

    def test_original_cdn_image_without_extension_gets_real_suffix(self):
        self.assertEqual(
            CORE.media_extension_from_bytes("application/octet-stream", "https://sns-img-bd.xhscdn.com/object", b"RIFF0000WEBP"),
            ".webp",
        )

    def test_worker_can_process_a_bounded_batch_in_parallel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            paths = []
            for index in range(4):
                path = jobs / f"content-20260914T000000Z-{index + 1:08d}" / "manifest.json"
                path.parent.mkdir()
                path.write_text('{"status":"queued","platform":"douyin"}', encoding="utf-8")
                paths.append(path)
            barrier = threading.Barrier(4)

            def fake_process(_path, _root):
                barrier.wait(timeout=3)
                return {}

            with mock.patch.object(CORE, "process_content_job", side_effect=fake_process) as process:
                _worker, processed = CORE.content_worker_once(root, max_parallel=4)
            self.assertTrue(processed)
            self.assertEqual(process.call_count, 4)

    def test_video_cover_alone_is_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "content" / "抖音" / "sample--1"
            out.mkdir(parents=True)
            (out / "cover.jpg").write_bytes(b"x" * 800)
            child = {
                "output_dir": out.relative_to(root).as_posix(),
                "douyin_media": {"kind": "video"},
                "outputs": [output(root, out / "cover.jpg", "cover_image")],
            }
            self.assertFalse(CORE._child_output_files_exist(root, child))
            video = out / "video.mp4"
            video.write_bytes(video_bytes())
            child["outputs"].append(output(root, video, "video"))
            self.assertTrue(CORE._child_output_files_exist(root, child))

    def test_gallery_requires_every_frozen_media_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "content" / "抖音" / "gallery--2"
            images = out / "图片"
            live = out / "Live图"
            images.mkdir(parents=True)
            live.mkdir(parents=True)
            image1, image2, motion = images / "001.jpg", images / "002.jpg", live / "002.mp4"
            image1.write_bytes(image_bytes())
            child = {
                "output_dir": out.relative_to(root).as_posix(),
                "douyin_media": {
                    "kind": "live_photo",
                    "items": [
                        {"image_urls": ["https://p3.douyinpic.com/1"], "motion_urls": []},
                        {"image_urls": ["https://p3.douyinpic.com/2"], "motion_urls": ["https://v.douyinvod.com/2"]},
                    ],
                },
                "outputs": [output(root, image1, "image")],
            }
            self.assertFalse(CORE._child_output_files_exist(root, child))
            image2.write_bytes(image_bytes())
            motion.write_bytes(video_bytes())
            child["outputs"].extend([output(root, image2, "image"), output(root, motion, "live_motion")])
            self.assertTrue(CORE._child_output_files_exist(root, child))

    def test_failed_child_with_complete_media_is_recovered_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_id = "batch-20260914T000000Z-cccccccc"
            child_id = "content-20260914T000000Z-dddddddd"
            parent_path = root / "jobs" / job_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            out = root / "content" / "抖音" / "作品--123"
            out.mkdir(parents=True)
            video = out / "video.mp4"
            video.write_bytes(video_bytes())
            parent_path.parent.mkdir(parents=True)
            child_path.parent.mkdir(parents=True)
            parent = {
                "job_id": job_id, "kind": "creator_batch", "platform": "douyin",
                "selection": {"ids": ["123"]}, "child_job_ids": [child_id],
                "inventory": {"items": [{"id": "123", "title": "作品", "media_kind": "video", "child_job_id": child_id}]},
            }
            child = {
                "job_id": child_id, "kind": "content", "platform": "douyin", "status": "failed",
                "output_dir": "content/抖音/作品--123", "douyin_media": {"kind": "video"},
                "outputs": [output(root, video, "video")],
                "error": {"code": "missing_command", "message": "转写工具不可用"},
            }
            parent_path.write_text(json.dumps(parent, ensure_ascii=False), encoding="utf-8")
            child_path.write_text(json.dumps(child, ensure_ascii=False), encoding="utf-8")
            refreshed = CORE._refresh_creator_batch_unlocked(parent, parent_path, root)
            self.assertEqual(refreshed["counts"]["completed"], 1)
            self.assertEqual(refreshed["counts"]["failed"], 0)
            self.assertEqual(json.loads(child_path.read_text(encoding="utf-8"))["status"], "completed")


class PreviewTests(unittest.TestCase):
    def test_profile_response_preserves_preview_candidates(self):
        aweme = {
            "video": {
                "dynamic_cover": {"url_list": ["https://p3.douyinpic.com/dynamic.webp"]},
                "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
            }
        }
        self.assertEqual(
            CORE.douyin_aweme_preview_urls(aweme),
            ["https://p3.douyinpic.com/dynamic.webp", "https://p3.douyinpic.com/cover.jpeg"],
        )

    def test_gallery_preview_candidates_support_image_post_info(self):
        aweme = {
            "aweme_id": "1234567890123456789",
            "image_post_info": {
                "images": [
                    {"display_image": {"url_list": ["https://p3.douyinpic.com/gallery.avif"]}}
                ]
            },
        }
        self.assertEqual(
            CORE.douyin_aweme_preview_urls(aweme),
            ["https://p3.douyinpic.com/gallery.avif"],
        )

    def test_video_inventory_preserves_direct_media_candidates(self):
        aweme = {
            "aweme_id": "1234567890123456789",
            "video": {"play_addr": {"url_list": ["https://v.douyinvod.com/source.mp4"]}},
        }
        media = CORE.douyin_aweme_media(aweme)
        self.assertEqual(media["kind"], "video")
        self.assertEqual(media["video_urls"], ["https://v.douyinvod.com/source.mp4"])

    def test_inventory_download_reuses_frozen_video_url(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "source": "https://www.douyin.com/video/1234567890123456789",
                "content_id": "1234567890123456789",
                "title": "frozen",
                "douyin_media": {
                    "content_id": "1234567890123456789",
                    "kind": "video",
                    "video_variants": [{"urls": ["https://v.douyinvod.com/source.mp4"], "clean_playback": True}],
                },
            }
            target = Path(tmp) / "work"
            measured = {"width": 1080, "height": 1920, "bitrate": 3000000, "fps": 30, "bytes": 100}

            def save_mock(_url, target, **_kwargs):
                target.write_bytes(b"video")

            with mock.patch.object(CORE, "download_public_https", side_effect=save_mock) as download, \
                    mock.patch.object(CORE, "ensure_video_readable"), \
                    mock.patch.object(CORE, "probe_video_file", return_value=measured), \
                    mock.patch.object(CORE, "fetch_douyin_aweme_detail", side_effect=CORE.ArchiveError("network_timeout", "offline")) as detail:
                info, output_path = CORE.download_douyin_inventory_fallback(manifest, target)
            download.assert_called_once()
            detail.assert_called_once()
            self.assertEqual(info["title"], "frozen")
            self.assertEqual(output_path.name, "source.mp4")
            self.assertEqual(info["_video_quality"]["height"], 1920)

    def test_preview_proxy_keeps_bytes_in_memory(self):
        import engine

        class Headers:
            @staticmethod
            def get_content_type():
                return "image/jpeg"

        class Response:
            headers = Headers()

            @staticmethod
            def geturl():
                return "https://p3.douyinpic.com/cover.jpeg"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read(_limit=-1):
                return b"\xff\xd8\xff" + b"x" * 600

        class Opener:
            @staticmethod
            def open(_url, timeout=0):
                return Response()

        item = {"id": "1234567890123456789", "preview_urls": ["https://p3.douyinpic.com/cover.jpeg"],
                "douyin_media": {"clean_source_revision": CORE.DOUYIN_CLEAN_REVISION}}
        with mock.patch.object(engine, "_http_opener_with_douyin_cookie", return_value=Opener()):
            result = engine.fetch_remote_preview(item)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "image/jpeg")

    def test_avif_preview_keeps_avif_extension(self):
        import engine

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = {"id": "1234567890123456789"}
            body = b"....ftypavif" + b"x" * 600
            with mock.patch.object(engine, "ARCHIVE_ROOT", root):
                cached = engine.cache_remote_preview(item, (body, "image/avif"))
            self.assertIsNotNone(cached)
            self.assertEqual(cached.suffix, ".avif")
            self.assertEqual(cached.read_bytes(), body)


class XiaohongshuTests(unittest.TestCase):
    def test_full_inventory_freezes_preview_urls_and_media_kind(self):
        url = (
            "https://www.xiaohongshu.com/user/profile/"
            "5d3cf500000000001602240f?xsec_token=test&xsec_source=pc_search"
        )
        payload = {
            "total": 1,
            "notes": [
                {
                    "note_id": "681e0077000000000303c0e9",
                    "display_title": "一条图文",
                    "type": "normal",
                    "xsec_token": "note-token",
                    "cover": {"url": "http://sns-webpic-qc.xhscdn.com/cover"},
                    "imageList": [{"urlPre": "https://sns-webpic-qc.xhscdn.com/full"}],
                    "user": {"nickname": "作者"},
                }
            ],
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload, ensure_ascii=False), stderr="")
        with mock.patch.object(CORE, "_redbook_command", return_value=["redbook"]), \
                mock.patch.object(CORE, "_firefox_xiaohongshu_cookie_file", return_value=None), \
                mock.patch.object(CORE.subprocess, "run", return_value=completed):
            creator, items = CORE._xiaohongshu_full_inventory(url, "firefox")
        self.assertTrue(creator["inventory_complete"])
        self.assertEqual(creator["inventory_backend"], "@lucasygu/redbook@0.8.2")
        self.assertEqual(items[0]["media_kind"], "image_gallery")
        self.assertEqual(items[0]["preview_urls"], [
            "http://sns-webpic-qc.xhscdn.com/cover",
            "https://sns-webpic-qc.xhscdn.com/full",
        ])


class SubmitTests(unittest.TestCase):
    def test_same_running_job_is_attached_instead_of_rejected(self):
        import server
        from http.server import HTTPServer

        class FakeRunner:
            def snapshot(self):
                return {"running": True, "job_id": "batch-active", "total": 2}

            def start(self, _job_id, _ids):  # pragma: no cover - should not be called
                raise AssertionError("same running job should attach")

        old_runner = server.RUNNER
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        server.RUNNER = FakeRunner()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            body = json.dumps({"job_id": "batch-active", "ids": ["1"]})
            conn.request("POST", "/api/download", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["resumed"])
        finally:
            server.RUNNER = old_runner
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


class FailureRetryTests(unittest.TestCase):
    def test_complete_on_disk_failed_item_is_not_listed_for_retry(self):
        import engine

        job_id = "batch-20260914T000000Z-eeeeeeee"
        child_id = "content-20260914T000000Z-ffffffff"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_path = root / "jobs" / job_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            out = root / "content" / "抖音" / "作品--1"
            out.mkdir(parents=True)
            video = out / "video.mp4"
            video.write_bytes(video_bytes())
            parent_path.parent.mkdir(parents=True)
            child_path.parent.mkdir(parents=True)
            parent = {"job_id": job_id, "kind": "creator_batch", "platform": "douyin", "selection": {"ids": ["1"]}, "inventory": {"items": [{"id": "1", "title": "作品", "media_kind": "video", "child_job_id": child_id, "result": "failed"}]}}
            child = {"job_id": child_id, "kind": "content", "platform": "douyin", "status": "failed", "output_dir": "content/抖音/作品--1", "douyin_media": {"kind": "video"}, "outputs": [output(root, video, "video")], "error": {"code": "missing_command", "message": "转写工具不可用"}}
            parent_path.write_text(json.dumps(parent, ensure_ascii=False), encoding="utf-8")
            child_path.write_text(json.dumps(child, ensure_ascii=False), encoding="utf-8")
            old_root = engine.ARCHIVE_ROOT
            try:
                engine.ARCHIVE_ROOT = root
                self.assertEqual(engine.failed_selection_details(job_id), [])
                disk = engine.selection_disk_status(job_id, ["1"])
                self.assertEqual(disk["ready"], 1)
                self.assertEqual(disk["failed"], 0)
            finally:
                engine.ARCHIVE_ROOT = old_root

    def test_failed_creator_items_are_listed_and_requeued_selectively(self):
        import engine

        job_id = "batch-20260914T000000Z-aaaaaaaa"
        child_id = "content-20260914T000000Z-bbbbbbbb"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_path = root / "jobs" / job_id / "manifest.json"
            child_path = root / "jobs" / child_id / "manifest.json"
            parent_path.parent.mkdir(parents=True)
            child_path.parent.mkdir(parents=True)
            parent = {
                "job_id": job_id,
                "kind": "creator_batch",
                "platform": "douyin",
                "status": "completed_with_failures",
                "selection": {"mode": "all", "ids": ["1", "2"]},
                "child_job_ids": [child_id],
                "inventory": {
                    "items": [
                        {
                            "id": "1",
                            "title": "下载失败的作品",
                            "published_at": "2026-09-14T00:00:00Z",
                            "url": "https://www.douyin.com/video/1",
                            "douyin_media": {"kind": "video"},
                            "child_job_id": child_id,
                            "result": "failed",
                            "error_code": "network_error",
                        },
                        {
                            "id": "2",
                            "title": "成功作品",
                            "douyin_media": {"kind": "video"},
                            "result": "completed",
                        },
                    ]
                },
            }
            child = {
                "job_id": child_id,
                "kind": "content",
                "platform": "douyin",
                "status": "failed",
                "error": {"code": "network_error", "message": "网络超时"},
            }
            parent_path.write_text(json.dumps(parent, ensure_ascii=False), encoding="utf-8")
            child_path.write_text(json.dumps(child, ensure_ascii=False), encoding="utf-8")
            old_root = engine.ARCHIVE_ROOT
            try:
                engine.ARCHIVE_ROOT = root
                details = engine.failed_selection_details(job_id)
                self.assertEqual([item["id"] for item in details], ["1"])
                self.assertEqual(details[0]["error_code"], "network_error")
                self.assertEqual(details[0]["error_message"], "网络超时")

                result = engine.retry_failed_selection(job_id, ["1", "2"])
                self.assertEqual(result["retried"], ["1"])
                self.assertEqual(result["ignored"], ["2"])
                requeued = json.loads(child_path.read_text(encoding="utf-8"))
                updated = json.loads(parent_path.read_text(encoding="utf-8"))
                self.assertEqual(requeued["status"], "queued")
                self.assertNotIn("error", requeued)
                self.assertEqual(updated["status"], "processing")
                self.assertEqual(updated["selection"]["mode"], "retry_failed")
                self.assertEqual(updated["selection"]["ids"], ["1"])
            finally:
                engine.ARCHIVE_ROOT = old_root

    def test_retry_endpoint_delegates_preparation_under_runner_lock(self):
        import server
        from http.server import HTTPServer

        class FakeRunner:
            def __init__(self):
                self.calls = []

            def snapshot(self):
                return {"running": False, "job_id": None}

            def start(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        old_runner = server.RUNNER
        old_retry = server.engine.retry_failed_selection
        runner = FakeRunner()
        server.RUNNER = runner
        server.engine.retry_failed_selection = lambda _job_id, _ids: {"retried": ["1"]}
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            body = json.dumps({"job_id": "batch-retry", "ids": ["1", "2"]})
            conn.request("POST", "/api/retry-failures", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["retried"], ["1", "2"])
            self.assertEqual(runner.calls[0][0], ("batch-retry", ["1", "2"]))
            self.assertTrue(runner.calls[0][1]["retry"])
            self.assertNotIn("prepared", runner.calls[0][1])
        finally:
            server.RUNNER = old_runner
            server.engine.retry_failed_selection = old_retry
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
