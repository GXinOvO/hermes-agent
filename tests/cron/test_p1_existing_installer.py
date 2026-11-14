"""Rehearsal for the guarded P1 install: temp profile only, injected reload."""
import hashlib
import fcntl
import subprocess
import sys
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
from contextlib import closing
from datetime import datetime, timedelta, UTC
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'apply_p1.py'
JOB = '341bad758066'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load():
    spec = importlib.util.spec_from_file_location('apply_p1_subject', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallRehearsal(unittest.TestCase):
    def test_default_candidate_is_the_reviewed_r3(self):
        module = load()
        self.assertEqual(module.NEW, '53322ccd001f447497c6f73aff562573528c614a8292e3862ba624054e2c7772')
        self.assertTrue(module.CANDIDATE.is_file())
        self.assertEqual(sha(module.CANDIDATE.read_bytes()), module.NEW)

    def setUp(self):
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory(prefix='p1-install-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base, self.candidate, self.receipts = (root / n for n in ('base', 'candidate', 'receipts'))
        self.target = self.base / 'plugins' / 'cron-supervisor-report-budget' / '__init__.py'
        self.target.parent.mkdir(parents=True)
        self.candidate.mkdir()
        self.old_bytes, self.new_bytes = b'old plugin\n', b'new plugin\n'
        self.target.write_bytes(self.old_bytes)
        self.target.chmod(0o600)
        (self.candidate / 'plugin.py').write_bytes(self.new_bytes)
        cron = self.base / 'cron'
        (cron / 'admissions').mkdir(parents=True)
        key = f'{cron.resolve()}::{JOB}'
        self.lock_paths = [cron / '.tick.lock',
                           cron / f'.fire-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.lock',
                           cron / '.jobs.lock']
        for path in self.lock_paths:
            path.touch()
        (cron / 'jobs.json').write_text(json.dumps({'jobs': [{
            'id': JOB, 'enabled': True, 'state': 'scheduled',
            'next_run_at': (datetime.now(UTC) + timedelta(hours=2)).isoformat()}]}))
        with closing(sqlite3.connect(cron / 'executions.db')) as conn, conn:
            conn.execute('CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT)')
        self.old, self.new = sha(self.old_bytes), sha(self.new_bytes)

    def install(self, **kwargs):
        kwargs.setdefault('reloader', lambda base: {'reloaded': True})
        return self.module.apply(base=self.base, candidate=self.candidate / 'plugin.py',
                                 receipt_dir=self.receipts, old=self.old, new=self.new, **kwargs)

    def test_success_records_exact_backup_and_installs_new_bytes(self):
        result = self.install()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.target.read_bytes(), self.new_bytes)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(result['backup']).read_bytes(), self.old_bytes)

    def test_pending_admission_blocks_before_any_write(self):
        (self.base / 'cron' / 'admissions' / (uuid.uuid4().hex + '.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'pending admission'):
            self.install()
        self.assertEqual(self.target.read_bytes(), self.old_bytes)

    def test_candidate_changed_after_preflight_never_installs_foreign_bytes(self):
        real_preflight = self.module.preflight
        candidate = self.candidate / 'plugin.py'
        calls = []
        def preflight_then_swap(*args, **kwargs):
            result = real_preflight(*args, **kwargs)
            self.assertEqual(result[0], [])
            candidate.write_bytes(b'foreign candidate\n')
            return result
        with patch.object(self.module, 'preflight', side_effect=preflight_then_swap):
            with self.assertRaises(Exception) as failure:
                self.install(reloader=lambda _: calls.append(True))
        self.assertIsInstance(failure.exception, ValueError, str(failure.exception))
        self.assertIn('candidate bytes changed', str(failure.exception))
        self.assertEqual(self.target.read_bytes(), self.old_bytes)
        self.assertEqual(calls, [])

    def test_post_replace_io_failure_restores_original_without_reloading(self):
        real_publish = self.module.publish
        calls = []
        def publish_then_error(path, data, mode, **kwargs):
            real_publish(path, data, mode, **kwargs)
            if data == self.new_bytes:
                raise OSError('injected directory sync failure after replace')
        with patch.object(self.module, 'publish', side_effect=publish_then_error):
            with self.assertRaisesRegex(OSError, 'injected directory sync failure'):
                self.install(reloader=lambda _: calls.append(True))
        self.assertEqual(self.target.read_bytes(), self.old_bytes)
        self.assertEqual(calls, [])

    def test_candidate_replaced_after_verified_snapshot_installs_verified_bytes(self):
        real_publish = self.module.publish
        candidate = self.candidate / 'plugin.py'
        def change_source_then_publish(path, data, mode, **kwargs):
            candidate.write_bytes(b'changed after snapshot\n')
            real_publish(path, data, mode, **kwargs)
        with patch.object(self.module, 'publish', side_effect=change_source_then_publish):
            result = self.install()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.target.read_bytes(), self.new_bytes)
        self.assertEqual(candidate.read_bytes(), b'changed after snapshot\n')

    def test_concurrent_target_change_is_not_overwritten_by_rollback(self):
        real_publish = self.module.publish
        foreign = b'concurrent writer\n'
        calls = []
        def publish_then_foreign(path, data, mode, **kwargs):
            real_publish(path, data, mode, **kwargs)
            path.write_bytes(foreign)
        with patch.object(self.module, 'publish', side_effect=publish_then_foreign):
            with self.assertRaisesRegex(RuntimeError, 'target changed concurrently'):
                self.install(reloader=lambda _: calls.append(True))
        self.assertEqual(self.target.read_bytes(), foreign)
        self.assertEqual(calls, [])

    def test_missing_next_run_blocks_install(self):
        jobs = json.loads((self.base / 'cron' / 'jobs.json').read_text())
        jobs['jobs'][0].pop('next_run_at')
        (self.base / 'cron' / 'jobs.json').write_text(json.dumps(jobs))
        with self.assertRaisesRegex(ValueError, 'next execution.*unavailable'):
            self.install()
        self.assertEqual(self.target.read_bytes(), self.old_bytes)
        self.assertFalse(self.receipts.exists())

    def test_imminent_execution_blocks(self):
        jobs = json.loads((self.base / 'cron' / 'jobs.json').read_text())
        jobs['jobs'][0]['next_run_at'] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        (self.base / 'cron' / 'jobs.json').write_text(json.dumps(jobs))
        with self.assertRaisesRegex(ValueError, 'too close'):
            self.install()

    def test_running_execution_blocks(self):
        with closing(sqlite3.connect(self.base / 'cron' / 'executions.db')) as conn, conn:
            conn.execute('INSERT INTO executions VALUES (?,?,?)', ('a' * 32, JOB, 'running'))
        with self.assertRaisesRegex(ValueError, 'nonterminal'):
            self.install()

    def test_changed_candidate_bytes_block(self):
        (self.candidate / 'plugin.py').write_bytes(b'tampered\n')
        with self.assertRaisesRegex(ValueError, 'candidate bytes changed'):
            self.install()

    def test_scheduler_locks_are_held_during_activation(self):
        code = ('import fcntl,sys; '
                'f=open(sys.argv[1], "r+"); '
                'fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)')
        observations = []
        def reloader(_base):
            for path in self.lock_paths:
                result = subprocess.run([sys.executable, '-B', '-c', code, str(path)],
                                        capture_output=True, text=True, timeout=5, check=False)
                observations.append(result.returncode)
                self.assertNotEqual(result.returncode, 0, f'lock not held: {path}')
                self.assertIn('BlockingIOError', result.stderr)
            return {'reloaded': True}
        self.install(reloader=reloader)
        self.assertEqual(len(observations), 3)
        for path in self.lock_paths:
            with path.open('r+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_lock_contention_refuses_without_mutation(self):
        for busy in self.lock_paths:
            with self.subTest(lock=busy.name):
                with busy.open('r+') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    called = []
                    with self.assertRaises(BlockingIOError):
                        self.install(reloader=lambda _: called.append(True))
                    self.assertFalse(called)
                    self.assertEqual(self.target.read_bytes(), self.old_bytes)
                    self.assertFalse(self.receipts.exists())
                    fcntl.flock(handle, fcntl.LOCK_UN)
                for path in self.lock_paths:
                    with path.open('r+') as probe:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(probe, fcntl.LOCK_UN)

    def test_failed_activation_restores_and_reloads_original(self):
        observed = []
        def reloader(_base):
            observed.append(self.target.read_bytes())
            if len(observed) == 1:
                raise RuntimeError('injected activation failure')
            return {'reloaded': True}
        with self.assertRaisesRegex(RuntimeError, 'injected activation failure'):
            self.install(reloader=reloader)
        self.assertEqual(observed, [self.new_bytes, self.old_bytes])
        self.assertEqual(self.target.read_bytes(), self.old_bytes)

    def test_activation_failure_restores_original_and_leaves_backup(self):
        observed = []
        def failing(base):
            observed.append(self.target.read_bytes())
            raise RuntimeError('injected activation failure')
        with self.assertRaisesRegex(RuntimeError, 'rollback bytes restored but gateway activation unverified') as error:
            self.install(reloader=failing)
        self.assertIn('injected activation failure', str(error.exception.__cause__))
        self.assertEqual(observed, [self.new_bytes, self.old_bytes])
        self.assertEqual(self.target.read_bytes(), self.old_bytes)
        backup = self.receipts / f'production-before-{self.old[:12]}.py'
        self.assertEqual(backup.read_bytes(), self.old_bytes)


if __name__ == '__main__':
    unittest.main()
