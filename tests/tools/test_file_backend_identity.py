import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent import terminal_env_registry
from tools import file_tools as ft
from tools import terminal_tool as tt
from tools.file_operations import ShellFileOperations


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def remote_project(tmp_path, monkeypatch):
    location = ROOT / 'tests' / 'fixtures' / 'file_state_provider' / 'plugin.py'
    spec = importlib.util.spec_from_file_location('file_state_test_provider', location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'TASK_ROOT', tmp_path)
    monkeypatch.delenv('HERMES_DISABLE_FILE_STATE_GUARD', raising=False)
    project = tmp_path / 'project'
    project.mkdir()
    leases = tmp_path / 'leases'
    provider = module.FixedProjectProvider(project, leases, 'private')
    terminal_env_registry.register_provider(provider)
    env = module.FixedProjectEnvironment(project, leases / 'main', 'private', timeout=4)
    env._hermes_backend_name = provider.name
    task = 'remote-file-state'
    tt.register_task_env_overrides(task, {'env_type': provider.name, 'cwd': '/workspace'})
    key = tt._resolve_container_task_id(task)
    with tt._env_lock:
        tt._active_environments[key] = env
    with ft._file_ops_lock:
        ft._file_ops_cache[key] = ShellFileOperations(env)
    try:
        assert env.execute('printf isolated-ready')['output'] == 'isolated-ready'
        yield project, env, task
    finally:
        ft.clear_file_ops_cache(task)
        with tt._env_lock:
            tt._active_environments.pop(key, None)
        env.cleanup()
        assert env.proc.poll() is not None


@pytest.mark.platforms('linux')
@pytest.mark.parametrize(('target', 'exc'), [
    ('/proc/self/mountinfo', 'FileNotFoundError'),
    ('/proc/sys/kernel/random/boot_id', 'PermissionError'),
])
def test_linux_probe_refuses_unavailable_mount_identity(tmp_path, target, exc):
    from tools.file_backend_probe import _PROBE

    script = (
        "import builtins,sys\n"
        "original = builtins.open\n"
        "def guarded(path, *args, **kwargs):\n"
        f"    if str(path) == {target!r}: raise {exc}(path)\n"
        "    return original(path, *args, **kwargs)\n"
        "builtins.open = guarded\n"
        f"sys.argv = ['probe', {str(tmp_path)!r}, 'metadata']\n"
        f"exec({_PROBE!r})\n"
    )
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode != 0, result.stdout
    assert target in result.stderr


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('missing', ['/proc/self/mountinfo', '/proc/sys/kernel/random/boot_id'])
def test_unavailable_mount_identity_refuses_existing_and_new_writes(remote_project, monkeypatch, missing):
    project, env, task = remote_project
    target = project / 'mount-identity.txt'
    target.write_text('original\n')
    assert 'content' in json.loads(ft.read_file_tool('/workspace/mount-identity.txt', task_id=task))
    probe = env.execute_file_probe
    calls = []

    def without_identity(command, **kwargs):
        if missing in command:
            calls.append(missing)
            command = command.replace(missing, '/proc/hermes-test-missing-mount-identity')
        return probe(command, **kwargs)

    monkeypatch.setattr(env, 'execute_file_probe', without_identity)
    for name in ('mount-identity.txt', 'new-mount-identity.txt'):
        result = json.loads(ft.write_file_tool('/workspace/' + name, 'must not be written\n', task_id=task))
        assert result.get('error') and not result.get('verified'), result
    assert calls
    assert target.read_text() == 'original\n'
    assert not (project / 'new-mount-identity.txt').exists()


