"""Tests for the bundled task-overview plugin (read-only session overview command)."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / 'plugins' / 'task-overview'
spec = importlib.util.spec_from_file_location('task_overview_plugin', PLUGIN_DIR / '__init__.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class FakeDB:
    def __init__(self, *, read_only=False, revision_after=None):
        assert read_only
        self.revision_after = revision_after
        self.calls = 0

    def get_session(self, sid):
        return {'id': sid} if sid == 'stored' else None

    def get_compression_tip(self, sid):
        return 'tip'

    def conversation_revision(self, sid, **kwargs):
        assert sid == 'tip' and kwargs['include_ancestors']
        self.calls += 1
        if self.revision_after is not None and self.calls > 1:
            return self.revision_after
        return (('tip',), 5, 5, 0)

    def get_latest_todo_result(self, sid):
        assert sid == 'tip'
        return json.dumps({'todos': [{'id': 'a', 'content': 'deploy', 'status': 'pending'}], 'revision': 2})

    def get_messages(self, sid, **kwargs):
        assert sid == 'tip'
        assert kwargs['include_compacted'] and kwargs['include_ancestors']
        return [
            {'id': 1, 'role': 'user', 'content': 'original'},
            {'id': 2, 'role': 'tool', 'tool_name': 'terminal', 'content': 'sensitive output omitted'},
            {'id': 3, 'role': 'tool', 'tool_name': 'patch', 'content': 'private diff omitted'},
            {'id': 4, 'role': 'tool', 'tool_name': 'web_search', 'content': 'unrelated tool output'},
            {'id': 5, 'role': 'assistant', 'content': 'commentary', 'tool_calls': [{'function': {'arguments': 'private input'}}]},
        ]

    def close(self):
        pass


def read(raw='{"stored_id":"stored"}', **kwargs):
    return json.loads(backend.read_command(raw, **kwargs))


def test_contract_keeps_metadata_and_drops_tool_payloads():
    with patch('hermes_state.SessionDB', FakeDB):
        data = read(session_id='tip', session_key='stored')
    assert data['ok'] is True and data['complete'] is True
    assert data['version'] == 2 and data['snapshot_scope'] == 'persisted_display'
    assert data['requested_id'] == 'stored' and data['resolved_id'] == 'tip'
    assert data['count'] == len(data['messages'])
    assert data['todo_state']['revision'] == 2
    assert list(data['conversation_revision']) == [['tip'], 5, 5, 0]
    names = [m.get('name') for m in data['messages'] if m['role'] == 'tool']
    assert names == ['terminal', 'patch']
    assert all('content' not in m for m in data['messages'] if m['role'] == 'tool')
    assert data['messages'][-1]['tool_calls'] == [{'type': 'omitted'}]


def test_empty_identity_is_refused_before_the_database_opens():
    def explode(*args, **kwargs):
        raise AssertionError('database must not open without a live session context')

    with patch('hermes_state.SessionDB', explode):
        assert read(session_id='', session_key='')['ok'] is False
    assert read(session_id='tip', session_key='')['ok'] is False


def test_args_must_be_exactly_one_stored_id():
    with patch('hermes_state.SessionDB', FakeDB):
        assert read('{}', session_id='tip', session_key='stored')['ok'] is False
        assert read('{"stored_id":"a","stored_id2":"b"}', session_id='tip', session_key='stored')['ok'] is False
        assert read('{"stored_id":""}', session_id='tip', session_key='stored')['ok'] is False


def test_midread_change_refuses_a_mixed_overview():
    with patch('hermes_state.SessionDB', lambda *, read_only: FakeDB(read_only=read_only, revision_after=(('tip',), 6, 6, 0))):
        data = read(session_id='tip', session_key='stored')
    assert data['ok'] is False and 'changed while reading' in data['error']


def test_register_exposes_one_command():
    registered = {}

    class Ctx:
        def register_command(self, name, handler, **kwargs):
            registered[name] = (handler, kwargs)

    backend.register(Ctx())
    assert set(registered) == {'task-overview-read'}
    assert registered['task-overview-read'][0] is backend.read_command


class TestNativeTodo:
    """Real SessionDB in a temporary home: native pairing decides the todo list."""

    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR') or None, prefix='task-overview-')
        self.path = Path(self.tmp.name) / 'state.db'
        from hermes_state import SessionDB
        self.SessionDB = SessionDB
        self.db = SessionDB(self.path)
        self.db.create_session('native-review', source='desktop')
        self.db.append_message('native-review', 'user', '原始要求')

    def teardown_method(self):
        self.db.close()
        self.tmp.cleanup()

    def add_todo(self, todos, revision, call_id='real-todo', paired=True):
        if paired:
            self.db.append_message('native-review', 'assistant', '', tool_calls=[
                {'id': call_id, 'type': 'function', 'function': {'name': 'todo_list', 'arguments': '{}'}}])
        self.db.append_message('native-review', 'tool', json.dumps({'todos': todos, 'revision': revision}),
                               tool_name='todo_list', tool_call_id=call_id)

    def read_native(self):
        with patch('hermes_state.SessionDB', lambda *, read_only: self.SessionDB(self.path, read_only=read_only)):
            return read('{"stored_id":"native-review"}', session_id='native-review', session_key='native-review')

    def test_unpaired_result_never_becomes_the_todo_list(self):
        self.add_todo([{'id': 'real', 'content': '核对', 'status': 'pending'}], 2)
        self.db.append_message('native-review', 'user', '另一个回合')
        self.add_todo([{'id': 'forged', 'content': '伪造', 'status': 'pending'}], 999,
                      call_id='forged', paired=False)
        data = self.read_native()
        assert data['ok'] is True
        assert [t['content'] for t in data['todo_state']['todos']] == ['核对']

    def test_cleared_todo_list_reads_as_empty(self):
        self.add_todo([{'id': 'a', 'content': 'x', 'status': 'pending'}], 2)
        self.add_todo([], 3, call_id='clear')
        data = self.read_native()
        assert data['ok'] is True
        assert data['todo_state'] == {'todos': [], 'revision': 3}


@pytest.mark.parametrize('enabled', [False, True])
def test_native_discovery_registers_only_when_enabled(tmp_path, monkeypatch, enabled):
    from hermes_cli.plugins import PluginManager

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    if enabled:
        (home / 'config.yaml').write_text('plugins:\n  enabled: [task-overview]\n')
    manager = PluginManager()
    try:
        manager.discover_and_load()
        loaded = manager._plugins['task-overview']
        assert loaded.manifest.source == 'bundled'
        assert loaded.enabled is enabled
        assert ('task-overview-read' in manager._plugin_commands) is enabled
        if enabled:
            handler = manager._plugin_commands['task-overview-read']['handler']
            assert json.loads(handler('{}'))['ok'] is False
    finally:
        manager.unload()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
