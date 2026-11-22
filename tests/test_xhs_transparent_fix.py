import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
CORE_PATH = PROJECT / "core" / "media_archive.py"
SPEC = importlib.util.spec_from_file_location("local_media_archive_xhs_fix", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CORE)


class XhsTransparentFixTests(unittest.TestCase):
    def test_windows_title_sanitizer_removes_reserved_filename_characters(self):
        value = CORE.safe_content_title('全世界最准麦克雷（*＾3＾）?<>|', 'fallback')
        self.assertNotRegex(value, r'[\\/:*?"<>|]')

    def test_redbook_note_normalizes_full_image_candidates(self):
        note = {
            "note_id": "681e0077000000000303c0e9",
            "type": "normal",
            "title": "图集",
            "desc": "说明",
            "image_list": [{
                "url_default": "http://sns-webpic-qc.xhscdn.com/a.webp",
                "url_pre": "http://sns-webpic-qc.xhscdn.com/b.webp",
                "live_photo": False,
            }],
        }
        info = CORE._redbook_note_info(note, "https://www.xiaohongshu.com/explore/681e0077000000000303c0e9")
        self.assertEqual(info["_xhs_note_type"], "normal")
        self.assertEqual(info["thumbnails"][0]["url"], "https://sns-webpic-qc.xhscdn.com/a.webp")
        self.assertEqual(len(info["thumbnails"][0]["urls"]), 2)

    def test_redbook_note_builds_no_resize_original_candidates(self):
        source = "https://sns-webpic-qc.xhscdn.com/202609142134/cache/notes_pre_post/1040g2sg31h8vpidd3s005n9suk05k90flj52dho!nd_dft_wlteh_webp_3"
        candidates = CORE._xhs_original_image_urls(source)
        self.assertIn(
            "https://sns-img-bd.xhscdn.com/notes_pre_post/1040g2sg31h8vpidd3s005n9suk05k90flj52dho",
            candidates,
        )
        self.assertNotIn("!nd_dft_wlteh_webp_3", " ".join(candidates))

    def test_image_child_uses_redbook_api_and_allows_empty_body(self):
        url = "https://www.xiaohongshu.com/explore/681e0077000000000303c0e9"
        note = {
            "note_id": "681e0077000000000303c0e9",
            "type": "normal",
            "title": "只有图片",
            "desc": "",
            "image_list": [{"url_default": "https://sns-webpic-qc.xhscdn.com/a.webp"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "jobs" / "content-20260914T000000Z-00000001"
            job.mkdir(parents=True)
            manifest_path = job / "manifest.json"
            manifest = {
                "job_id": job.name,
                "kind": "content",
                "status": "queued",
                "source": url,
                "platform": "xiaohongshu",
                "content_id": "681e0077000000000303c0e9",
                "outputs": [],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            from io import BytesIO
            from PIL import Image
            image = BytesIO()
            Image.new("RGB", (24, 24), "red").save(image, "WEBP")

            def fake_fetch(*_args, **_kwargs):
                return image.getvalue(), "image/webp", "https://sns-webpic-qc.xhscdn.com/a.webp"

            with mock.patch.object(CORE, "_redbook_read_note", return_value=note), mock.patch.object(
                CORE, "fetch_limited", side_effect=fake_fetch
            ):
                result = CORE.process_xiaohongshu(manifest, manifest_path, root)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["route"], "redbook_api")
            self.assertTrue((root / "content" / "小红书").exists())

    def test_video_child_uses_direct_stream_without_transparent_core(self):
        url = "https://www.xiaohongshu.com/explore/6aa3dbfa000000002900ca31"
        note = {
            "note_id": "6aa3dbfa000000002900ca31",
            "type": "video",
            "title": "视频",
            "desc": "说明",
            "image_list": [],
            "video": {"media": {"stream": {"h264": [{
                "height": 720,
                "master_url": "https://sns-video-v2.xhscdn.com/a.mp4",
                "backup_urls": [],
            }]}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "jobs" / "content-20260914T000000Z-00000002"
            job.mkdir(parents=True)
            manifest_path = job / "manifest.json"
            manifest = {
                "job_id": job.name,
                "kind": "content",
                "status": "queued",
                "source": url,
                "platform": "xiaohongshu",
                "content_id": "6aa3dbfa000000002900ca31",
                "outputs": [],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            def fake_download(_urls, target, _referer):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fake mp4" * 300)

            with mock.patch.object(CORE, "_redbook_read_note", return_value=note), mock.patch.object(
                CORE, "_download_xhs_video", side_effect=fake_download
            ), mock.patch.object(CORE, "ensure_video_readable"), mock.patch.object(
                CORE, "probe_video_file", return_value={"width": 480, "height": 720, "fps": 30, "bitrate": 1500000}
            ), mock.patch.object(
                CORE, "transcribe_content_video", return_value=[]
            ):
                result = CORE.process_xiaohongshu(manifest, manifest_path, root)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["route"], "redbook_api")
            self.assertTrue((root / "content" / "小红书").exists())


if __name__ == "__main__":
    unittest.main()