def test_failed_remote_probe_cannot_overwrite_unread(remote_project):
    project, env, task = remote_project
    target = project / 'unread.txt'
    target.write_text('must read before rewrite\n')
    result = env.execute("mkdir -p /workspace/fakebin; printf '#!/bin/sh\\nexit 19\\n' > /workspace/fakebin/python3; chmod +x /workspace/fakebin/python3; export PATH=/workspace/fakebin:/usr/bin:/bin")
    assert result['returncode'] == 0
    assert env.execute('python3 -c pass')['returncode'] == 19
    try:
        write = json.loads(ft.write_file_tool('/workspace/unread.txt', 'overwritten without read\n', task_id=task))
        assert not write.get('verified'), write
        assert target.read_text() == 'must read before rewrite\n'
    finally:
        env.execute('export PATH=/usr/bin:/bin')


def test_context_reset_retains_remote_full_baseline(remote_project):
    from agent.read_context import reset_read_context
    from tools import file_tools_read_tracking as rt

    project, _env, task = remote_project
    (project / 'reset.txt').write_text('known complete\n')
    first = json.loads(ft.read_file_tool('/workspace/reset.txt', task_id=task))
    assert 'known complete' in first['content']
    assert '/workspace/reset.txt' in rt._read_tracker[task]['full_write_baselines']
    reset_read_context(object(), task)
    assert '/workspace/reset.txt' in rt._read_tracker[task]['full_write_baselines']
    write = json.loads(ft.write_file_tool('/workspace/reset.txt', 'next legitimate\n', task_id=task))
    assert write.get('verified'), write
    assert (project / 'reset.txt').read_text() == 'next legitimate\n'


