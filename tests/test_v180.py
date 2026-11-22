import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import download_history
import engine
import server


CORE = engine.get_mod()


class DesktopArchiveTests(unittest.TestCase):
    def test_historical_retry_keeps_authors_separate_and_leaves_old_archive_intact(self):
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            destination = base / "作品提取"
            original = base / "测试旧档案"
            entries = []
            fixtures = [
                ("douyin", "nameA", "12", "8432905563268412093", "https://www.douyin.com/user/MS4wLjABAAAAa"),
                ("xiaohongshu", "nameA", "12", "650e509400000000120056fc", "https://www.rednote.com/user/profile/635468cf0000000009000000"),
                ("douyin", "nameB", "13", "8432905563268412094", "https://www.douyin.com/user/MS4wLjABAAAAb"),
            ]
            for index, (platform, name, account, item_id, profile_url) in enumerate(fixtures):
                batch_id = f"batch-20261119T000000Z-{index:08d}"
                work_id = f"content-20261119T000000Z-{index:08d}"
                url = f"https://www.rednote.com/explore/{item_id}" if platform == "xiaohongshu" else f"https://www.douyin.com/video/{item_id}"
                item = {"id": item_id, "url": url, "child_job_id": work_id}
                record = {"author_url": profile_url + "?from=original", "platform": platform,
                          "creator": {"id": account, "name": name, "url": profile_url, "account": account},
                          "inventory": {"items": [item]}}
                CORE.write_json(original / "jobs" / batch_id / "manifest.json", record)
                entries.append({"platform": platform, "work_id": item_id, "title": "作品", "archive_root": str(original),
                                "child_job": work_id, "batch_job": batch_id if index != 2 else None})

            with patch.object(engine, "_DESTINATION", destination), patch.object(engine, "_EXPLICIT_ROOT", ""), \
                    patch.object(app_runtime, "CONFIG_PATH", base / "settings.json"), \
                    patch.object(engine, "known_archive_roots", return_value=[original]), patch.dict(engine.JOB_ROOTS, {}), \
                    patch.object(app_runtime, "DATA_DIR", base):
                groups, skipped = engine.history_retry_groups(entries)
                self.assertEqual(len(groups), 3)
                self.assertFalse(skipped)
                for group in groups:
                    job_id = engine.stage_history_retry(group)
                    manifest = engine.read_manifest(job_id)
                    prefix = "小红书" if group["platform"] == "xiaohongshu" else "抖音"
                    author = group["creator"]
                    folder = destination / prefix / f'{author["name"]}（{author["account"]}）'
                    self.assertEqual(engine.job_root(job_id) / manifest["author_folder"], folder)
                    self.assertTrue(manifest["direct_archive"])
                    self.assertEqual(manifest["author_url"], author["url"] + "?from=original")
                    self.assertIsNone(manifest["inventory"]["items"][0]["child_job_id"])
                self.assertFalse((original / "content").exists())

    def test_bulk_retry_reads_failure_rows_across_pages_and_excludes_success(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(app_runtime, "DATA_DIR", Path(scratch)):
            rows = [("douyin", str(i), "作品", "failure", str(i), "", "", "") for i in range(29)]
            rows.append(("douyin", "29", "成功作品", "success", "29", "", "", ""))
            download_history.save(rows)
            self.assertEqual(len(download_history.page()["failure"]["items"]), 24)
            self.assertEqual({row["work_id"] for row in download_history.failed_entries()}, {str(i) for i in range(29)})

    def test_bulk_download_progress_processes_multiple_groups_and_reports_unknown_author(self):
        runner = server.DownloadRunner()
        entries = [{"work_id": str(index)} for index in range(4)]
        runner.history_run = {"total": 4, "completed": 0, "failed": 0, "transferred_bytes": 0, "job_id": None}
        groups = [{"ids": ["0", "1"]}, {"ids": ["2"]}]

        def simulate(_job, ids, **_options):
            runner.state.update(completed=len(ids), transferred_bytes=100)
            return True

        with patch.object(engine, "history_retry_groups", return_value=(groups, entries[-1:])), \
                patch.object(engine, "stage_history_retry", side_effect=["one", "two"]), \
                patch.object(runner, "start", side_effect=simulate) as start:
            runner._run_history(entries)
        self.assertEqual(start.call_count, 2)
        state = runner.snapshot()
        self.assertEqual((state["completed"], state["failed"], state["total"]), (3, 1, 4))
        self.assertIn("1 件缺少可核对的作者清单", state["message"])
