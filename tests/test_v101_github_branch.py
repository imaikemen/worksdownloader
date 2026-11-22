import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import download_history
import engine
import favorite_authors

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parents[1]


class V101GitHubBranchTests(unittest.TestCase):
    def test_app_branding_and_version(self):
        self.assertEqual(app_runtime.APP_NAME, "作品鉴赏downloader")
        self.assertEqual(app_runtime.VERSION, "1.0.1")
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertIn("<title>作品鉴赏downloader V1.0.1</title>", html)
        self.assertIn("<h1>作品鉴赏downloader V1.0.1</h1>", html)
        self.assertIn('id="first-path-dialog"', html)

    def test_bilibili_and_x_removed_from_backend_and_ui(self):
        expected_platforms = {"douyin", "xiaohongshu", "weibo"}
        self.assertEqual(engine.PLATFORMS, expected_platforms)
        self.assertEqual(set(engine.AUTHOR_PLATFORMS.keys()), expected_platforms)
        self.assertEqual(favorite_authors.PLATFORMS, expected_platforms)
        self.assertEqual(download_history.PLATFORMS, expected_platforms)

        self.assertTrue((PROJECT / "core" / "media_archive.py").is_file())
        self.assertFalse((PROJECT / "wechat-archive").exists())

        for unsupported in (
            "https://x.com/demo_creator",
            "https://twitter.com/demo_creator/status/1234567890",
            "https://space.bilibili.com/946974",
            "https://www.bilibili.com/video/BV1xx411c7mD/",
            "https://b23.tv/abc123",
            "https://mp.weixin.qq.com/s/demo",
            "https://channels.weixin.qq.com/demo",
        ):
            with self.assertRaises(CORE.ArchiveError) as ctx:
                CORE.submission_url(unsupported)
            self.assertEqual(ctx.exception.code, "unsupported_url", unsupported)

        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertNotIn('<option value="x">', html)
        self.assertNotIn('<option value="bilibili">', html)
        self.assertNotIn('id="btn-x-continue-inspect"', html)
        self.assertNotIn('id="x-network-float"', html)
        self.assertNotIn("/api/inspect-continue", html)

    def test_default_download_path_uses_drive_worksdownloader_not_personal_path(self):
        default_dir = engine._DEFAULT_DOWNLOAD_DIR.resolve()
        self.assertEqual(default_dir.name, "WorksDownloader")
        if sys.platform == "win32":
            self.assertEqual(default_dir.parent, Path(default_dir.anchor))
            self.assertNotIn("\\Users\\", str(default_dir))
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "settings.json"
            with patch.object(engine, "_EXPLICIT_ROOT", ""), \
                    patch.object(app_runtime, "CONFIG_PATH", cfg), \
                    patch.object(engine, "_CONFIG_PATH", cfg), \
                    patch.object(engine, "_DESTINATION", default_dir):
                self.assertFalse(engine.is_destination_configured())
                self.assertEqual(engine.get_destination_base(), default_dir)
                self.assertEqual(set(engine.platform_paths_for_base().keys()), {"douyin", "xiaohongshu", "weibo"})
        html = (PROJECT / "index.html").read_text("utf-8")
        self.assertNotIn("drive + '作品提取'", html)
        self.assertIn("drive + 'WorksDownloader'", html)

    def test_core_self_check_validates_only_supported_platforms(self):
        with tempfile.TemporaryDirectory() as td:
            fake_yt = Path(td) / "yt_dlp"
            fake_yt.mkdir()
            (fake_yt / "__init__.py").write_text("class YoutubeDL:\n    pass\n", encoding="utf-8")
            with patch.object(CORE, "transparent_core_root", return_value=Path(td)):
                res = CORE.self_check(Path(td) / "archive")
        self.assertTrue(res["ok"])
        self.assertEqual(res["supported_platforms"], ["douyin", "xiaohongshu", "weibo"])
        self.assertTrue(res["enabled"])

    def test_privacy_and_compliance_audit_passes(self):
        audit_script = PROJECT / "scripts" / "privacy_audit.py"
        self.assertTrue(audit_script.is_file())
        res = subprocess.run([sys.executable, str(audit_script)], cwd=str(PROJECT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
