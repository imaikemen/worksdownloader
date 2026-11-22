from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import engine


CORE = engine.get_mod()


def jpeg(size):
    target = BytesIO()
    Image.new("RGB", size).save(target, "JPEG")
    return target.getvalue()


class QualityTests(unittest.TestCase):
    def test_rednote_reads_the_object_path_and_real_dimensions(self):
        thumb = "https://sns-web-i10.rednotecdn.com/202610261049/6fd352312b11cd8f6b258eecff22e17e/notes_pre_post/photo!nd_dft_wlteh_webp_3"
        original = "https://ci.xiaohongshu.com/notes_pre_post/photo?imageView2/2/w/0/format/jpg/v3"
        choices = CORE._xhs_original_image_urls(thumb, "photo")
        self.assertIn(original, choices)
        low = jpeg((1080, 1919))
        high = jpeg((2161, 3840))
        with patch.object(CORE, "fetch_limited", side_effect=lambda url, **_: (high if url == original else low, "image/jpeg", url)):
            fetched, preview, size = CORE.fetch_xhs_image(choices, [thumb], (2161, 3840), "https://www.rednote.com/")
        self.assertFalse(preview)
        self.assertEqual(size, (2161, 3840))
        self.assertEqual(fetched[0], high)

    def test_known_large_photo_never_succeeds_as_small_preview(self):
        low = jpeg((1080, 1919))
        with patch.object(CORE, "fetch_limited", return_value=(low, "image/jpeg", "public-cdn")):
            with self.assertRaises(CORE.ArchiveError) as result:
                CORE.fetch_xhs_image(["first"], ["preview"], (2161, 3840), "https://www.rednote.com/")
        self.assertEqual(result.exception.code, "image_resolution_unavailable")

    def test_video_uses_measured_quality_when_default_is_smaller(self):
        low = {"width": 720, "height": 1280, "bitrate": 9800000, "fps": 60}
        high = {"width": 1440, "height": 2560, "bitrate": 1800000, "fps": 60}
        choices = [{"urls": ["default"], "inspect_first": True}, {"urls": ["adaptive"], **high}]
        with tempfile.TemporaryDirectory() as scratch, patch.object(CORE, "probe_video_file", side_effect=[low, high]):
            target = Path(scratch) / "video.mp4"

            def download(urls, path, _referer):
                path.write_text(urls[0], encoding="utf-8")

            actual = CORE.download_best_video(choices, target, "", download)
            self.assertEqual(actual["width"], 1440)
            self.assertEqual(target.read_text(encoding="utf-8"), "adaptive")

    def test_download_selection_can_refresh_older_completed_work(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            job = root / "jobs" / "content-20261119T000000Z-12345678" / "manifest.json"
            doc = {"job_id": job.parent.name, "platform": "douyin", "content_id": "1234567890123456789",
                   "status": "completed", "content_type": "video", "work_file_prefix": "示例--1234567890123456789"}
            CORE.write_json(job, doc)
            with patch.object(CORE, "_child_output_files_exist", return_value=True):
                new = CORE.submit_content("https://www.douyin.com/video/1234567890123456789", root, refresh_quality=True)
            self.assertNotEqual(new["job_id"], job.parent.name)
            self.assertEqual(CORE.read_json_if_valid(root / new["manifest"])["work_file_prefix"], doc["work_file_prefix"])
            self.assertEqual(CORE.read_json_if_valid(job), doc)
