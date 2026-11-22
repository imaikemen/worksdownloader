import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import engine

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parents[1]


class V192Tests(unittest.TestCase):
    def test_weibo_is_own_original_status_excludes_likes_reposts_and_other_users(self):
        uid = "5838703552"
        own_post = {
            "mid": "5367407885156501",
            "user": {"idstr": uid, "screen_name": "草莓不吃柚子"},
            "text_raw": "毛茸茸的冬天❄️",
        }
        liked_other_post = {
            "mid": "5367401231158796",
            "user": {"idstr": "5651488956", "screen_name": "小狗不长黑眼圈"},
            "title": {"text": "她11-20赞过的微博(13)"},
            "text_raw": "北京的阳光到底为什么这么清透！！",
        }
        liked_without_user = {
            "mid": "5360920075173996",
            "title": {"text": "她11-3赞过的微博(5)"},
            "text_raw": "动起来的视频感受下",
        }
        repost = {
            "mid": "5360000000000001",
            "user": {"idstr": uid, "screen_name": "草莓不吃柚子"},
            "retweeted_status": {"mid": "111", "text_raw": "别人发的"},
            "text_raw": "转发微博",
        }
        self.assertTrue(CORE._weibo_is_own_original_status(own_post, uid))
        self.assertFalse(CORE._weibo_is_own_original_status(liked_other_post, uid))
        self.assertFalse(CORE._weibo_is_own_original_status(liked_without_user, uid))
        self.assertFalse(CORE._weibo_is_own_original_status(repost, uid))

    def test_weibo_creator_inventory_excludes_liked_and_non_author_posts(self):
        uid = "5838703552"
        seen_urls = []

        def fake_api_get(_opener, _xsrf, url, _referer, _label):
            seen_urls.append(url)
            if "/profile/info" in url:
                return {"ok": 1, "data": {"user": {"idstr": uid, "screen_name": "草莓不吃柚子"}}}
            if "getImageWall" in url:
                return {"ok": 1, "data": {
                    "list": [{"mid": "5367407885156501", "pid": "p_own_1", "type": "pic", "timeline_year": "2026", "timeline_month": "11"}],
                    "since_id": "0",
                }}
            if "getWaterFallContent" in url:
                return {"ok": 1, "data": {
                    "list": [
                        {
                            "mid": "5257299498632284", "mblogid": "OwnVid",
                            "user": {"idstr": uid}, "text_raw": "自己的视频",
                            "created_at": "Wed Nov 11 10:00:00 +0800 2026",
                            "page_info": {"object_type": "video", "media_info": {"mp4_hd_url": "https://f.video.weibocdn.com/own.mp4"}},
                        },
                        {
                            "mid": "5360920075173996", "mblogid": "OtherVid",
                            "user": {"idstr": "1774037114"}, "title": {"text": "她11-3赞过的微博(5)"},
                            "text_raw": "别人发的视频",
                            "page_info": {"object_type": "video", "media_info": {"mp4_hd_url": "https://f.video.weibocdn.com/other.mp4"}},
                        },
                    ],
                    "next_cursor": "0",
                }}
            if "mymblog" in url:
                return {"ok": 1, "data": {"list": [
                    {
                        "mid": "5367407885156501", "mblogid": "OwnPost",
                        "user": {"idstr": uid}, "text_raw": "毛茸茸的冬天❄️",
                        "created_at": "Fri Nov 20 19:13:43 +0800 2026",
                        "pic_ids": ["p_own_1"],
                        "pic_infos": {"p_own_1": {"type": "pic", "largest": {"url": "https://wx1.sinaimg.cn/large/p_own_1.jpg", "width": 1000, "height": 1500}}},
                    },
                    {
                        "mid": "5367401231158796", "mblogid": "LikedPost",
                        "user": {"idstr": "5651488956"},
                        "title": {"text": "她11-20赞过的微博(13)"},
                        "text_raw": "北京的阳光到底为什么这么清透！！",
                        "created_at": "Fri Nov 20 18:47:00 +0800 2026",
                        "pic_ids": ["p_other_1"],
                        "pic_infos": {"p_other_1": {"type": "livephoto", "video": "https://video.weibo.com/media/play?livephoto=1",
                                                    "largest": {"url": "https://wx1.sinaimg.cn/large/p_other_1.jpg", "width": 1000, "height": 1500}}},
                    },
                ]}}
            raise AssertionError(url)

        with patch.object(CORE, "_weibo_opener", return_value=(None, CORE.http.cookiejar.CookieJar(), "")), \
                patch.object(CORE, "_weibo_api_get", side_effect=fake_api_get):
            creator, items = CORE.weibo_creator_inventory(f"https://weibo.com/u/{uid}")

        self.assertEqual(creator["name"], "草莓不吃柚子")
        ids = [it["id"] for it in items]
        self.assertEqual(ids, ["5367407885156501", "5257299498632284"])
        self.assertNotIn("5367401231158796", ids)
        self.assertNotIn("5360920075173996", ids)
        self.assertTrue(any("mymblog" in u and "feature=1" in u for u in seen_urls))

    def test_v192_search_and_date_picker_ui_markers(self):
        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn(f"<h1>作品鉴赏downloader V{app_runtime.VERSION}</h1>", html)
        self.assertIn('id="favorite-search"', html)
        self.assertIn('id="favorite-search-results"', html)
        self.assertIn('id="work-search"', html)
        self.assertIn('id="work-search-results"', html)
        self.assertIn('id="btn-work-date"', html)
        self.assertIn('id="work-date-year"', html)
        self.assertIn('id="work-date-month"', html)
        self.assertIn('id="work-date-calendar"', html)
        self.assertIn("animation:work-flash-yellow 0.65s ease-in-out 2", html)


if __name__ == "__main__":
    unittest.main()
