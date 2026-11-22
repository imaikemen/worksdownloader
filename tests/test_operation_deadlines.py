import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app_runtime
import download_history
import engine
import favorite_authors
import server


class HistoryAttemptTests(unittest.TestCase):
    def test_two_quality_failures_are_visible_to_the_author_until_lower_quality_succeeds(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app_runtime, "DATA_DIR", Path(directory)):
            root = Path(directory)
            job = "batch-20261120T160001Z-aabbccdd"
            author = ("douyin", "creator")
            works = ["7080062408722631976", "7069595857628237056"]
            items = [{"id": work, "child_job_id": f"content-20261120T160005Z-{index:08x}"}
                     for index, work in enumerate(works)]
            batch = {"platform": author[0], "creator": {"id": author[1]}, "inventory": {"items": items}}

            for item in items:
                child = {"platform": "douyin", "submission_id": "old-internal-id", "status": "failed",
                         "updated_at": "2026-11-20T16:00:05Z",
                         "error": {"code": "image_resolution_unavailable", "message": "已跳过低清晰度图片"}}
                app_runtime.atomic_json(root / "jobs" / item["child_job_id"] / "manifest.json", child)
            previously_downloaded = {"platform": "douyin", "status": "completed",
                                    "completed_at": "2026-11-20T15:00:00Z", "output_dir": "作者目录"}
            prior = download_history.from_manifest(root, previously_downloaded, work_id=works[0])
            download_history.save([prior])

            with patch.object(engine, "job_root", return_value=root), patch.object(engine, "read_manifest", return_value=batch):
                disk = engine.selection_disk_status(job, works)
            self.assertEqual(disk["failed"], 2)
            download_history.save(disk["history"], author=disk["author"], track_attempts=True)
            history = download_history.page(author=author)
            self.assertEqual(history["success"]["total"], 1)
            self.assertEqual({item["work_id"] for item in history["failure"]["items"]}, set(works))
            for work in works:
                self.assertEqual(download_history.entry("douyin", work, failure=True)["batch_job"], job)
            self.assertEqual(download_history.page(author=("douyin", "other"))["failure"]["total"], 0)

            lower = {**previously_downloaded, "completed_at": "2026-11-20T16:01:00Z", "non_highest": True}
            download_history.save([download_history.from_manifest(root, lower, work_id=work) for work in works],
                                  author=author, track_attempts=True)
            download_history.save(disk["history"])
            result = download_history.page(author=author)
            self.assertEqual(result["failure"]["total"], 0)
            self.assertEqual(result["success"]["total"], 2)


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(app_runtime, "DATA_DIR", Path(directory)))

    def test_inventory_timeout_allows_another_request_while_the_old_callback_drains(self):
        blocked = threading.Event()
        released = threading.Event()

        def slow(*args, **kwargs):
            blocked.set()
            released.wait(2)
            return {"ok": True, "job_id": "expired"}

        def request():
            handler = SimpleNamespace(path="/api/inspect", headers={}, _read_body=lambda: {"url": "https://example.com"},
                                      _send_json=lambda payload, status=200: (payload, status))
            return server.Handler._do_POST(handler)

        try:
            with patch.object(app_runtime, "CHECK_SECONDS", .05), patch.object(engine, "inspect_creator", side_effect=slow):
                result, status = request()
            self.assertTrue(blocked.is_set())
            self.assertEqual((result["code"], status), ("inventory_timeout", 504))
            with patch.object(engine, "inspect_creator", return_value={"ok": True, "job_id": "second", "platform": "douyin"}):
                self.assertEqual(request()[0]["job_id"], "second")
        finally:
            released.set()

    def test_favorite_timeout_releases_check_and_discards_late_response(self):
        profile = {"id": "person", "url": "https://example.com/author"}
        favorite_authors.keep("job", {"platform": "douyin", "creator": profile,
                              "inventory": {"complete": True, "captured_at": "2026-11-20", "items": []}}, "firefox")
        released = threading.Event()
        ended = threading.Event()

        def slow(*args):
            released.wait(2)
            ended.set()
            return profile, [{"id": "late"}]

        try:
            with patch.object(app_runtime, "CHECK_SECONDS", .05):
                favorite_authors.start(slow, None)
                self.assertTrue(favorite_authors.view()["checking"])
                deadline = time.monotonic() + 1
                while favorite_authors.view()["checking"] and time.monotonic() < deadline:
                    time.sleep(.01)
            current = favorite_authors.view()
            self.assertFalse(current["checking"])
            self.assertIn("超时", current["authors"][0]["error"])
            released.set()
            self.assertTrue(ended.wait(1))
            favorite_authors._check_all(list(favorite_authors._read().values()),
                                        lambda *_: (profile, [{"id": "one"}, {"id": "two"}]), None, None)
            current = favorite_authors.view()["authors"][0]
            self.assertEqual((current["new_count"], current["error"]), (2, ""))
        finally:
            released.set()

    def test_download_timeout_stops_dispatch_and_keeps_last_task(self):
        runner = server.DownloadRunner()
        runner.budget = app_runtime.TimeBudget(0)
        runner.running = True
        runner.state.update(job_id="batch-resumable", ids=["1"], running=True)
        self.assertEqual(runner.snapshot()["status"], "stalled")
        worker = Mock(return_value=({"active": 0}, False))
        with (patch.object(engine, "worker_for_selection", worker),
              patch.object(engine, "job_status", return_value={}),
              patch.object(engine, "selection_disk_status", return_value={"pending": 1})):
            runner._run("batch-resumable", ["1"])
        worker.assert_called_once_with("batch-resumable", [])
        saved = server.DownloadRunner().snapshot()
        self.assertEqual((saved["status"], saved["ids"], saved["running"]), ("stalled", ["1"], False))
