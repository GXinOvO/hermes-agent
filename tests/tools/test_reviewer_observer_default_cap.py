"""Default-cap reproduction through actual delegation and cleanup."""
import json
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import cleanup_task_resources
from tests.tools import test_delegate_output_schema as delegate_fixtures
from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_state, file_tools as ft
from tools.delegate_tool import _run_single_child

remote_project = backend_fixtures.remote_project


def test_default_4096_read_cap_cannot_erase_observer_parent_display(remote_project, monkeypatch):
    project, _env, task = remote_project
    registry = file_state.FileStateRegistry()
    monkeypatch.setattr(file_state, '_registry', registry)
    assert file_state._MAX_PATHS_PER_AGENT == 4096
    (project / 'known.txt').write_text('parent known\n')
    (project / 'link.txt').symlink_to('known.txt')
    parent_path = '/workspace/link.txt'
    assert 'content' in json.loads(ft.read_file_tool(parent_path, task_id=task))
    writer = 'cap-native-host-writer'
    native_target = str(project / 'known.txt')
    assert 'content' in json.loads(ft.read_file_tool(native_target, task_id=writer))

    class Parent(delegate_fixtures._StubParent):
        _current_task_id = task

    class Child(delegate_fixtures._StubChild):
        def run_conversation(self, *args, **kwargs):
            assert registry._write_observers
            # Legitimate registry population under the unchanged 4096 cap;
            # synthetic paths avoid 4096 unrelated backend subprocesses.
            for index in range(file_state._MAX_PATHS_PER_AGENT):
                registry.record_read(task, str(project / f'later-{index}'), mtime=1)
            assert len(registry.known_reads(task)) == 4096
            result = json.loads(ft.write_file_tool(native_target, 'native child wrote\n', task_id=writer))
            assert result.get('verified'), result
            cleanup_task_resources(SimpleNamespace(verbose_logging=False), writer)
            assert not registry._last_writer
            return super().run_conversation(*args, **kwargs)

    child = Child(['{"status":"ok"}'])
    child._delegate_output_schema = {'type': 'object'}
    result = _run_single_child(0, 'default read cap display retention', child, Parent())
    print(json.dumps({'result': result, 'expected_stale_paths': [parent_path], 'read_cap': file_state._MAX_PATHS_PER_AGENT}, sort_keys=True))
    assert result['status'] == 'completed' and result['schema_valid'] is True, result
    assert result['summary'] == '{"status":"ok"}'
    assert (project / 'known.txt').read_text() == 'native child wrote\n'
    assert not registry._write_observers and not registry._last_writer
    assert result.get('stale_paths') == [parent_path], result
