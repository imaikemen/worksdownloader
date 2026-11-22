import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app_runtime
import engine

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parents[1]


class V194Tests(unittest.TestCase):
    def test_rednote_full_inventory_uses_fast_delay_and_budget_max_duration(self):
        captured_cmd = []
        note_id = "65506e64000000000202833f"
        payload = {
            "notes": [{
                "note_id": note_id,
                "display_title": "测试笔记",
                "type": "normal",
                "web_url": f"https://www.rednote.com/explore/{note_id}?xsec_token=abc",
            }],
            "profile": {"basic_info": {"nickname": "海外博主", "red_id": "rn123"}},
            "complete": False,
        }

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        budget = app_runtime.TimeBudget(app_runtime.CHECK_SECONDS)
        with patch.object(CORE, "_redbook_command", return_value=["redbook"]), \
                patch.object(CORE, "_firefox_xiaohongshu_cookie_file", return_value=None), \
                patch.object(CORE, "run_background_process", side_effect=fake_run):
            creator, items = budget.call(
                CORE._xiaohongshu_full_inventory,
                "https://www.rednote.com/user/profile/65506e64000000000202833f",
                "firefox",
            )

        self.assertEqual(app_runtime.CHECK_SECONDS, 60)
        self.assertIn("--delay", captured_cmd)
        self.assertEqual(captured_cmd[captured_cmd.index("--delay") + 1], "450")
        self.assertIn("--max-duration", captured_cmd)
        max_ms = int(captured_cmd[captured_cmd.index("--max-duration") + 1])
        self.assertTrue(45000 <= max_ms <= 57000, max_ms)
        self.assertFalse(creator["inventory_complete"])
        self.assertEqual(creator["inventory_status"], "budget_capped_via_redbook")
        self.assertEqual(len(items), 1)

    def test_v194_disclaimer_clean_ui_and_version(self):
        self.assertRegex(app_runtime.VERSION, r"^1\.\d+\.\d+$")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn(f"<title>作品鉴赏downloader V{app_runtime.VERSION}</title>", html)
        self.assertIn(f"<h1>作品鉴赏downloader V{app_runtime.VERSION}</h1>", html)
        self.assertIn("const CHECK_TIMEOUT_MS = 60000;", html)
        self.assertIn("盘点超时（60 秒）", html)
        self.assertIn("盘点时间取决于网络、作者作品数量等多方面因素，请耐心等待", html)
        self.assertNotIn("id=\"disclaimer-countdown\"", html)
        self.assertNotIn("秒后可选", html)
        self.assertNotIn("已可选择", html)
        self.assertIn("<span>我同意声明</span>", html)
        self.assertIn("<span>不同意声明</span>", html)
        self.assertNotIn("我同意声明（", html)
        self.assertNotIn("不同意并退出（", html)


if __name__ == "__main__":
    unittest.main()
