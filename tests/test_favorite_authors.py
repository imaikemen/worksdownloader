from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app_runtime
import favorite_authors


PROFILE = {"id": "creator", "name": "常看测试", "account": "12345",
           "avatar": "https://images.example.com/avatar.jpg",
           "url": "https://www.rednote.com/user/profile/creator?xsec_source=pc_user"}


def manifest(items, captured="2026-11-19T00:00:00Z"):
    return {"platform": "xiaohongshu", "creator": PROFILE,
            "inventory": {"complete": True, "captured_at": captured, "items": items}}


class FavoriteAuthorTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(app_runtime, "DATA_DIR", Path(directory)))
        self.key = favorite_authors.keep("first", manifest([{"id": "pinned"}]), "firefox")

    def test_persist_profile_and_count_only_ids_missing_from_the_last_inventory(self):
        stored = favorite_authors.view()["authors"][0]
        self.assertEqual(stored["url"], PROFILE["url"])
        self.assertEqual(stored["account"], PROFILE["account"])
        self.assertEqual(stored["avatar"], PROFILE["avatar"])
        items = [{"id": "pinned", "published_at": 1799999999},
                 {"id": "new-a", "published_at": 1795147200000},
                 {"id": "new-a", "published_at": 1795147200000},
                 {"id": "new-b"}]
        favorite_authors._check_all(list(favorite_authors._read().values()),
                                    lambda *_: (PROFILE, items),
                                    lambda *_: "2026-11-20T12:30:00+00:00", None)
        current = favorite_authors.view()["authors"][0]
        self.assertEqual(current["new_count"], 2)
        self.assertEqual(current["latest_at"], 1795177800)
        self.assertTrue(current["checked_at"])
        self.assertNotIn("baseline_ids", current)
        favorite_authors.record_inventory("second", "xiaohongshu", PROFILE,
                                          lambda _: manifest(items, "2026-11-20T13:00:00Z"))
        self.assertEqual(favorite_authors.view()["authors"][0]["new_count"], 0)
        self.assertIsNone(favorite_authors.view()["authors"][0]["latest_at"])
        favorite_authors.remove(self.key)
        self.assertEqual(favorite_authors.view()["authors"], [])

    def test_background_result_does_not_overwrite_new_inventory(self):
        entered = threading.Event()
        release = threading.Event()
        records = list(favorite_authors._read().values())

        def scan(*_):
            entered.set()
            release.wait(4)
            return PROFILE, [{"id": "new-a", "published_at": 1795147200}]

        worker = threading.Thread(target=favorite_authors._check_all, args=(records, scan, None, None))
        worker.start()
        try:
            self.assertTrue(entered.wait(4))
            favorite_authors.record_inventory("second", "xiaohongshu", PROFILE,
                                              lambda _: manifest([{"id": "new-a"}], "2026-11-20T13:00:00Z"))
        finally:
            release.set()
            worker.join(4)
        self.assertEqual(favorite_authors.view()["authors"][0]["new_count"], 0)

    def test_failure_keeps_previous_verified_update_and_single_work_cannot_be_added(self):
        records = list(favorite_authors._read().values())
        favorite_authors._check_all(records, lambda *_: (PROFILE, [{"id": "new", "published_at": 1795147200}]), None, None)

        def unavailable(*_):
            raise OSError("Service unavailable")

        favorite_authors._check_all(records, unavailable, None, None)
        current = favorite_authors.view()["authors"][0]
        self.assertEqual(current["new_count"], 1)
        self.assertTrue(current["error"])
        single_work = {**manifest([]), "creator": {**PROFILE, "single_work": True}}
        with self.assertRaises(ValueError):
            favorite_authors.keep("single", single_work)
