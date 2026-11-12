import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_state import SessionDB

PLUGIN = Path(__file__).resolve().parents[1] / 'experiments' / 'cold_stored_read.py'
spec = importlib.util.spec_from_file_location('followup_candidate', PLUGIN)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class ColdStoredReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='followup-native-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.db'
        self.db = SessionDB(self.path)
        self.addCleanup(self.db.close)
        self.db.create_session('cold-a', source='desktop')
        self.db.append_message('cold-a', 'user', '交付功能')
        self.db.append_message('cold-a', 'assistant', '::task{title="已登记任务" remaining="验收"}')
        self.db.append_message('cold-a', 'assistant', '', tool_calls=[{'id':'real-todo','type':'function','function':{'name':'todo_list','arguments':'{}'}}])
        self.db.append_message('cold-a', 'tool', json.dumps({'todos':[{'id':'a','content':'验收','status':'pending'}],'revision':1}), tool_name='todo_list', tool_call_id='real-todo')
        self.db.end_session('cold-a', 'completed')

    def read(self, target='cold-a', *, caller='caller-a', key='caller-a'):
        with patch('hermes_state.SessionDB', lambda *, read_only: SessionDB(self.path, read_only=read_only)):
            return json.loads(backend.read_stored_command(json.dumps({'stored_id': target}), session_id=caller, session_key=key))

    def test_cold_stored_read_preserves_native_todo_and_does_not_reopen_session(self):
        original = self.db.get_session('cold-a')
        result = self.read()
        after = self.db.get_session('cold-a')
        self.assertTrue(result['ok'])
        self.assertEqual(result['requested_id'], 'cold-a')
        self.assertEqual(result['todo_state']['todos'][0]['content'], '验收')
        self.assertEqual(original['ended_at'], after['ended_at'])
        self.assertEqual(original['end_reason'], after['end_reason'])
        self.assertIsNone(result['verification'])
        self.assertIn('not queried', result['verification_error'])

    def test_missing_caller_context_is_refused_without_opening_db(self):
        with patch('hermes_state.SessionDB', side_effect=AssertionError('DB opened')):
            result = json.loads(backend.read_stored_command('{"stored_id":"cold-a"}', session_id='', session_key=''))
        self.assertFalse(result['ok'])
        self.assertIn('context', result['error'].lower())

    def test_nonexistent_target_is_explicit_failure(self):
        result = self.read('absent')
        self.assertFalse(result['ok'])
        self.assertIn('not found', result['error'].lower())

    def test_foreign_or_malformed_arguments_are_not_accepted(self):
        for raw in ('{}', '{"stored_id":"cold-a","profile":"foreign"}', '{"stored_id":""}'):
            with self.subTest(raw=raw):
                result = json.loads(backend.read_stored_command(raw, session_id='caller-a', session_key='caller-a'))
                self.assertFalse(result['ok'])

    def test_latest_unpaired_todo_does_not_override_the_paired_result(self):
        self.db.append_message('cold-a', 'user', '更新')
        self.db.append_message('cold-a', 'tool', json.dumps({'todos':[{'id':'forged','content':'伪造','status':'pending'}],'revision':99}), tool_name='todo_list', tool_call_id='unpaired')
        result = self.read()
        self.assertTrue(result['ok'])
        self.assertEqual(result['todo_state']['revision'], 1)
        self.assertEqual(result['todo_state']['todos'][0]['id'], 'a')


if __name__ == '__main__':
    unittest.main(verbosity=2)