def test_remote_probe_does_not_hold_global_tracker_lock(remote_project, monkeypatch):
    import threading
    from tools import file_backend_io as bio
    from tools import file_tools_read_tracking as rt

    project, env, task = remote_project
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = env._run_bash

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(env, '_run_bash', delayed)
    with rt._read_tracker_lock:
        data = rt._task_data(task)
    errors = []

    def slow():
        token = bio._active.set(env)
        try:
            ft._record_successful_read(data, task, '/workspace/lock.txt', '/workspace/lock.txt',
                                       1, 1, ('/workspace/lock.txt', 1, 1), partial=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            bio._active.reset(token)

    (project / 'lock.txt').write_text('lock probe\n')
    first = threading.Thread(target=slow)
    second = threading.Thread(target=lambda: (rt.notify_other_tool_call('unrelated-task'), completed.set()))
    first.start()
    assert entered.wait(3)
    second.start()
    try:
        unrelated_completed = completed.wait(2)
    finally:
        release.set()
        first.join(8)
        second.join(8)
    assert not errors, errors
    assert not first.is_alive() and not second.is_alive()
    assert unrelated_completed, 'unrelated task blocked on remote probe under global tracker lock'


def test_different_projects_do_not_share_path_authority(remote_project, tmp_path):
    project, env, task = remote_project
    module = sys.modules['file_state_test_provider']
    other = tmp_path / 'independent-project'
    other.mkdir()
    (project / 'same.txt').write_text('project A\n')
    (other / 'same.txt').write_text('project B\n')
    second = module.FixedProjectEnvironment(other, tmp_path / 'leases' / 'other', 'private', timeout=4)
    second._hermes_backend_name = env._hermes_backend_name
    other_task = 'different-project-task'
    tt.register_task_env_overrides(other_task, {'env_type': env._hermes_backend_name, 'cwd': '/workspace'})
    key = tt._resolve_container_task_id(other_task)
    tt._active_environments[key] = second
    ft._file_ops_cache[key] = ShellFileOperations(second)
    try:
        for owner in (task, other_task):
            assert 'content' in json.loads(ft.read_file_tool('/workspace/same.txt', task_id=owner))
        b = json.loads(ft.write_file_tool('/workspace/same.txt', 'new B\n', task_id=other_task))
        a = json.loads(ft.write_file_tool('/workspace/same.txt', 'new A\n', task_id=task))
        assert a.get('verified') and b.get('verified'), (a, b)
        assert (project / 'same.txt').read_text() == 'new A\n'
        assert (other / 'same.txt').read_text() == 'new B\n'
    finally:
        ft.clear_file_ops_cache(other_task)
        tt._active_environments.pop(key, None)
        second.cleanup()
        assert second.proc.poll() is not None


def test_shared_project_retains_cross_owner_protection(remote_project, tmp_path):
    from tools.file_backend_io import backend_io_scope, storage_path
    from tools import file_state

    project, env, task = remote_project
    module = sys.modules['file_state_test_provider']
    (project / 'shared.txt').write_text('shared content\n')
    second = module.FixedProjectEnvironment(project, tmp_path / 'leases' / 'shared', 'private', timeout=4)
    second._hermes_backend_name = env._hermes_backend_name
    sibling = 'shared-project-sibling'
    tt.register_task_env_overrides(sibling, {'env_type': env._hermes_backend_name, 'cwd': '/workspace'})
    key = tt._resolve_container_task_id(sibling)
    tt._active_environments[key] = second
    ft._file_ops_cache[key] = ShellFileOperations(second)
    try:
        with backend_io_scope(task):
            first_identity = storage_path('/workspace/shared.txt')
        with backend_io_scope(sibling):
            assert first_identity == storage_path('/workspace/shared.txt')
        assert env._session_id != second._session_id
        for owner in (task, sibling):
            assert 'content' in json.loads(ft.read_file_tool('/workspace/shared.txt', task_id=owner))
        assert json.loads(ft.write_file_tool('/workspace/shared.txt', 'sibling edit\n', task_id=sibling)).get('verified')
        assert file_state.writes_since(task, 0, file_state.known_reads(task)).get(sibling) == ['/workspace/shared.txt']
        blocked = json.loads(ft.write_file_tool('/workspace/shared.txt', 'stale parent\n', task_id=task))
        assert blocked.get('stale_write_blocked'), blocked
        assert (project / 'shared.txt').read_text() == 'sibling edit\n'
    finally:
        ft.clear_file_ops_cache(sibling)
        tt._active_environments.pop(key, None)
        second.cleanup()
        assert second.proc.poll() is not None


def test_generation_reset_invalidates_remote_cache_without_host_io(remote_project, monkeypatch):
    import os
    from tools import file_tools_read_tracking as rt

    project, env, task = remote_project
    (project / 'generation.txt').write_text('stable generation\n')
    assert 'content' in json.loads(ft.read_file_tool('/workspace/generation.txt', task_id=task))
    original_stat = os.stat
    outside = []

    def guarded_stat(path, *args, **kwargs):
        if str(path).startswith('/workspace/'):
            outside.append(str(path))
            raise AssertionError('remote target reached host stat')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', guarded_stat)
    old = env._session_id
    env._session_id = old + '-next'
    rt.reset_file_dedup(task)
    assert not rt._read_tracker[task]['full_write_baselines']
    blocked = json.loads(ft.write_file_tool('/workspace/generation.txt', 'unseen generation\n', task_id=task))
    assert not blocked.get('verified'), blocked
    assert 'content' in json.loads(ft.read_file_tool('/workspace/generation.txt', task_id=task))
    assert json.loads(ft.write_file_tool('/workspace/generation.txt', 'refreshed generation\n', task_id=task)).get('verified')
    assert outside == []


@pytest.mark.parametrize('failure', ['exit', 'malformed', 'permission', 'hash', 'identity', 'timeout', 'unbound'])
def test_probe_failures_refuse_existing_and_new_writes(remote_project, monkeypatch, failure):
    from tools import file_backend_io as bio
    from tools import file_backend_probe as bp

    project, env, task = remote_project
    (project / 'failure.txt').write_text('protected original\n')
    with bio.backend_io_scope(task):
        assert bio.file_exists('/workspace/failure.txt')
    original = bp._execute

    def fail(backend, command, **kwargs):
        if failure == 'exit':
            raise bp.FileBackendUnavailable('probe exit 19')
        if failure == 'timeout':
            raise TimeoutError('probe timeout')
        if failure == 'malformed':
            return {'state': 'present'}
        value = original(backend, command, **kwargs)
        if failure == 'permission':
            value.update(state='unavailable', errno=13)
        elif failure == 'hash':
            value['sha256'] = 'not-a-hash'
        elif failure == 'identity':
            value['identity'][0] += 1
        return value

    if failure == 'unbound':
        env._session_id = None
    else:
        monkeypatch.setattr(bp, '_execute', fail)
    for name in ('failure.txt', 'new-failure.txt'):
        try:
            result = json.loads(ft.write_file_tool('/workspace/' + name, 'must refuse\n', task_id=task))
        except bp.FileBackendUnavailable:
            result = {'error': 'unbound identity refused'}
        assert not result.get('verified'), (failure, name, result)
    assert (project / 'failure.txt').read_text() == 'protected original\n'
    assert not (project / 'new-failure.txt').exists()


def test_absence_is_only_enoent_or_enotdir(remote_project):
    from tools import file_backend_io as bio
    from tools.file_backend_probe import probe_environment

    project, env, task = remote_project
    (project / 'plain.txt').write_text('plain\n')
    for path in ('/workspace/missing.txt', '/workspace/plain.txt/child.txt'):
        result = probe_environment(env, path)
        assert result.state == 'absent', result
        with bio.backend_io_scope(task):
            assert not bio.file_exists(path)
    made = json.loads(ft.write_file_tool('/workspace/missing.txt', 'legitimate new\n', task_id=task))
    assert made.get('verified'), made
    assert (project / 'missing.txt').read_text() == 'legitimate new\n'


def test_remote_full_partial_stale_patch_search_and_ipynb(remote_project, monkeypatch):
    import builtins
    import os
    original_open = builtins.open
    original_os_open = os.open
    outside = []

    def guarded_open(path, *args, **kwargs):
        if str(path).startswith('/workspace/'):
            outside.append(str(path))
            raise AssertionError('remote body reached host open')
        return original_open(path, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        if str(path).startswith('/workspace/'):
            outside.append(str(path))
            raise AssertionError('remote body reached host os.open')
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(os, 'open', guarded_os_open)
    project, _env, task = remote_project
    (project / 'document.txt').write_text('one\ntwo\nthree\n')
    partial = json.loads(ft.read_file_tool('/workspace/document.txt', limit=1, task_id=task))
    assert partial.get('truncated'), partial
    assert json.loads(ft.write_file_tool('/workspace/document.txt', 'blind\n', task_id=task)).get('stale_write_blocked')
    for offset in (2, 3):
        assert 'content' in json.loads(ft.read_file_tool('/workspace/document.txt', offset=offset, limit=1, task_id=task))
    patch = json.loads(ft.patch_tool(path='/workspace/document.txt', old_string='two', new_string='second', task_id=task))
    assert patch.get('success'), patch
    assert json.loads(ft.write_file_tool('/workspace/document.txt', 'known changed\n', task_id=task)).get('verified')
    found = json.loads(ft.search_tool('known', target='content', path='/workspace', task_id=task))
    assert found.get('total_count', 0) >= 1, found
    (project / 'document.txt').write_text('external replacement\n')
    assert not json.loads(ft.write_file_tool('/workspace/document.txt', 'stale replacement\n', task_id=task)).get('verified')
    notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {}, 'cells': [
        {'cell_type': 'code', 'execution_count': None, 'metadata': {}, 'outputs': [], 'source': ['print(1)\n']}]}
    (project / 'note.ipynb').write_text(json.dumps(notebook))
    result = json.loads(ft.read_file_tool('/workspace/note.ipynb', task_id=task))
    assert 'print(1)' in result.get('content', ''), result
    notebook['cells'][0]['source'] = ['print(2)\n']
    assert json.loads(ft.write_file_tool('/workspace/note.ipynb', json.dumps(notebook), task_id=task)).get('verified')
    assert json.loads((project / 'note.ipynb').read_text()) == notebook
    assert outside == []


def test_context_reset_from_plain_thread_and_bulk_reset(remote_project):
    import threading
    from agent.read_context import reset_read_context
    from tools import file_tools_read_tracking as rt
    from tools.file_backend_io import _active

    project, _env, task = remote_project
    (project / 'thread.txt').write_text('complete thread view\n')
    assert 'content' in json.loads(ft.read_file_tool('/workspace/thread.txt', task_id=task))
    observations = []

    def reset():
        observations.append(_active.get())
        reset_read_context(object(), task)
        rt.reset_file_dedup()
        observations.append('/workspace/thread.txt' in rt._read_tracker[task]['full_write_baselines'])

    thread = threading.Thread(target=reset)
    thread.start()
    thread.join(8)
    assert not thread.is_alive()
    assert observations == [None, True]
    assert json.loads(ft.write_file_tool('/workspace/thread.txt', 'legitimate thread write\n', task_id=task)).get('verified')


def test_late_read_probe_cannot_publish_after_reset(remote_project, monkeypatch):
    import contextvars
    import threading
    from tools import file_backend_io as bio
    from tools import file_tools_read_tracking as rt

    project, env, task = remote_project
    (project / 'late.txt').write_text('old read body\n')
    with bio.backend_io_scope(task):
        version = rt._file_version('/workspace/late.txt')
        context = contextvars.copy_context()
    with rt._read_tracker_lock:
        data = rt._task_data(task)
    entered, release = threading.Event(), threading.Event()
    original = env._run_bash
    errors = []

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(env, '_run_bash', delayed)

    def late_read():
        try:
            ft._record_successful_read(data, task, '/workspace/late.txt', '/workspace/late.txt',
                                       1, 50, ('/workspace/late.txt', 1, 50), partial=False,
                                       version_before=version[:-1], snapshot=version)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=lambda: context.run(late_read))
    thread.start()
    assert entered.wait(3)
    rt.reset_file_dedup(task)
    release.set()
    thread.join(8)
    assert not thread.is_alive() and not errors, errors
    assert not data['full_write_baselines']
    assert not data['dedup_generation_reads']


