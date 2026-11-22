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
PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_jpeg(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (210, 130, 80)).save(buf, format="JPEG")
    return buf.getvalue()


class V190WeiboTests(unittest.TestCase):
    def test_weibo_url_detection_and_normalization(self):
        for raw, expected in [
            ("https://weibo.com/u/2132012180", "https://weibo.com/u/2132012180"),
            ("https://www.weibo.com/u/5838703552?tabtype=album", "https://weibo.com/u/5838703552"),
            ("https://m.weibo.cn/u/5884985024", "https://weibo.com/u/5884985024"),
            ("https://m.weibo.cn/profile/5884985024", "https://weibo.com/u/5884985024"),
            ("https://weibo.com/p/1005052132012180/home", "https://weibo.com/u/2132012180"),
        ]:
            url, platform = CORE.submission_url(raw)
            self.assertEqual((url, platform), (expected, "weibo"))
            self.assertTrue(CORE.is_creator_profile_url(url, platform))

        post_url, platform = CORE.submission_url("https://weibo.com/2132012180/Rosnj7MrL")
        self.assertEqual(platform, "weibo")
        self.assertFalse(CORE.is_creator_profile_url(post_url, platform))
        self.assertEqual(CORE.submission_content_id("weibo", post_url), "Rosnj7MrL")

    def test_weibo_creator_inventory_merges_album_video_and_enrichment(self):
        uid = "2132012180"
        profile_resp = {"ok": 1, "data": {"user": {"id": int(uid), "screen_name": "Realllyeol", "domain": "", "avatar_large": "https://tvax1.sinaimg.cn/av.jpg"}}}
        wall_resp = {
            "ok": 1,
            "data": {
                "since_id": "0",
                "list": [
                    {"pid": "pid_a1", "mid": "5358289491854529", "type": "livephoto", "timeline_year": "2026", "timeline_month": "10",
                     "video": "https://video.weibo.com/media/play?livephoto=https%3A%2F%2Fus.sinaimg.cn%2Fclip.mov"},
                    {"pid": "pid_b1", "mid": "5342466545025130", "type": "pic", "timeline_year": "2026", "timeline_month": "09"},
                ],
            },
        }
        wf_resp = {
            "ok": 1,
            "data": {
                "next_cursor": "-1",
                "list": [{
                    "mid": "5366360980856945",
                    "mblogid": "RrQlQ3AVH",
                    "text_raw": "测试视频标题 http://t.cn/AXTw8Nzd",
                    "created_at": "Tue Nov 17 21:53:43 +0800 2026",
                    "page_info": {
                        "object_type": "video",
                        "page_pic": "https://wx4.sinaimg.cn/orj480/cover.jpg",
                        "media_info": {
                            "playback_list": [
                                {"meta": {"quality_label": "1080p"}, "play_info": {"mime": "video/mp4", "width": 1080, "height": 1920, "bitrate": 1300000, "size": 680000, "url": "https://f.video.weibocdn.com/o0/v1080.mp4"}},
                                {"meta": {}, "play_info": {"mime": "image/jpeg", "width": 320, "height": 180, "url": "https://wx2.sinaimg.cn/large/sprite.jpg"}},
                            ]
                        },
                    },
                }],
            },
        }
        mblog_resp = {
            "ok": 1,
            "data": {
                "since_id": "",
                "list": [{
                    "mid": "5358289491854529",
                    "mblogid": "Rosnj7MrL",
                    "text_raw": "海岛实况图 \u200b",
                    "created_at": "Mon Oct 26 15:20:30 +0800 2026",
                    "pic_ids": ["pid_a1"],
                    "pic_infos": {
                        "pid_a1": {
                            "type": "livephoto",
                            "largest": {"url": "https://wx1.sinaimg.cn/large/pid_a1.jpg", "width": 2048, "height": 3640},
                            "bmiddle": {"url": "https://wx1.sinaimg.cn/wap360/pid_a1.jpg", "width": 202, "height": 360},
                            "video": "https://video.weibo.com/media/play?livephoto=https%3A%2F%2Fus.sinaimg.cn%2Fclip.mov&Expires=1799999999&ssig=abc",
                        }
                    },
                }],
            },
        }

        def fake_api(_opener, _xsrf, url, _ref, _label):
            if "profile/info" in url:
                return profile_resp
            if "getImageWall" in url:
                return wall_resp
            if "getWaterFallContent" in url:
                return wf_resp
            if "mymblog" in url:
                return mblog_resp
            raise AssertionError(url)

        with (
            patch.object(CORE, "_weibo_opener", return_value=(None, None, "")),
            patch.object(CORE, "_weibo_api_get", side_effect=fake_api),
        ):
            creator, items = CORE.weibo_creator_inventory(f"https://weibo.com/u/{uid}")

        self.assertEqual(creator["name"], "Realllyeol")
        self.assertTrue(creator["inventory_complete"])
        self.assertEqual([it["id"] for it in items], ["5366360980856945", "5358289491854529", "5342466545025130"])
        self.assertEqual(items[0]["media_kind"], "video")
        self.assertEqual(len(items[0]["weibo_media"]["video_variants"]), 1)  # sprite jpg filtered out
        self.assertEqual(items[1]["media_kind"], "live_photo")
        self.assertEqual(items[1]["title"], "海岛实况图")
        self.assertEqual(items[2]["media_kind"], "image_gallery")
        self.assertIn("2026-09", items[2]["title"])

    def test_process_weibo_livephoto_and_video_works_end_to_end(self):
        large_jpg = make_jpeg((160, 240))
        mp4_bytes = (FIXTURES / "valid.mp4").read_bytes()

        def fake_dl(url, target, _ref, *, max_bytes, cookie_jar=None):
            if ".jpg" in url:
                target.write_bytes(large_jpg)
            else:
                target.write_bytes(mp4_bytes)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 1. Live photo post
            job_dir = root / "jobs" / "content-20261122T070000Z-aaaa1111"
            job_dir.mkdir(parents=True)
            mpath = job_dir / "manifest.json"
            status_lp = {
                "mid": "5358289491854529",
                "mblogid": "Rosnj7MrL",
                "text_raw": "海岛随手拍",
                "user": {"id": 2132012180},
                "pic_ids": ["pid1"],
                "pic_infos": {
                    "pid1": {
                        "type": "livephoto",
                        "largest": {"url": "https://wx1.sinaimg.cn/large/pid1.jpg", "width": 120, "height": 180},
                        "video": "https://video.weibo.com/media/play?livephoto=https%3A%2F%2Fus.sinaimg.cn%2Fclip.mov&Expires=1799999999&ssig=ok",
                    }
                },
            }
            manifest = {
                "job_id": job_dir.name,
                "platform": "weibo",
                "source": "https://weibo.com/2132012180/Rosnj7MrL",
                "content_id": "5358289491854529",
                "author_folder": "Realllyeol（2132012180）",
                "direct_archive": True,
            }
            CORE.write_json(mpath, manifest)

            with (
                patch.object(CORE, "_weibo_load_cookie_jar", return_value=object()),
                patch.object(CORE, "fetch_weibo_status", return_value=status_lp),
                patch.object(CORE, "_download_weibo_media_url", side_effect=fake_dl),
            ):
                result = CORE.process_weibo_work(manifest, mpath, root)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["outputs"]), 2)
            self.assertTrue(CORE._child_output_files_exist(root, result))
            row = download_history.from_manifest(root, result)
            self.assertIn("原画质", row[7])

            # 2. Video post
            vdir = root / "jobs" / "content-20261122T070001Z-bbbb2222"
            vdir.mkdir(parents=True)
            vpath = vdir / "manifest.json"
            status_v = {
                "mid": "5366360980856945",
                "mblogid": "RrQlQ3AVH",
                "text_raw": "微博测试视频",
                "user": {"id": 5884985024},
                "page_info": {
                    "object_type": "video",
                    "media_info": {
                        "playback_list": [
                            {"meta": {"quality_label": "1080p"}, "play_info": {"mime": "video/mp4", "width": 64, "height": 64, "bitrate": 100000, "size": len(mp4_bytes), "url": "https://f.video.weibocdn.com/o0/v1080.mp4"}},
                        ]
                    },
                },
            }
            vmanifest = {
                "job_id": vdir.name,
                "platform": "weibo",
                "source": "https://weibo.com/5884985024/RrQlQ3AVH",
                "content_id": "5366360980856945",
                "author_folder": "盐城欧阳娜娜（5884985024）",
                "direct_archive": True,
            }
            CORE.write_json(vpath, vmanifest)
            with (
                patch.object(CORE, "_weibo_load_cookie_jar", return_value=object()),
                patch.object(CORE, "fetch_weibo_status", return_value=status_v),
                patch.object(CORE, "_download_weibo_media_url", side_effect=fake_dl),
            ):
                vresult = CORE.process_weibo_work(vmanifest, vpath, root)
            self.assertEqual(vresult["status"], "completed")
            self.assertEqual(vresult["outputs"][0]["role"], "video")
            self.assertTrue(CORE._child_output_files_exist(root, vresult))

    def test_weibo_favorite_author_and_v190_ui_markers(self):
        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        self.assertIn("weibo", favorite_authors.PLATFORMS)
        self.assertIn("weibo", engine.AUTHOR_PLATFORMS)
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn('<option value="weibo">微博</option>', html)
        self.assertIn(".favorite-platform.weibo", html)


if __name__ == "__main__":
    unittest.main()
