"""Independent minimal probes; product and existing tests remain unchanged."""
import json

import pytest

from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_state
from tools import file_tools as ft
from tools.file_backend_io import backend_io_scope, storage_path

remote_project = backend_fixtures.remote_project


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    registry = file_state.FileStateRegistry()
    monkeypatch.setattr(file_state, '_registry', registry)
    return registry


def test_fixed_symlink_shared_key_and_parent_display(remote_project, isolated_registry):
    project, _env, task = remote_project
    (project / 'known.txt').write_text('known original\n')
    (project / 'link.txt').symlink_to('known.txt')
    assert 'known original' in json.loads(ft.read_file_tool('/workspace/link.txt', task_id=task))['content']
    with backend_io_scope(task):
        assert storage_path('/workspace/link.txt') == storage_path('/workspace/known.txt')
    result = json.loads(ft.write_file_tool('/workspace/link.txt', 'legitimate known write\n', task_id=task))
    assert result.get('verified'), result
    assert (project / 'known.txt').read_text() == 'legitimate known write\n'
    _, _, observer = isolated_registry.observe_writes(task)
    try:
        with backend_io_scope(task):
            isolated_registry.note_write('sibling', '/workspace/known.txt', mtime=2)
        isolated_registry.forget_task('sibling')
        assert observer.snapshot() == ['/workspace/link.txt']
    finally:
        observer.close()


@pytest.mark.parametrize('initial', ['known-link', 'absent'])
def test_retarget_at_real_commit_cannot_overwrite_unread(remote_project, monkeypatch, initial):
    project, env, task = remote_project
    (project / 'known.txt').write_text('known original\n')
    (project / 'unread.txt').write_text('unread protected\n')
    if initial == 'known-link':
        (project / 'link.txt').symlink_to('known.txt')
        assert 'known original' in json.loads(ft.read_file_tool('/workspace/link.txt', task_id=task))['content']
    operations = ft._get_file_ops(task)
    atomic_write = operations._atomic_write
    injections = []

    def retarget(path, content):
        assert path == '/workspace/link.txt'
        changed = env.execute('ln -sfn unread.txt /workspace/link.txt')
        assert changed['returncode'] == 0, changed
        injections.append({'path': path, 'stage': 'ShellFileOperations._atomic_write entry'})
        return atomic_write(path, content)

    monkeypatch.setattr(operations, '_atomic_write', retarget)
    result = json.loads(ft.write_file_tool('/workspace/link.txt', 'overwritten without reading target\n', task_id=task))
    observed = {'initial': initial, 'injections': injections, 'result': result,
                'known': (project / 'known.txt').read_text(), 'unread': (project / 'unread.txt').read_text()}
    print(json.dumps(observed, sort_keys=True))
    assert injections
    assert not result.get('verified') and observed['unread'] == 'unread protected\n', observed


def test_live_observer_retains_parent_display_after_read_cap_eviction(remote_project, isolated_registry, monkeypatch):
    project, _env, task = remote_project
    for name in ('known.txt', 'later-one.txt', 'later-two.txt'):
        (project / name).write_text(name + '\n')
    (project / 'link.txt').symlink_to('known.txt')
    monkeypatch.setattr(file_state, '_MAX_PATHS_PER_AGENT', 2)
    assert 'content' in json.loads(ft.read_file_tool('/workspace/link.txt', task_id=task))
    _, _, observer = isolated_registry.observe_writes(task)
    try:
        for name in ('later-one.txt', 'later-two.txt'):
            assert 'content' in json.loads(ft.read_file_tool('/workspace/' + name, task_id=task))
        assert len(isolated_registry.known_reads(task)) == 2
        with backend_io_scope(task):
            isolated_registry.note_write('sibling', '/workspace/known.txt', mtime=2)
        isolated_registry.forget_task('sibling')
        actual = observer.snapshot()
        print(json.dumps({'expected': ['/workspace/link.txt'], 'actual': actual, 'live_writers': isolated_registry._last_writer}))
        assert actual == ['/workspace/link.txt'], actual
    finally:
        observer.close()