def test_interpreter_probe_ignores_exported_path_and_keeps_python(remote_project):
    project, env, task = remote_project
    (project / 'exports.txt').write_text('exports supported\n')
    result = env.execute("export FILE_STATE_VALUE=retained; export PATH=/workspace/fakebin:/usr/bin:/bin; python3 -c 'print(6 * 7)'")
    assert result['returncode'] == 0 and '42' in result['output']
    assert 'content' in json.loads(ft.read_file_tool('/workspace/exports.txt', task_id=task))
    assert json.loads(ft.write_file_tool('/workspace/exports.txt', 'exported path write\n', task_id=task)).get('verified')
    assert env.execute('printf %s "$FILE_STATE_VALUE"')['output'] == 'retained'


def test_async_backend_context_is_task_local(remote_project):
    import asyncio
    from tools import file_backend_io as bio
    from tools import file_tools_read_tracking as rt

    project, env, task = remote_project
    (project / 'async.txt').write_text('context\n')

    async def remote():
        with bio.backend_io_scope(task):
            await asyncio.sleep(0)
            assert bio._active.get() is env
            assert rt._file_metadata('/workspace/async.txt') is not None

    async def unrelated():
        await asyncio.sleep(0)
        assert bio._active.get() is None

    async def run():
        await asyncio.gather(remote(), unrelated())

    asyncio.run(run())
    assert bio._active.get() is None


