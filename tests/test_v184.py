from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import engine


CORE = engine.get_mod()
MARKED = "https://p3-pc-sign.douyinpic.com/obj/item~tplv-dy-water-v2:account.jpeg"
CLEAN = "https://p3-pc-sign.douyinpic.com/obj/item~tplv-dy-aweme-images:q75.jpeg"


class CleanDouyinTests(unittest.TestCase):
    def test_only_watermarked_still_is_kept_in_inventory_but_never_downloaded(self):
        detail = {"aweme_id": "1234567890123456789", "images": [{"download_url_list": [MARKED], "url_list": [MARKED]}]}
        media = CORE.douyin_aweme_media(detail)
        self.assertEqual(media["item_count"], 1)
        self.assertTrue(media["items"][0]["has_still"])
        self.assertEqual(media["items"][0]["image_urls"], [])
        self.assertEqual(CORE.douyin_aweme_preview_urls(detail), [])
        with tempfile.TemporaryDirectory() as folder, patch.object(CORE, "download_public_https") as fetch:
            with self.assertRaisesRegex(CORE.ArchiveError, "无水印"):
                CORE.download_douyin_still([MARKED], Path(folder) / "image", "", (0, 0), allow_lower=True)
            fetch.assert_not_called()

    def test_download_formats_and_watermark_variants_are_excluded(self):
        shared = "https://v.douyinvod.com/secret/video/tos/cn/a/?br=90"
        mirror = "https://node.douyinvod.com/other/video/tos/cn/a/?br=90&tag=1"
        clean = "https://v.douyinvod.com/source.mp4"
        official = "https://www.douyin.com/aweme/v1/play/?video_id=v123456789&watermark=0"
        video = {"download_addr": {"url_list": [shared]}, "play_addr": {"uri": "v123456789", "url_list": [mirror, clean, official]}}
        choices = CORE.douyin_video_choices(video)
        accepted = {url for entry in choices for url in entry["urls"]}
        self.assertIn(clean, accepted)
        self.assertIn("https://www.douyin.com/aweme/v1/play/?video_id=v123456789&ratio=default&watermark=0", accepted)
        self.assertFalse({shared, mirror} & accepted)
        self.assertTrue(all(entry["clean_playback"] for entry in choices))
        self.assertEqual(CORE.douyin_clean_media_urls([official.replace("=0", "=1"), official]), [official])

    def test_preview_rejects_watermarked_redirect_and_reuses_only_clean_cache(self):
        clean2 = CLEAN.replace("item~", "other~")
        stream = BytesIO()
        Image.new("RGB", (30, 60), "white").save(stream, "JPEG")
        data = stream.getvalue()
        snapshot = ("douyin", "示例", "https://www.douyin.com/video/123", [
            {"id": "part-1", "image": {"image_urls": [MARKED, CLEAN, clean2]}}])
        with (patch.object(engine, "_media_snapshot", return_value=snapshot),
              patch.object(CORE, "fetch_limited", side_effect=[(data, "image/jpeg", MARKED),
                                                             (data, "image/jpeg", clean2)]) as fetch):
            result = engine.work_media_preview("job", "123", "part-1", False)
        self.assertEqual(result[1], "image/jpeg")
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [CLEAN, clean2])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / ".preview"
            legacy.mkdir()
            (legacy / "1234567890123456789.jpg").write_bytes(data)
            item = {"id": "1234567890123456789", "url": "https://www.douyin.com/video/1234567890123456789"}
            self.assertIsNone(engine.preview_cache_path(item, root=root))
            self.assertEqual(engine.cache_remote_preview(item, (data, "image/jpeg"), root=root).parent.name, "douyin-clean-v1")

    def test_smaller_download_keeps_archived_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "asset.jpg"
            Image.new("RGB", (60, 120), "black").save(old)
            temporary = root / "fresh.jpg"
            Image.new("RGB", (30, 60), "white").save(temporary)
            new, size, retained = CORE.commit_image(temporary, old, (30, 60))
            self.assertTrue(retained)
            self.assertEqual((new, size), (old, (60, 120)))
            self.assertFalse(temporary.exists())
