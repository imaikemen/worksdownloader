import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine


CORE = engine.get_mod()
PROFILE = "https://www.douyin.com/user/example?from_tab_name=main"


class DouyinBrowserSessionTests(unittest.TestCase):
    def test_inventory_uses_current_login_even_when_a_cache_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "douyin.txt"
            saved.touch()
            fresh = object()
            with (
                patch.object(engine, "get_archive_root", return_value=root),
                patch.object(engine, "prepare_archive_root"),
                patch.object(engine, "inventory_view", return_value={"items": []}),
                patch.object(CORE, "platform_cookie_jar", return_value=saved),
                patch.object(CORE, "extract_platform_browser_cookies", return_value=fresh) as extract,
                patch.object(CORE, "inspect_creator", return_value={"ok": True, "job_id": "test-v154"}) as inspect,
                patch.object(CORE, "save_platform_cookies") as save,
                patch.dict(engine.JOB_ROOTS),
            ):
                result = engine.inspect_creator(PROFILE, "firefox")
            self.assertTrue(result["ok"])
            extract.assert_called_once_with("douyin", "firefox")
            inspect.assert_called_once_with(PROFILE, root, douyin_jar=fresh)
            save.assert_called_once_with("douyin", fresh)

    def test_empty_response_does_not_replace_saved_session(self):
        error = CORE.ArchiveError("douyin_empty_response", "HTTP 200", 69)
        with (
            patch.object(CORE, "extract_platform_browser_cookies", return_value=object()),
            patch.object(CORE, "inspect_creator", side_effect=error) as inspect,
            patch.object(CORE, "save_platform_cookies") as save,
        ):
            with self.assertRaises(CORE.ArchiveError) as raised:
                engine._inspect_douyin_with_browser(CORE, PROFILE, Path.cwd(), "firefox")
        self.assertEqual(raised.exception.code, "douyin_empty_response")
        inspect.assert_called_once()
        save.assert_not_called()

    def test_saved_session_is_used_when_browser_cannot_be_read(self):
        failure = CORE.ArchiveError("cookie_import_failed", "读取失败", 69)
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "douyin.txt"
            saved.touch()
            with (
                patch.object(CORE, "platform_cookie_jar", return_value=saved),
                patch.object(CORE, "extract_platform_browser_cookies", side_effect=failure),
                patch.object(CORE, "inspect_creator", return_value={"ok": True}) as inspect,
            ):
                result = engine._inspect_douyin_with_browser(CORE, PROFILE, saved.parent, "firefox")
        self.assertTrue(result["ok"])
        inspect.assert_called_once_with(PROFILE, saved.parent)