@pytest.mark.parametrize('backend', ['ssh', 'docker', 'modal', 'daytona', 'vercel_sandbox'])
def test_common_backend_names_keep_real_namespace_file_io(remote_project, backend, monkeypatch):
    from tools.file_backend_io import backend_io_scope, storage_path

    project, env, task = remote_project
    monkeypatch.setattr(env, '_hermes_backend_name', backend)
    (project / 'common.txt').write_text('real namespace path\n')
    first = json.loads(ft.read_file_tool('/workspace/common.txt', task_id=task))
    assert 'real namespace path' in first.get('content', ''), first
    with backend_io_scope(task):
        assert storage_path('/workspace/common.txt').startswith('hermes-storage://')
    result = json.loads(ft.write_file_tool('/workspace/common.txt', 'common name write\n', task_id=task))
    assert result.get('verified'), result
    assert (project / 'common.txt').read_text() == 'common name write\n'


@pytest.mark.parametrize('command', ['exit 19', "printf 'malformed-json'", "printf '{\"state\":\"absent\",\"errno\":13}'"])
def test_actual_probe_transport_errors_cannot_create_absence(remote_project, monkeypatch, command):
    from tools.file_backend_io import backend_io_scope, file_exists

    project, env, task = remote_project
    (project / 'transport.txt').write_text('known current\n')
    assert 'content' in json.loads(ft.read_file_tool('/workspace/transport.txt', task_id=task))
    original = env._run_bash
    monkeypatch.setattr(env, '_run_bash', lambda *args, **kwargs: original(command, **kwargs))
    with backend_io_scope(task):
        from tools.file_backend_probe import FileBackendUnavailable
        with pytest.raises(FileBackendUnavailable):
            file_exists('/workspace/transport.txt')
    result = json.loads(ft.write_file_tool('/workspace/transport.txt', 'forbidden\n', task_id=task))
    assert not result.get('verified'), result
    assert (project / 'transport.txt').read_text() == 'known current\n'


