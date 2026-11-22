from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import app_runtime
import download_history
import engine


CORE = engine.get_mod()
WORK_ID = "650e509400000000120056fc"
CHILD = "content-20261119T000000Z-00000001"
BATCH = "batch-20261119T000000Z-00000002"
AUTHOR_URL = "https://www.rednote.com/user/profile/635468cf0000000009000000"


class HistoryArchiveTests(unittest.TestCase):
    def test_old_history_resolves_its_exact_media_and_own_batch_after_upgrade(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(app_runtime, "DATA_DIR", Path(scratch)):
            root = Path(scratch)
            folder = root / "content/小红书/作者（帐号）"
            folder.mkdir(parents=True)
            picture = folder / ("照片--" + WORK_ID + "--图001.jpg")
            Image.new("RGB", (98, 163), "blue").save(picture)
            child = {"job_id": CHILD, "platform": "xiaohongshu", "content_id": WORK_ID, "status": "completed",
                     "title": "照片", "source": f"https://www.rednote.com/explore/{WORK_ID}",
                     "completed_at": "2026-11-19T00:00:01Z", "content_type": "image_text",
                     "output_dir": folder.relative_to(root).as_posix(), "expected_image_count": 1,
                     "outputs": [{"role": "image", "path": picture.relative_to(root).as_posix()}]}
            batch = {"inventory": {"items": [{"id": WORK_ID, "child_job_id": CHILD}]}}
            for job, manifest in ((CHILD, child), (BATCH, batch)):
                CORE.write_json(root / "jobs" / job / "manifest.json", manifest)
            # Existing installations have both the result and the old import marker.
            with closing(download_history.connect()) as database, database:
                database.execute("INSERT INTO imported_roots VALUES (?)", (str(root),))
            download_history.save([download_history.from_manifest(root, child)[:8]])

            download_history.import_archives([])
            record = download_history.entry("xiaohongshu", WORK_ID)
            self.assertEqual(record["batch_job"], BATCH)
            self.assertEqual(engine.history_preview(record), picture)
            self.assertEqual(engine.history_local_path(record, root, child), picture)

    def test_first_success_writes_exact_pasted_author_url_once(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            original = AUTHOR_URL + "?from=share"
            engine.write_author_address(folder, original)
            address = folder / "作者主页.txt"
            engine.write_author_address(folder, AUTHOR_URL)
            self.assertEqual(address.read_text(encoding="utf-8"), original + "\n")
            self.assertEqual(list(folder.iterdir()), [address])

    def test_failed_history_reuses_original_author_batch(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = f"https://www.rednote.com/explore/{WORK_ID}"
            child = {"job_id": CHILD, "source": source, "status": "failed"}
            item = {"id": WORK_ID, "url": source, "child_job_id": CHILD, "result": "failed"}
            batch = {"kind": "creator_batch", "platform": "xiaohongshu", "inventory": {"items": [item]}}
            CORE.write_json(root / "jobs" / CHILD / "manifest.json", child)
            CORE.write_json(root / "jobs" / BATCH / "manifest.json", batch)
            entry = {"archive_root": str(root), "platform": "xiaohongshu", "work_id": WORK_ID, "child_job": CHILD,
                     "batch_job": BATCH, "title": "原作"}
            with patch.dict(engine.JOB_ROOTS, {}):
                self.assertEqual(engine.history_retry_job(entry), BATCH)
                self.assertEqual(engine.job_root(BATCH), root)
