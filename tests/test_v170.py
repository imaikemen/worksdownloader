import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_runtime
import download_history
import engine


CORE = engine.get_mod()


class AuthorArchiveTests(unittest.TestCase):
    def test_legacy_results_link_by_author_and_folder_survives_rename(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(app_runtime, "DATA_DIR", Path(scratch)):
            root = Path(scratch)
            author = {"id": "stable-a"}
            first = "content/抖音/初始昵称（123）"
            job = root / "jobs" / "batch-20261119T000000Z-00000001"
            job.mkdir(parents=True)
            CORE.write_json(job / "manifest.json", {
                "platform": "douyin", "creator": author, "inventory": {"items": [{"id": "old-work"}]},
            })
            download_history.save([
                ("douyin", "old-work", "已移除的作品", "failure", "2026-11-19", "失败", "", ""),
                ("xiaohongshu", "old-work", "另一平台同 ID", "success", "2026-11-19", "", "", ""),
            ])
            download_history.import_author_links([root])
            self.assertEqual(download_history.bind_author(root, "douyin", author, [{"id": "fresh"}], first), first)
            self.assertEqual(download_history.bind_author(root, "douyin", author, [], "content/抖音/改名（456）"), first)
            self.assertNotEqual(download_history.bind_author(root, "douyin", {"id": "stable-b"}, [], first), first)

            filtered = download_history.page(author=("douyin", author["id"]))
            self.assertEqual(filtered["failure"]["items"][0]["work_id"], "old-work")
            self.assertEqual(filtered["success"]["total"], 0)
            self.assertEqual(download_history.page()["success"]["total"], 1)

    def test_two_flat_works_share_one_folder_and_use_their_own_covers(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            author_folder = "content/小红书/作者（123）"
            ids = ("6aea85030000000010012c60", "6aea85030000000010012c61")
            images = []
            for item_id in ids:
                manifest = {"author_folder": author_folder}
                folder, prefix = CORE.work_archive_layout(root, manifest, "xiaohongshu", "相同标题", item_id)
                folder.mkdir(parents=True, exist_ok=True)
                image = folder / f"{prefix}--图001.webp"
                image.touch()
                images.append(image)
            for item_id, image in zip(ids, images):
                item = {"id": item_id, "url": f"https://www.rednote.com/explore/{item_id}", "_author_folder": author_folder}
                self.assertEqual(engine.resolve_item_cover(item, root=root), ("image", image))

            self.assertEqual(set(images), set((root / author_folder).iterdir()))
            self.assertNotEqual(images[0], images[1])
            legacy, prefix = CORE.work_archive_layout(root, {}, "xiaohongshu", "同标题", ids[0])
            self.assertEqual(legacy.parent, (root / author_folder).parent)
            self.assertIsNone(prefix)

    def test_rednote_retries_the_original_source_when_the_refreshed_link_fails(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            item_id = "6aea85030000000010012c60"
            base = f"https://www.rednote.com/explore/{item_id}?xsec_token="
            fresh, old = base + "new", base + "old"
            note = {"noteId": item_id, "type": "normal", "imageList": [{"urlDefault": "https://sns-webpic-qc.xhscdn.com/a.webp"}]}
            manifest = {"source": fresh, "previous_xiaohongshu_source": old,
                        "content_id": item_id, "xiaohongshu_browser": "firefox"}
            with patch.object(CORE, "_redbook_read_note", side_effect=[CORE.ArchiveError("xiaohongshu_detail_failed", "失效"), note]) as read, \
                 patch.object(CORE, "finalize_xiaohongshu_image_text", return_value={"status": "completed"}) as finalize:
                result = CORE.process_xiaohongshu(manifest, root / "manifest.json", root)
            self.assertEqual([call.args for call in read.call_args_list], [(fresh, "firefox"), (old, "firefox")])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(finalize.call_args.args[3]["thumbnails"][0]["url"], "https://sns-webpic-qc.xhscdn.com/a.webp")