def test_failed_hash_cannot_use_existing_full_baseline(remote_project, monkeypatch):
    from tools import file_backend_probe as bp

    project, _env, task = remote_project
    (project / 'hash.txt').write_text('known hash\n')
    assert 'content' in json.loads(ft.read_file_tool('/workspace/hash.txt', task_id=task))
    original = bp._execute

    def bad_hash(backend, command, **kwargs):
        value = original(backend, command, **kwargs)
        if command.endswith(' hash'):
            value.pop('sha256', None)
        return value

    monkeypatch.setattr(bp, '_execute', bad_hash)
    result = json.loads(ft.write_file_tool('/workspace/hash.txt', 'forbidden\n', task_id=task))
    assert not result.get('verified'), result
    assert (project / 'hash.txt').read_text() == 'known hash\n'


def test_shared_host_and_remote_project_use_one_registry_key(remote_project):
    from tools import file_state
    from tools.file_backend_io import backend_io_scope, storage_path

    project, _env, task = remote_project
    target = project / 'host-shared.txt'
    target.write_text('host read current\n')
    host_key = storage_path(str(target))
    file_state.record_read('host-observer', str(target))
    try:
        with backend_io_scope(task):
            assert storage_path('/workspace/host-shared.txt') == host_key
        assert 'content' in json.loads(ft.read_file_tool('/workspace/host-shared.txt', task_id=task))
        assert json.loads(ft.write_file_tool('/workspace/host-shared.txt', 'remote edited\n', task_id=task)).get('verified')
        assert 'sibling subagent' in file_state.check_stale('host-observer', str(target))
    finally:
        file_state.get_registry().forget_task('host-observer')


def test_remote_missing_search_cache_never_probes_host(remote_project, monkeypatch):
    import os

    _project, _env, task = remote_project
    first = json.loads(ft.search_tool('needle', path='/workspace/missing-root', task_id=task))
    assert first.get('error', '').startswith('Path not found:'), first
    original = os.stat
    outside = []

    def guarded(path, *args, **kwargs):
        if str(path).startswith('/workspace/'):
            outside.append(str(path))
            raise AssertionError('remote search reached host stat')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', guarded)
    second = json.loads(ft.search_tool('needle', path='/workspace/missing-root', task_id=task))
    assert outside == [], second
    assert second.get('error', '').startswith('Path not found:'), second
