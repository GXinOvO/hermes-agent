import json

import pytest

from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_backend_io as io
from tools import file_tools as ft
from tools import file_tools_read_tracking as tracking
from tools import terminal_tool as terminal
from tools.file_backend_probe import FileBackendUnavailable
from tools.file_operations import ShellFileOperations

remote_project = backend_fixtures.remote_project


def test_midcall_remote_generation_loss_requires_new_read(remote_project):
    project, environment, task = remote_project
    path = '/workspace/revocation.txt'
    (project / 'revocation.txt').write_text('original full content\n', encoding='utf-8')
    assert 'content' in json.loads(ft.read_file_tool(path, task_id=task))
    saved = environment._session_id
    with pytest.raises(FileBackendUnavailable):
        with io.backend_io_scope(task):
            environment._session_id += '-rejected'
            io._check_generation(environment)
    environment._session_id = saved
    refused = json.loads(ft.write_file_tool(path, 'stale write\n', task_id=task))
    assert refused.get('error') and not refused.get('verified'), refused
    assert (project / 'revocation.txt').read_text(encoding='utf-8') == 'original full content\n'
    assert 'content' in json.loads(ft.read_file_tool(path, task_id=task))
    recovered = json.loads(ft.write_file_tool(path, 'new read accepted\n', task_id=task))
    assert recovered.get('verified'), recovered


def test_revocation_preserves_unrelated_environment(remote_project, tmp_path):
    project, first, task = remote_project
    module = backend_fixtures.sys.modules['file_state_test_provider']
    independent = tmp_path / 'unrelated-project'
    independent.mkdir()
    (project / 'first.txt').write_text('first task\n', encoding='utf-8')
    (independent / 'second.txt').write_text('second task\n', encoding='utf-8')
    second = module.FixedProjectEnvironment(independent, tmp_path / 'second-lease', 'private', timeout=4)
    second._hermes_backend_name = first._hermes_backend_name
    other = 'revocation-unrelated-task'
    terminal.register_task_env_overrides(other, {'env_type': second._hermes_backend_name, 'cwd': '/workspace'})
    key = terminal._resolve_container_task_id(other)
    terminal._active_environments[key] = second
    ft._file_ops_cache[key] = ShellFileOperations(second)
    try:
        assert 'content' in json.loads(ft.read_file_tool('/workspace/first.txt', task_id=task))
        assert 'content' in json.loads(ft.read_file_tool('/workspace/second.txt', task_id=other))
        before = dict(tracking._read_tracker[other]['full_write_baselines'])
        io.revoke_backend_reads(first)
        assert not tracking._read_tracker[task]['full_write_baselines']
        assert tracking._read_tracker[other]['full_write_baselines'] == before
        result = json.loads(ft.write_file_tool('/workspace/second.txt', 'other authority preserved\n', task_id=other))
        assert result.get('verified'), result
        assert (independent / 'second.txt').read_text(encoding='utf-8') == 'other authority preserved\n'
    finally:
        ft.clear_file_ops_cache(other)
        terminal._active_environments.pop(key, None)
        terminal.clear_task_env_overrides(other)
        second.cleanup()
        assert second.proc.poll() is not None


@pytest.mark.parametrize('observer_has_read', [True, False], ids=['known-observer', 'new-observer'])
def test_entry_generation_loss_revokes_shared_tasks(remote_project, observer_has_read):
    project, environment, observer = remote_project
    path = '/workspace/shared-entry.txt'
    (project / 'shared-entry.txt').write_text('initial shared complete read\n', encoding='utf-8')
    reader = observer + '-retained-reader'
    terminal.register_task_env_overrides(reader, {'env_type': environment._hermes_backend_name, 'cwd': '/workspace'})
    key = terminal._resolve_container_task_id(reader)
    terminal._active_environments[key] = environment
    ft._file_ops_cache[key] = ShellFileOperations(environment)
    saved = environment._session_id
    try:
        assert 'content' in json.loads(ft.read_file_tool(path, task_id=reader))
        if observer_has_read:
            assert 'content' in json.loads(ft.read_file_tool(path, task_id=observer))
        revision = tracking._read_tracker[reader].get('io_revision', 0)
        environment._session_id = saved + '-changed-at-entry'
        observed = json.loads(ft.write_file_tool(path, 'observer may not overwrite\n', task_id=observer))
        environment._session_id = saved
        reverted = json.loads(ft.write_file_tool(path, 'stale reader may not overwrite\n', task_id=reader))
        assert observed.get('error') and not observed.get('verified'), observed
        assert reverted.get('error') and not reverted.get('verified'), reverted
        assert (project / 'shared-entry.txt').read_text(encoding='utf-8') == 'initial shared complete read\n'
        assert tracking._read_tracker[reader].get('io_revision', 0) > revision
        reread = json.loads(ft.read_file_tool(path, task_id=reader))
        recovery = json.loads(ft.write_file_tool(path, 'shared reader recovered\n', task_id=reader))
        assert 'content' in reread and not reread.get('dedup') and recovery.get('verified'), (reread, recovery)
        assert (project / 'shared-entry.txt').read_text(encoding='utf-8') == 'shared reader recovered\n'
    finally:
        environment._session_id = saved
        ft.clear_file_ops_cache(reader)
        terminal._active_environments.pop(key, None)
        terminal.clear_task_env_overrides(reader)


