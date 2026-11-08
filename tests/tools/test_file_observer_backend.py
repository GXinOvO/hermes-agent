import json
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import cleanup_task_resources
from tests.tools import test_delegate_output_schema as fixtures
from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_state, file_tools as ft, terminal_tool as tt
from tools.file_operations import ShellFileOperations

remote_project = backend_fixtures.remote_project


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(file_state, '_registry', file_state.FileStateRegistry())
    return file_state.get_registry()


@pytest.mark.parametrize('mode', ['shared-project', 'different-project', 'host-parent', 'next-generation'])
def test_native_backend_observation_survives_cleanup_with_parent_display(remote_project, tmp_path, registry, mode):
    import sys

    project, parent_env, parent_task = remote_project
    module = sys.modules['file_state_test_provider']
    (project / 'observed.txt').write_text('parent-before\n')
    parent_path = '/workspace/observed.txt'
    if mode == 'host-parent':
        parent_task = 'host-observer-parent'
        parent_path = str(project / 'observed.txt')
    assert 'content' in json.loads(ft.read_file_tool(parent_path, task_id=parent_task))
    writer_project = project
    if mode == 'different-project':
        writer_project = tmp_path / 'other-project'
        writer_project.mkdir()
        (writer_project / 'observed.txt').write_text('other-before\n')
    writer = 'observer-nested-writer'
    env = module.FixedProjectEnvironment(writer_project, tmp_path / 'leases' / 'writer', 'private', timeout=4)
    env._hermes_backend_name = parent_env._hermes_backend_name
    if mode == 'next-generation':
        env._session_id += '-next'
    tt.register_task_env_overrides(writer, {'env_type': env._hermes_backend_name, 'cwd': '/workspace'})
    key = tt._resolve_container_task_id(writer)
    with tt._env_lock:
        tt._active_environments[key] = env
    with ft._file_ops_lock:
        ft._file_ops_cache[key] = ShellFileOperations(env)

    class Parent(fixtures._StubParent):
        _current_task_id = parent_task

    class Child(fixtures._StubChild):
        def run_conversation(self, *args, **kwargs):
            assert 'content' in json.loads(ft.read_file_tool('/workspace/observed.txt', task_id=writer))
            result = json.loads(ft.write_file_tool('/workspace/observed.txt', 'writer-after\n', task_id=writer))
            assert result.get('verified'), result
            cleanup_task_resources(SimpleNamespace(verbose_logging=False), writer)
            assert not registry._last_writer
            return super().run_conversation(*args, **kwargs)

    child = Child(['{"status":"ok"}'])
    child._delegate_output_schema = {'type': 'object'}
    try:
        from tools.delegate_tool import _run_single_child
        result = _run_single_child(0, 'native storage observation fixture', child, Parent())
        assert result['status'] == 'completed' and result['schema_valid'] is True, result
        assert result['summary'] == '{"status":"ok"}'
        assert (writer_project / 'observed.txt').read_bytes() == b'writer-after\n'
        if mode == 'different-project':
            assert not result.get('stale_paths'), result
            assert (project / 'observed.txt').read_bytes() == b'parent-before\n'
        else:
            assert result.get('stale_paths') == [parent_path], result
            assert all(not p.startswith('hermes-storage://') for p in result['stale_paths'])
        assert not registry._write_observers
    finally:
        ft.clear_file_ops_cache(writer)
        with tt._env_lock:
            tt._active_environments.pop(key, None)
        env.cleanup()
        assert env.proc.poll() is not None


def test_display_translation_does_not_hold_state_lock(tmp_path, registry, monkeypatch):
    path = str(tmp_path / 'watched')
    registry.record_read('parent', path, mtime=1)
    _, _, observer = registry.observe_writes('parent')
    assert observer is not None
    registry.note_write('writer', path, mtime=2)
    registry.forget_task('writer')
    original = registry.display_paths

    def display(parent, keys):
        acquired = registry._state_lock.acquire(blocking=False)
        assert acquired, 'display translation reentered non-reentrant registry state lock'
        registry._state_lock.release()
        return original(parent, keys)

    monkeypatch.setattr(registry, 'display_paths', display)
    try:
        assert observer.snapshot() == [path]
    finally:
        observer.close()
