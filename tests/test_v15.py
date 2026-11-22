import os
import json
import logging
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import app_runtime
import engine
import server

CORE = engine.get_mod()
PROJECT = Path(__file__).resolve().parents[1]

class V15Tests(unittest.TestCase):
    def test_configuration_survives_independent_processes(self):
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env.pop('WORKS_ARCHIVE_ROOT', None)
            env['WORKS_ASSISTANT_DATA'] = str(Path(td)/'settings')
            selected = str(Path(td)/'中文 archive')
            code = 'import engine;engine.set_archive_root(' + repr(selected) + ')'
            subprocess.run([sys.executable, '-c', code], cwd=PROJECT, env=env, check=True, capture_output=True)
            read = subprocess.check_output([sys.executable, '-c', 'import engine;print(str(engine.ARCHIVE_ROOT).encode("utf-8").hex())'], cwd=PROJECT, env=env)
            self.assertEqual(bytes.fromhex(read.decode().strip()).decode('utf-8'), selected)

    def test_explicit_environment_override_is_not_overwritten(self):
        with patch.object(engine, '_EXPLICIT_ROOT', str(PROJECT)):
            self.assertEqual(engine.get_archive_root(), PROJECT)
            with self.assertRaises(engine.EngineError):
                engine.set_archive_root(str(PROJECT/'other'))

    def test_cookie_negative_cache_can_recover(self):
        CORE.invalidate_redbook_cache()
        with tempfile.TemporaryDirectory() as td:
            cookie = Path(td)/'cookie.json'
            cookie.write_text('{}')
            with patch.object(CORE, '_firefox_xiaohongshu_cookie_file', side_effect=[None, cookie]) as read:
                self.assertIsNone(CORE._redbook_cookie_file('firefox'))
                self.assertEqual(CORE._redbook_cookie_file('firefox'), cookie)
                self.assertEqual(read.call_count, 2)
            CORE.invalidate_redbook_cache()

    def test_cookie_import_invalidates_details(self):
        CORE._REDBOOK_NOTE_CACHE['test'] = {'id': 'test'}
        with patch.object(CORE, 'import_browser_cookies', return_value={'ok': True}):
            engine.import_browser_cookies('xiaohongshu', 'firefox')
        self.assertEqual(CORE._REDBOOK_NOTE_CACHE, {})

    def test_invalid_video_is_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); out=root/'content';out.mkdir()
            (out/'video.mp4').write_bytes(b'NOT A VIDEO'*300)
            self.assertFalse(CORE._child_output_files_exist(root, {'output_dir':'content','content_type':'video'}))

    def test_real_video_is_complete(self):
        self.assertTrue(CORE.valid_media_file(PROJECT/'tests/fixtures/valid.mp4', 'video'))

    def test_atomic_write_failure_preserves_old_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'manifest.json';target.write_text('{"old": true}')
            with patch.object(CORE.os, 'replace', side_effect=PermissionError('locked')), patch.object(CORE.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    CORE.write_json(target, {'new': True})
            self.assertEqual(json.loads(target.read_text()), {'old': True})

    def test_sensitive_error_chain_is_redacted(self):
        formatter = app_runtime.SafeFormatter('%(message)s')
        record = logging.LogRecord('test',40,'',0,'URL https://example.com/a?token=SECRET cookie=PRIVATE',(),None)
        text=formatter.format(record)
        self.assertNotIn('SECRET', text);self.assertNotIn('PRIVATE',text)

    def test_runner_restores_interrupted_state(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engine,'ARCHIVE_ROOT',Path(td)):
            app_runtime.atomic_json(Path(td)/'jobs/.gui-run.json', {'job_id':'batch-test','ids':['1'],'running':True,'finished':False})
            runner=server.DownloadRunner()
            self.assertEqual(runner.snapshot()['status'],'interrupted')
            self.assertFalse(runner.running)

    def test_partial_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engine,'ARCHIVE_ROOT',Path(td)):
            runner=server.DownloadRunner()
            with patch.object(engine,'worker_for_selection',return_value=({'active':0},False)), patch.object(engine,'job_status',return_value={'job':{'counts':{}}}), patch.object(engine,'selection_disk_status',return_value={'ready':1,'failed':1,'pending':0,'missing':0}):
                runner._run('batch-test',['1','2'])
            self.assertFalse(runner.snapshot()['success'])
            self.assertEqual(runner.snapshot()['status'],'completed_with_failures')

    def test_waiting_for_login_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engine,'ARCHIVE_ROOT',Path(td)):
            runner=server.DownloadRunner()
            with patch.object(engine,'worker_for_selection',return_value=({'active':0},False)), patch.object(engine,'job_status',return_value={'job':{'counts':{'waiting_for_reauthentication':1}}}), patch.object(engine,'selection_disk_status',return_value={'ready':0,'failed':0,'pending':1,'missing':0}):
                runner._run('batch-test',['1'])
            self.assertEqual(runner.snapshot()['status'],'waiting_for_reauthentication')

    def test_worker_only_schedules_selected_children(self):
        with tempfile.TemporaryDirectory() as td, patch.object(engine,'ARCHIVE_ROOT',Path(td)), patch.object(engine,'_BATCH_FUTURES',{}):
            for n in ('selected','unrelated'):
                CORE.write_json(Path(td)/'jobs'/n/'manifest.json', {'status':'queued'})
            parent={'inventory':{'items':[{'id':'1','child_job_id':'selected'},{'id':'2','child_job_id':'unrelated'}]}}
            from unittest.mock import MagicMock
            pool=MagicMock()
            with patch.object(engine,'read_manifest',return_value=parent), patch.object(CORE,'_content_executor',return_value=pool):
                engine.worker_for_selection('parent',['1'])
            self.assertEqual(pool.submit.call_count,1)
            self.assertEqual(pool.submit.call_args.args[1].parent.name,'selected')

    def test_transient_retry_has_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); path=root/'jobs/content/manifest.json'
            CORE.write_json(path,{'job_id':'test','platform':'douyin','status':'queued'})
            with patch.object(CORE,'process_transparent_video',side_effect=CORE.ArchiveError('network_timeout','timeout',69)):
                for attempt in range(3):
                    result=CORE._process_content_job_unlocked(path,root)
                    self.assertEqual(result['status'],'queued' if attempt<2 else 'failed')
            self.assertEqual(result['auto_attempts'],2)

    def test_permanent_failure_does_not_auto_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'jobs/content/manifest.json'
            CORE.write_json(path,{'job_id':'test','platform':'douyin','status':'queued'})
            with patch.object(CORE,'process_transparent_video',side_effect=CORE.ArchiveError('media_unavailable','deleted',69)):
                result=CORE._process_content_job_unlocked(path,root)
            self.assertEqual(result['status'],'unavailable')
            self.assertNotIn('auto_attempts',result)

if __name__=='__main__': unittest.main()