def test_entry_revocation_preserves_distinct_environment_and_host(remote_project, tmp_path):
    project, changed, task = remote_project
    module = backend_fixtures.sys.modules['file_state_test_provider']
    independent = tmp_path / 'entry-independent-project'
    independent.mkdir()
    (project / 'changed.txt').write_text('changed domain\n', encoding='utf-8')
    (independent / 'independent.txt').write_text('unrelated domain\n', encoding='utf-8')
    host = tmp_path / 'host-positive.txt'
    host.write_text('host complete read\n', encoding='utf-8')
    other = task + '-independent'
    second = module.FixedProjectEnvironment(independent, tmp_path / 'independent-lease', 'private', timeout=4)
    second._hermes_backend_name = changed._hermes_backend_name
    second._session_id = changed._session_id
    terminal.register_task_env_overrides(other, {'env_type': second._hermes_backend_name, 'cwd': '/workspace'})
    key = terminal._resolve_container_task_id(other)
    terminal._active_environments[key] = second
    ft._file_ops_cache[key] = ShellFileOperations(second)
    try:
        assert 'content' in json.loads(ft.read_file_tool('/workspace/changed.txt', task_id=task))
        assert 'content' in json.loads(ft.read_file_tool('/workspace/independent.txt', task_id=other))
        assert 'content' in json.loads(ft.read_file_tool(str(host), task_id='default'))
        changed._session_id += '-next'
        refused = json.loads(ft.write_file_tool('/workspace/changed.txt', 'must re-read\n', task_id=task))
        assert refused.get('error') and not refused.get('verified'), refused
        other_write = json.loads(ft.write_file_tool('/workspace/independent.txt', 'unrelated authority retained\n', task_id=other))
        host_write = json.loads(ft.write_file_tool(str(host), 'host authority retained\n', task_id='default'))
        assert other_write.get('verified') and host_write.get('verified'), (other_write, host_write)
        assert (independent / 'independent.txt').read_text(encoding='utf-8') == 'unrelated authority retained\n'
        assert host.read_text(encoding='utf-8') == 'host authority retained\n'
    finally:
        ft.clear_file_ops_cache(other)
        terminal._active_environments.pop(key, None)
        terminal.clear_task_env_overrides(other)
        ft.clear_file_ops_cache('default')
        second.cleanup()
        assert second.proc.poll() is not None


def test_rejected_cross_environment_entry_cannot_revive_previous_binding(remote_project, tmp_path):
    project, first, task = remote_project
    module = backend_fixtures.sys.modules['file_state_test_provider']
    foreign = tmp_path / 'rejected-rebind-project'
    foreign.mkdir()
    (project / 'rebind.txt').write_text('prior environment full read\n', encoding='utf-8')
    (foreign / 'rebind.txt').write_text('different environment full read\n', encoding='utf-8')
    second = module.FixedProjectEnvironment(foreign, tmp_path / 'rejected-rebind-lease', 'private', timeout=4)
    second._hermes_backend_name = first._hermes_backend_name
    other = task + '-rebind-other'
    terminal.register_task_env_overrides(other, {'env_type': second._hermes_backend_name, 'cwd': '/workspace'})
    other_key = terminal._resolve_container_task_id(other)
    task_key = terminal._resolve_container_task_id(task)
    terminal._active_environments[other_key] = second
    ft._file_ops_cache[other_key] = ShellFileOperations(second)
    try:
        assert 'content' in json.loads(ft.read_file_tool('/workspace/rebind.txt', task_id=task))
        assert 'content' in json.loads(ft.read_file_tool('/workspace/rebind.txt', task_id=other))
        second._session_id += '-entry-transition'
        terminal._active_environments[task_key] = second
        ft._file_ops_cache[task_key] = ShellFileOperations(second)
        rejected = json.loads(ft.write_file_tool('/workspace/rebind.txt', 'unread second environment\n', task_id=task))
        terminal._active_environments[task_key] = first
        ft._file_ops_cache[task_key] = ShellFileOperations(first)
        reverted = json.loads(ft.write_file_tool('/workspace/rebind.txt', 'must not revive prior authority\n', task_id=task))
        assert rejected.get('error') and not rejected.get('verified'), rejected
        assert reverted.get('error') and not reverted.get('verified'), reverted
        assert (project / 'rebind.txt').read_text(encoding='utf-8') == 'prior environment full read\n'
        assert (foreign / 'rebind.txt').read_text(encoding='utf-8') == 'different environment full read\n'
        reread = json.loads(ft.read_file_tool('/workspace/rebind.txt', task_id=task))
        recovered = json.loads(ft.write_file_tool('/workspace/rebind.txt', 'fresh prior environment read\n', task_id=task))
        assert 'content' in reread and recovered.get('verified'), (reread, recovered)
        assert (project / 'rebind.txt').read_text(encoding='utf-8') == 'fresh prior environment read\n'
    finally:
        terminal._active_environments[task_key] = first
        ft._file_ops_cache[task_key] = ShellFileOperations(first)
        ft.clear_file_ops_cache(other)
        terminal._active_environments.pop(other_key, None)
        terminal.clear_task_env_overrides(other)
        second.cleanup()
        assert second.proc.poll() is not None
