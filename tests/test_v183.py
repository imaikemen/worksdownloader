from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import engine


CORE = engine.get_mod()


def photo(size):
    output = BytesIO()
    Image.new("RGB", size, "orange").save(output, "JPEG")
    return output.getvalue()


class WorkMediaTests(unittest.TestCase):
    def test_douyin_live_placeholder_does_not_reject_matched_motion(self):
        choices = [{"width": 720, "height": 720, "bytes": 1200, "urls": ["square"]},
                   {"width": 576, "height": 768, "bytes": 1200, "bitrate": 800000, "urls": ["adaptive"]},
                   {"urls": ["default"], "inspect_first": True}]
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "live.mp4"

            def download(urls, path, origin):
                path.write_text(urls[0], encoding="utf-8")

            def probe(path):
                return {"width": 654, "height": 872, "bytes": path.stat().st_size}

            with patch.object(CORE, "probe_video_file", side_effect=probe):
                result = CORE.download_best_video(choices, target, "", download, reference_size=(654, 872))
                self.assertNotIn("non_highest", result)
                self.assertEqual(target.read_text("utf-8"), "default")
                choices[0].update(width=960, height=960, bitrate=2000000)
                with self.assertRaisesRegex(CORE.ArchiveError, "960×960"):
                    CORE.download_best_video(choices, target, "", download, reference_size=(654, 872))
                self.assertFalse(target.exists())
                result = CORE.download_best_video(choices, target, "", download, reference_size=(654, 872), allow_lower=True)
                self.assertTrue(result["non_highest"])

    def test_explicit_picture_quality_fallback_chooses_largest_working_variant(self):
        images = {"low": photo((240, 320)), "better": photo((480, 640))}
        with patch.object(CORE, "fetch_limited", side_effect=lambda url, **_: (images[url], "image/jpeg", url)):
            with self.assertRaisesRegex(CORE.ArchiveError, "480×640"):
                CORE.fetch_xhs_image([], list(images), (960, 1280), "")
            data, lower, size = CORE.fetch_xhs_image([], list(images), (960, 1280), "", allow_lower=True)
        self.assertTrue(lower)
        self.assertEqual(size, (480, 640))
        self.assertEqual(data[0], images["better"])

    def test_selected_second_picture_does_not_save_other_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "jobs/content-job/manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest = {"platform": "douyin", "content_id": "123", "source": "https://www.douyin.com/video/123",
                        "author_folder": "作者（123）", "direct_archive": True, "media_ids": ["part-2"]}
            parts = [{"id": f"part-{number}", "kind": "image", "index": number, "choices": [],
                      "image": {"image_urls": [f"image{number}"], "width": 40, "height": 50}}
                     for number in (1, 2, 3)]

            def still(urls, path, referer, expected, **kwargs):
                self.assertEqual(urls, ["image2"])
                path.write_bytes(photo(expected))
                return expected, ".jpg"

            with (patch.object(CORE, "work_media_parts", return_value=("精选", manifest["source"], parts)),
                  patch.object(CORE, "download_douyin_still", side_effect=still)):
                result = CORE.process_selected_media(manifest, manifest_path, root)
            folder = root / "作者（123）"
            self.assertEqual([p.name for p in folder.iterdir()], ["精选--123--图002.jpg"])
            self.assertTrue(CORE._child_output_files_exist(root, result))
            self.assertIsNone(CORE.existing_content(root, "douyin", "123"))

    def test_douyin_checks_full_photo_before_the_watermarked_web_copy(self):
        smaller = "https://p3-pc-sign.douyinpic.com/obj/file~tplv-dy-water.webp"
        larger = "https://p3-pc-sign.douyinpic.com/obj/file~tplv-dy-aweme-images.webp"
        downloads = []

        def download(url, target, **_):
            downloads.append(url)
            target.write_bytes(photo((120, 160) if url == larger else (60, 80)))

        with tempfile.TemporaryDirectory() as directory, patch.object(CORE, "download_public_https", side_effect=download):
            size, _ = CORE.download_douyin_still([smaller, larger], Path(directory) / "still", "", (60, 80))
        self.assertEqual(size, (120, 160))
        self.assertEqual(downloads, [larger])
