
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import app_runtime
import download_history
import engine
import favorite_authors
from quality_verification import verified_work

CORE = engine.get_mod()


class PublicInventoryTests(unittest.TestCase):
    def test_xhs_author_paginates_public_notes_only(self):
        author = '638b68b6000000001f015fdb'
        profile = 'https://www.xiaohongshu.com/user/profile/' + author
        first, second, foreign = '1' * 24, '2' * 24, '3' * 24

        def page(notes, next_page=''):
            state = {'user': {'notes': [notes], 'userPageData': {'basicInfo': {'nickname': 'author'}}}}
            return f'{next_page}<script>window.__INITIAL_STATE__={json.dumps(state)}</script>'.encode()

        with patch.object(CORE, 'fetch_limited', side_effect=[
            (page([{'id': first, 'xsecToken': 'public', 'noteCard': {'displayTitle': 'first', 'user': {'userId': author}}},
                    {'noteId': foreign, 'noteCard': {'user': {'userId': 'other'}}}], '<link rel="next" href="?page=2">'), 'text/html', profile),
            (page([{'noteId': second, 'noteCard': {'displayTitle': 'second'}}]), 'text/html', profile+'?page=2'),
        ]) as request, patch.object(CORE, 'extract_platform_browser_cookies', side_effect=AssertionError('login used')):
            creator, rows = CORE.guest_creator_inventory(profile, 'xiaohongshu')
        self.assertEqual(request.call_count, 2)
        self.assertEqual([row['id'] for row in rows], [first, second])
        self.assertTrue(all(row['guest_only'] for row in rows))
        self.assertIn('xsec_token=public', rows[0]['url'])
        self.assertFalse(creator['inventory_complete'])

    def test_douyin_guest_profile_and_gallery_use_no_login(self):
        profile = 'https://www.douyin.com/user/MS4example'
        author = {'sec_uid': 'MS4example', 'nickname': 'author', 'aweme_count': 2}
        aweme = {'aweme_id': '1234567890123456789', 'author': author,
                 'images': [{'width': 100, 'height': 200, 'url_list': ['https://p1.douyinpic.com/sample.jpg']}]}
        unrelated = {'aweme_id': '2234567890123456789', 'author': {'sec_uid': 'other'}}
        html = ('<script id="RENDER_DATA">' + quote(json.dumps({'posts': [aweme, unrelated]})) + '</script>').encode()
        with patch.object(CORE, 'fetch_limited', return_value=(html, 'text/html', profile)), \
                patch.object(CORE, 'fetch_douyin_aweme_detail', side_effect=AssertionError('login used')), \
                patch.object(CORE, 'extract_platform_browser_cookies', side_effect=AssertionError('login used')):
            creator, rows = CORE.guest_creator_inventory(profile, 'douyin')
            self.assertFalse(creator['inventory_complete'])
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['guest_only'])
            self.assertIn('/note/', rows[0]['url'])
            detail, media = CORE.douyin_gallery_source(rows[0])
            self.assertIsNone(detail)
            self.assertEqual(media['item_count'], 1)

    def test_platform_can_hide_guest_inventory(self):
        url = 'https://www.douyin.com/user/MS4example'
        with patch.object(CORE, 'fetch_limited', return_value=(b'<html>Sign in</html>', 'text/html', url)):
            with self.assertRaises(CORE.ArchiveError) as unavailable:
                CORE.guest_creator_inventory(url, 'douyin')
        self.assertEqual(unavailable.exception.code, 'guest_profile_unavailable')


class DataAndQualityTests(unittest.TestCase):
    def test_note_survives_reinventory_check_and_reopen(self):
        profile = {'id': 'abc', 'url': 'https://www.douyin.com/user/abc', 'name': 'author'}
        inventory = {'complete': True, 'captured_at': '2026-11-20T00:00:00Z', 'items': [{'id': '1'}]}
        first = {'platform': 'douyin', 'creator': profile, 'inventory': inventory}
        with tempfile.TemporaryDirectory() as directory, patch.object(app_runtime, 'DATA_DIR', Path(directory)):
            key = favorite_authors.keep('first', first)
            self.assertEqual(favorite_authors.view()['authors'][0]['note'], '')
            favorite_authors.set_note(key, '猫咪 <白色>')
            favorite_authors.record_inventory('second', 'douyin', profile, lambda _: first)
            favorite_authors._commit_checks([({'key': key, 'job_id': 'second'}, {'new_count': 2})])
            current = favorite_authors.view()['authors'][0]
            self.assertEqual((current['note'], current['new_count']), ('猫咪 <白色>', 2))

    def test_only_matched_complete_work_has_quality_label(self):
        video = {'width':1920, 'height':1080, 'bitrate':2000000, 'fps':30}
        self.assertTrue(CORE.source_video_matches(video, video))
        self.assertFalse(CORE.source_video_matches({**video, 'bitrate':600000}, video))
        gallery = {'platform':'douyin', 'content_id':'1', 'status':'completed',
                   'outputs':[{'role':'image', 'source_match':True}, {'role':'image', 'source_match':True}]}
        self.assertTrue(verified_work(gallery))
        self.assertFalse(verified_work({**gallery, 'media_ids':['part-1']}))
        self.assertFalse(verified_work({**gallery, 'image_quality':'api_fallback'}))
        label = '原画质'
        self.assertIn(label, download_history.from_manifest(Path.cwd(), gallery)[7])
        self.assertNotIn(label, download_history.from_manifest(Path.cwd(), {**gallery, 'non_highest':True})[7])


if __name__ == '__main__':
    unittest.main()

