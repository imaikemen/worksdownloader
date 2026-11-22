from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import app_runtime
import download_history
import engine


CORE = engine.get_mod()


class DurableArchiveTests(unittest.TestCase):
    def test_fresh_inventory_reads_verified_history_without_quality_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform = 'douyin'
            work_id = '1234567890123456789'
            child = {'platform': platform, 'submission_id': work_id, 'title': '作品', 'status': 'completed'}
            failure = {**child, 'status': 'failed', 'updated_at': '2026-11-21T03:00:00Z'}
            inventory = {'platform': platform, 'inventory': {'items': [{'id': work_id, 'title': '作品'}]}}
            with (patch.object(app_runtime, 'DATA_DIR', root), patch.object(engine, 'job_root', return_value=root),
                  patch.object(engine, 'read_manifest', return_value=inventory),
                  patch.object(engine, 'enrich_items_with_cover')):
                self.assertFalse(engine.inventory_view('first')['items'][0].get('_downloaded', False))
                download_history.save([download_history.from_manifest(root, child)])
                download_history.save([download_history.from_manifest(root, failure)])
                item = engine.inventory_view('reopened')['items'][0]
                self.assertTrue(item['_downloaded'])
                self.assertEqual(item['archive_quality'], '')

    def test_equal_pixels_preserve_image_bytes_and_more_pixels_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, candidate = root / 'archive.jpg', root / 'incoming.jpg'
            Image.new('RGB', (80, 40), 'blue').save(old)
            original = old.read_bytes()
            Image.new('RGB', (40, 80), 'white').save(candidate)
            self.assertEqual(CORE.commit_image(candidate, old, (40, 80)), (old, (80, 40), True))
            self.assertEqual(old.read_bytes(), original)
            Image.new('RGB', (80, 50), 'white').save(candidate)
            self.assertEqual(CORE.commit_image(candidate, old, (80, 50)), (old, (80, 50), False))
            self.assertEqual(CORE.image_size(old), (80, 50))

    def test_video_bitrate_alone_never_replaces_archived_bytes(self):
        dimensions = {'width': 1080, 'height': 1920}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, candidate = root / 'video.mp4', root / 'candidate.mp4'
            old.write_bytes(b'old')
            candidate.write_bytes(b'new')
            with patch.object(CORE, 'probe_video_file', return_value={**dimensions, 'bitrate': 700000}):
                retained = CORE.commit_video(candidate, old, {**dimensions, 'bitrate': 9000000}, trust_existing=False)
                self.assertTrue(retained['retained_existing'])
                self.assertTrue(retained['unverified_clean_source'])
                self.assertEqual(old.read_bytes(), b'old')
                candidate.write_bytes(b'higher')
                CORE.commit_video(candidate, old, {'width': 1440, 'height': 2560, 'bitrate': 700000})
                self.assertEqual(old.read_bytes(), b'higher')

    def test_second_cdn_can_return_advertised_pixels_after_first_returns_lower(self):
        first, second = 'https://v1-web.douyinvod.com/video', 'https://v2-web.douyinvod.com/video'
        attempts = []

        def download(urls, path, _referer):
            attempts.extend(urls)
            path.write_bytes(urls[0].encode())

        def probe(path):
            data = path.read_bytes().decode()
            width, height = (1080, 1920) if data == second else (540, 960)
            return {'width': width, 'height': height}

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(CORE, 'probe_video_file', side_effect=probe), patch.object(CORE, '_MEDIA_ROUTES', {})):
            result = CORE.download_best_video([{'urls': [first, second], 'width': 1080, 'height': 1920}],
                                              Path(directory) / 'video.mp4', '', download)
            self.assertEqual(attempts, [first, second])
            self.assertEqual((result['width'], result['height']), (1080, 1920))


class GuestDownloadTests(unittest.TestCase):
    def test_douyin_guest_video_uses_public_extractor_without_account_detail(self):
        work_id = '1234567890123456789'
        url = f'https://www.douyin.com/video/{work_id}'
        manifest = {'source': url, 'content_id': work_id, 'guest_only': True}
        metadata = {'id': work_id, 'title': '公开作品'}
        sources = [{'width': 1080, 'height': 1920, 'urls': ['https://v1-web.douyinvod.com/file']}]
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(CORE, 'fetch_douyin_aweme_detail', side_effect=AssertionError('account requested')),
              patch.object(CORE, 'inspect_with_transparent_core', return_value=metadata) as inspect,
              patch.object(CORE, 'douyin_public_video_choices', return_value=sources),
              patch.object(CORE, 'download_best_video', return_value=sources[0])):
            result, _ = CORE.download_douyin_inventory_fallback(manifest, Path(directory))
            inspect.assert_called_once_with(url, 'douyin', use_cookies=False)
            self.assertEqual(result['id'], work_id)

    def test_rednote_missing_public_media_never_reads_account(self):
        url = 'https://www.rednote.com/explore/6af05aa40000000014028741'
        manifest = {'source': url, 'guest_only': True}
        with (tempfile.TemporaryDirectory() as directory, patch.object(CORE, '_rednote_page_note', return_value=None) as read,
              patch.object(CORE, '_redbook_read_note', side_effect=AssertionError('account requested')),
              patch.object(CORE, '_redbook_cookie_file', side_effect=AssertionError('account requested')),
              patch.object(CORE, 'inspect_with_transparent_core', side_effect=AssertionError('account fallback requested'))):
            root = Path(directory)
            with self.assertRaises(CORE.ArchiveError) as error:
                CORE.process_xiaohongshu(manifest, root / 'manifest.json', root)
            self.assertEqual(error.exception.code, 'guest_media_unavailable')
            read.assert_called_once_with(url, None, guest_only=True)
