import hashlib
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

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parent.parent


def make_image(size, fmt="WEBP"):
    buf = io.BytesIO()
    Image.new("RGB", size, (180, 90, 120)).save(buf, format=fmt)
    return buf.getvalue()


class V189XhsCloudOriginalTests(unittest.TestCase):
    def test_cloud_capped_xhs_raw_object_verified_by_imageinfo_downloads_as_original(self):
        raw_webp = make_image((1440, 2560), "WEBP")
        jpeg_render = make_image((1440, 2560), "JPEG")
        preview_webp = make_image((1080, 1920), "WEBP")
        md5 = hashlib.md5(raw_webp).hexdigest()
        fid = "notes_pre_post/1040g3k831esv15o4mm704a7f2fsd2khhl9cu8eg"
        ci_raw = f"https://ci.xiaohongshu.com/{fid}"
        ci_jpg = ci_raw + "?imageView2/2/w/0/format/jpg/v3"
        xhscdn_raw = f"https://sns-img-bd.xhscdn.com/{fid}"
        preview = f"https://sns-web-i10.rednotecdn.com/202611221434/sig/{fid}!nd_dft_wlteh_webp_3"
        seen_referers = {}

        def fake_fetch(url, **kwargs):
            seen_referers[url] = (kwargs.get("headers") or {}).get("Referer")
            if url == f"{ci_raw}?imageInfo":
                payload = json.dumps({"width": "1440", "height": "2560", "size": str(len(raw_webp)), "md5": md5}).encode()
                return payload, "application/json", url
            if url in {ci_raw, xhscdn_raw}:
                return raw_webp, "image/webp", url
            if url == ci_jpg:
                return jpeg_render, "image/jpeg", url
            if url == preview:
                return preview_webp, "image/webp", url
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(CORE, "fetch_limited", side_effect=fake_fetch):
            fetched, using_fallback, size = CORE.fetch_xhs_image(
                [ci_raw, ci_jpg, xhscdn_raw],
                [preview],
                (2131, 3788),
                "https://www.rednote.com/",
                allow_lower=False,
            )

        self.assertFalse(using_fallback)
        self.assertEqual(size, (1440, 2560))
        self.assertEqual(fetched[0], raw_webp)
        self.assertEqual(CORE.xhs_image_source(fetched[2]), "cdn_object")
        self.assertEqual(seen_referers[xhscdn_raw], "https://www.xiaohongshu.com/")
        self.assertEqual(seen_referers[ci_raw], "https://www.xiaohongshu.com/")
        self.assertEqual(seen_referers[preview], "https://www.rednote.com/")

    def test_finalize_xhs_marks_cloud_original_as_verified_original_quality(self):
        raw_webp = make_image((1440, 2560), "WEBP")
        md5 = hashlib.md5(raw_webp).hexdigest()
        fid = "notes_pre_post/1040g3k831esv15o4mm704a7f2fsd2khhl9cu8eg"
        ci_raw = f"https://ci.xiaohongshu.com/{fid}"

        def fake_fetch(url, **kwargs):
            if url == f"{ci_raw}?imageInfo":
                payload = json.dumps({"width": "1440", "height": "2560", "size": str(len(raw_webp)), "md5": md5}).encode()
                return payload, "application/json", url
            return raw_webp, "image/webp", ci_raw

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = root / "jobs" / "content-20261122T065000Z-11112222"
            job_dir.mkdir(parents=True)
            manifest_path = job_dir / "manifest.json"
            manifest = {
                "job_id": job_dir.name,
                "platform": "xiaohongshu",
                "source": "https://www.rednote.com/explore/67d027470000000029036c93",
                "content_id": "67d027470000000029036c93",
                "author_folder": "islasweety（637778336）",
                "direct_archive": True,
            }
            info = {
                "id": "67d027470000000029036c93",
                "title": "随手拍",
                "thumbnails": [{
                    "url": ci_raw,
                    "urls": [ci_raw],
                    "original_urls": [ci_raw],
                    "width": 2131,
                    "height": 3788,
                }],
            }
            with patch.object(CORE, "fetch_limited", side_effect=fake_fetch):
                result = CORE.finalize_xiaohongshu_image_text(manifest, manifest_path, root, info)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["image_quality"], "original_cdn")
            self.assertFalse(result["non_highest"])
            self.assertTrue(result["outputs"][0]["source_match"])
            row = download_history.from_manifest(root, result)
            self.assertIn("原画质", row[7])

    def test_genuine_lower_res_image_still_rejected_when_imageinfo_disagrees(self):
        low_webp = make_image((1080, 1920), "WEBP")
        fid = "notes_pre_post/sample-low"
        ci_raw = f"https://ci.xiaohongshu.com/{fid}"

        def fake_fetch(url, **kwargs):
            if url == f"{ci_raw}?imageInfo":
                payload = json.dumps({"width": "2131", "height": "3788", "size": "999999", "md5": "0" * 32}).encode()
                return payload, "application/json", url
            return low_webp, "image/webp", ci_raw

        with patch.object(CORE, "fetch_limited", side_effect=fake_fetch):
            with self.assertRaises(CORE.ArchiveError) as raised:
                CORE.fetch_xhs_image([ci_raw], [], (2131, 3788), "https://www.rednote.com/", allow_lower=False)
        self.assertEqual(raised.exception.code, "image_resolution_unavailable")

    def test_v189_version_and_favorite_list_ui_structure(self):
        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn(f"<h1>作品鉴赏downloader V{app_runtime.VERSION}</h1>", html)
        self.assertIn('id="favorite-authors" class="favorites-list"', html)
        self.assertIn("overflow-y:auto", html)
        # Ensure .favorite-update is inline rather than position:absolute overlapping IDs
        update_css = next(line for line in html.splitlines() if ".favorite-update{" in line)
        self.assertIn("display:inline-flex", update_css)
        self.assertNotIn("position:absolute", update_css)


if __name__ == "__main__":
    unittest.main()
