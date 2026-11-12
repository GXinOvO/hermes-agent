import json

import pytest

from agent.runtime_scope import bind_state
from hermes_scope import Principal, Scope
from tests.tools import test_file_backend_identity as backend_fixtures
from tools import file_tools, terminal_tool
from tools.environments.sandbox_conformance import RecordingSandboxEnvironment, RecordingSandboxProvider
from tools.environments.sandbox_scope import (
    HostedSandboxRequired, ScopedEnvironment, require_hosted_interface, sandbox_identity,
)
from tools.file_operations import ShellFileOperations

remote_project = backend_fixtures.remote_project


class HostedFileBackend:
    uses_host_filesystem = False
    is_local = False

    def __init__(self, inner):
        self.inner = inner
        self.cwd = inner.cwd
        self.timeout = inner.timeout
        self.env = inner.env

    def file_generation(self, *, scope):
        return self.inner._session_id

    def execute_file_probe(self, command, *, scope, stdin_data=None):
        return self.inner.execute_file_probe(command, stdin_data=stdin_data)

    def execute(self, command, cwd='', *, scope, **kwargs):
        return self.inner.execute(command, cwd=cwd, **kwargs)


class LegacyHostedFileBackend(RecordingSandboxEnvironment):
    def __init__(self, inner, scope):
        super().__init__(RecordingSandboxProvider(), sandbox_identity(scope), inner.cwd)
        self.inner = inner
        self.timeout = inner.timeout
        self.env = inner.env

    def execute(self, command, cwd='', *, scope, **kwargs):
        return self.inner.execute(command, cwd=cwd, **kwargs)


def test_sensitive_hosted_write_refused_before_backend_start(monkeypatch):
    started = []

    def start_backend(task_id):
        started.append(task_id)
        raise AssertionError('sensitive write started a backend')

    monkeypatch.setattr(file_tools, '_get_file_ops', start_backend)
    scope = Scope('tenant', 'preflight', Principal('owner'), owner=Principal('owner'), can_administer=True)
    with bind_state(scope):
        result = json.loads(file_tools.write_file_tool('/etc/passwd', 'blocked', task_id='preflight'))
    assert 'sensitive system path' in result.get('error', ''), result
    assert not started


@pytest.mark.parametrize('method', ['file_generation', 'execute_file_probe'])
def test_hosted_file_authority_methods_must_accept_scope(method):
    class BlindBackend(RecordingSandboxEnvironment):
        def file_generation(self):
            return 'generation'

        def execute_file_probe(self, command, *, stdin_data=None):
            return {'returncode': 0, 'output': ''}

    class Backend(RecordingSandboxEnvironment):
        pass

    setattr(Backend, method, getattr(BlindBackend, method))
    scope = Scope('tenant', 'agent', Principal('owner'), owner=Principal('owner'), can_administer=True)
    inner = Backend(RecordingSandboxProvider(), sandbox_identity(scope), '/workspace')
    with pytest.raises(HostedSandboxRequired, match=method):
        require_hosted_interface(inner, 'hosted-test')


def test_hosted_generation_error_revokes_old_read(remote_project, monkeypatch):
    project, inner, task = remote_project
    target = project / 'generation-error.txt'
    target.write_text('original\n', encoding='utf-8')
    scope = Scope('tenant', 'generation-error', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    backend = HostedFileBackend(inner)
    env = ScopedEnvironment(backend, identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, env)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(env))
    generation = backend.file_generation
    with bind_state(scope):
        assert 'original' in json.loads(file_tools.read_file_tool('/workspace/generation-error.txt', task_id=task)).get('content', '')
        def failed_generation(*, scope):
            raise TimeoutError('generation unavailable')
        monkeypatch.setattr(backend, 'file_generation', failed_generation)
        failed = json.loads(file_tools.write_file_tool('/workspace/generation-error.txt', 'must refuse\n', task_id=task))
        assert failed.get('error') and not failed.get('verified'), failed
        monkeypatch.setattr(backend, 'file_generation', generation)
        stale = json.loads(file_tools.write_file_tool('/workspace/generation-error.txt', 'still refuse\n', task_id=task))
        assert stale.get('error') and not stale.get('verified'), stale
    assert target.read_text(encoding='utf-8') == 'original\n'


def test_hosted_capability_loss_does_not_downgrade_to_legacy(remote_project, monkeypatch):
    project, inner, task = remote_project
    target = project / 'capability-loss.txt'
    target.write_text('original\n', encoding='utf-8')
    scope = Scope('tenant', 'capability-loss', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    backend = HostedFileBackend(inner)
    env = ScopedEnvironment(backend, identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, env)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(env))
    with bind_state(scope):
        assert 'original' in json.loads(file_tools.read_file_tool('/workspace/capability-loss.txt', task_id=task)).get('content', '')
        monkeypatch.setattr(backend, 'execute_file_probe', None)
        for _ in range(2):
            result = json.loads(file_tools.write_file_tool('/workspace/capability-loss.txt', 'must refuse\n', task_id=task))
            assert result.get('error') and not result.get('verified'), result
    assert target.read_text(encoding='utf-8') == 'original\n'


def test_hosted_backend_switch_during_write_cannot_bypass_new_authority(remote_project, monkeypatch):
    project, inner, task = remote_project
    target = project / 'switch-during-write.txt'
    target.write_text('unread original\n', encoding='utf-8')
    scope = Scope('tenant', 'switch-during-write', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    legacy = ScopedEnvironment(LegacyHostedFileBackend(inner, scope), identity, owner=scope)
    verified = ScopedEnvironment(HostedFileBackend(inner), identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, legacy)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(legacy))
    original_check = file_tools._check_binary_document_write

    def replace_backend(path, task_id):
        monkeypatch.setitem(terminal_tool._active_environments, identity.key, verified)
        return original_check(path, task_id)

    monkeypatch.setattr(file_tools, '_check_binary_document_write', replace_backend)
    with bind_state(scope):
        result = json.loads(file_tools.write_file_tool('/workspace/switch-during-write.txt', 'unread overwrite\n', task_id=task))
    assert result.get('error') and not result.get('verified'), result
    assert target.read_text(encoding='utf-8') == 'unread original\n'


def test_hosted_read_uses_its_session_directory(remote_project, monkeypatch):
    project, inner, task = remote_project
    for name in ('A', 'B'):
        (project / name).mkdir()
        (project / name / 'doc.txt').write_text(f'{name} content\n', encoding='utf-8')
    monkeypatch.setattr(inner, 'cwd', '/workspace/B')
    scope = Scope('tenant', 'cwd', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    environment = ScopedEnvironment(HostedFileBackend(inner), identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, environment)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(environment))
    with bind_state(scope):
        terminal_tool.record_session_cwd(task, '/workspace/A')
        try:
            read = json.loads(file_tools.read_file_tool('doc.txt', task_id=task))
            write = json.loads(file_tools.write_file_tool('doc.txt', 'updated\n', task_id=task))
        finally:
            terminal_tool.clear_session_cwd(task)
    assert 'A content' in read.get('content', ''), read
    assert write.get('verified'), write
    assert (project / 'A' / 'doc.txt').read_text(encoding='utf-8') == 'updated\n'
    assert (project / 'B' / 'doc.txt').read_text(encoding='utf-8') == 'B content\n'


def test_hosted_full_read_and_backend_generation_gate_writes(remote_project, monkeypatch):
    project, inner, task = remote_project
    target = project / 'hosted.txt'
    target.write_text('original\n', encoding='utf-8')
    scope = Scope('tenant', 'agent', Principal('owner'), owner=Principal('owner'), can_administer=True)
    key = sandbox_identity(scope).key
    environment = ScopedEnvironment(HostedFileBackend(inner), sandbox_identity(scope), owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, key, environment)
    monkeypatch.setitem(file_tools._file_ops_cache, key, ShellFileOperations(environment))
    path = '/workspace/hosted.txt'

    with bind_state(scope):
        unread = json.loads(file_tools.write_file_tool(path, 'unread overwrite\n', task_id=task))
        assert unread.get('error') and not unread.get('verified'), unread
        assert target.read_text(encoding='utf-8') == 'original\n'
        read = json.loads(file_tools.read_file_tool(path, task_id=task))
        assert 'original' in read.get('content', ''), read
        written = json.loads(file_tools.write_file_tool(path, 'authorized\n', task_id=task))
        assert written.get('verified'), written
        assert target.read_text(encoding='utf-8') == 'authorized\n'
        inner._session_id += '-replacement'
        stale = json.loads(file_tools.write_file_tool(path, 'stale overwrite\n', task_id=task))
        assert stale.get('error') and not stale.get('verified'), stale
        assert target.read_text(encoding='utf-8') == 'authorized\n'
        read = json.loads(file_tools.read_file_tool(path, task_id=task))
        assert 'authorized' in read.get('content', ''), read
        written = json.loads(file_tools.write_file_tool(path, 'new generation\n', task_id=task))
        assert written.get('verified'), written
        assert target.read_text(encoding='utf-8') == 'new generation\n'


@pytest.mark.parametrize('unanchored', [False, True])
def test_legacy_hosted_read_write_and_patch_stay_in_sandbox(remote_project, monkeypatch, tmp_path, unanchored):
    from tools import file_tools_paths, file_tools_read_tracking

    project, inner, task = remote_project
    target = project / 'legacy.txt'
    target.write_text('sandbox content\n', encoding='utf-8')
    host = tmp_path / 'host-cwd'
    host.mkdir()
    (host / 'legacy.txt').write_text('host sentinel\n', encoding='utf-8')
    scope = Scope('tenant', 'legacy', Principal('owner'), owner=Principal('owner'), can_administer=True)
    key = sandbox_identity(scope).key
    backend = LegacyHostedFileBackend(inner, scope)
    require_hosted_interface(backend, 'legacy-hosted')
    environment = ScopedEnvironment(backend, sandbox_identity(scope), owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, key, environment)
    monkeypatch.setitem(file_tools._file_ops_cache, key, ShellFileOperations(environment))
    if unanchored:
        def unknown_cwd(*args, **kwargs):
            raise ValueError('sandbox working directory unknown')
        monkeypatch.chdir(host)
        monkeypatch.setattr(file_tools_paths, '_resolve_base_dir', unknown_cwd)
        monkeypatch.setattr(file_tools, '_resolve_base_dir', unknown_cwd)
    path = 'legacy.txt' if unanchored else '/workspace/legacy.txt'

    with bind_state(scope):
        read = json.loads(file_tools.read_file_tool(path, task_id=task))
        assert 'sandbox content' in read.get('content', ''), read
        assert 'host sentinel' not in json.dumps(read)
        with file_tools_read_tracking._read_tracker_lock:
            tracker = file_tools_read_tracking._read_tracker.get(task) or {}
            assert not tracker.get('full_write_baselines')
        write = json.loads(file_tools.write_file_tool(path, 'overwrite\n', task_id=task))
        assert write.get('verified'), write
        assert target.read_text(encoding='utf-8') == 'overwrite\n'
        patched = json.loads(file_tools.patch_tool(path=path, old_string='overwrite', new_string='patched', task_id=task))
        assert patched.get('success'), patched
        assert target.read_text(encoding='utf-8') == 'patched\n'
        v4a = json.loads(file_tools.patch_tool(mode='patch', patch=(
            f'*** Begin Patch\n*** Update File: {path}\n@@\n-patched\n+v4a\n*** End Patch\n'
        ), task_id=task))
        assert v4a.get('success'), v4a
        assert target.read_text(encoding='utf-8') == 'v4a\n'
        with file_tools_read_tracking._read_tracker_lock:
            tracker = file_tools_read_tracking._read_tracker.get(task) or {}
            assert not tracker.get('full_write_baselines')
    assert (host / 'legacy.txt').read_text(encoding='utf-8') == 'host sentinel\n'


def test_legacy_backend_switch_does_not_revive_previous_read(remote_project, monkeypatch):
    project, inner, task = remote_project
    target = project / 'switch.txt'
    target.write_text('original\n', encoding='utf-8')
    scope = Scope('tenant', 'switch', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    key = identity.key
    verified = ScopedEnvironment(HostedFileBackend(inner), identity, owner=scope)
    legacy = ScopedEnvironment(LegacyHostedFileBackend(inner, scope), identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, key, verified)
    monkeypatch.setitem(file_tools._file_ops_cache, key, ShellFileOperations(verified))
    path = '/workspace/switch.txt'

    with bind_state(scope):
        assert 'original' in json.loads(file_tools.read_file_tool(path, task_id=task)).get('content', '')
        monkeypatch.setitem(terminal_tool._active_environments, key, legacy)
        monkeypatch.setitem(file_tools._file_ops_cache, key, ShellFileOperations(legacy))
        assert 'original' in json.loads(file_tools.read_file_tool(path, task_id=task)).get('content', '')
        legacy_write = json.loads(file_tools.write_file_tool(path, 'legacy update\n', task_id=task))
        assert legacy_write.get('verified'), legacy_write
        monkeypatch.setitem(terminal_tool._active_environments, key, verified)
        monkeypatch.setitem(file_tools._file_ops_cache, key, ShellFileOperations(verified))
        stale = json.loads(file_tools.write_file_tool(path, 'stale overwrite\n', task_id=task))
        assert stale.get('error') and not stale.get('verified'), stale
        assert target.read_text(encoding='utf-8') == 'legacy update\n'
        assert 'legacy update' in json.loads(file_tools.read_file_tool(path, task_id=task)).get('content', '')
        written = json.loads(file_tools.write_file_tool(path, 'fresh overwrite\n', task_id=task))
        assert written.get('verified'), written
    assert target.read_text(encoding='utf-8') == 'fresh overwrite\n'


def test_hosted_task_id_does_not_revoke_same_named_local_task(remote_project, monkeypatch, tmp_path):
    project, inner, _ = remote_project
    (project / 'hosted-collision.txt').write_text('hosted\n', encoding='utf-8')
    local = tmp_path / 'local-collision.txt'
    control = tmp_path / 'local-control.txt'
    local.write_text('local\n', encoding='utf-8')
    control.write_text('control\n', encoding='utf-8')
    scope = Scope('tenant', 'collision', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    hosted_task = 'shared'
    local_task = f'{identity.key}:{hosted_task}'
    environment = ScopedEnvironment(HostedFileBackend(inner), identity, owner=scope)
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, environment)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(environment))

    assert 'local' in json.loads(file_tools.read_file_tool(str(local), task_id=local_task)).get('content', '')
    assert 'control' in json.loads(file_tools.read_file_tool(str(control), task_id='unrelated-local')).get('content', '')
    with bind_state(scope):
        hosted = json.loads(file_tools.read_file_tool('/workspace/hosted-collision.txt', task_id=hosted_task))
    assert 'hosted' in hosted.get('content', ''), hosted
    control_write = json.loads(file_tools.write_file_tool(str(control), 'control updated\n', task_id='unrelated-local'))
    assert control_write.get('verified'), control_write
    local_write = json.loads(file_tools.write_file_tool(str(local), 'local updated\n', task_id=local_task))
    assert local_write.get('verified'), local_write
    assert local.read_text(encoding='utf-8') == 'local updated\n'


def test_replaced_hosted_backend_cannot_reuse_cached_read(remote_project, monkeypatch, tmp_path):
    project, first, task = remote_project
    second_project = tmp_path / 'replacement-project'
    second_project.mkdir()
    first_target = project / 'replacement.txt'
    second_target = second_project / 'replacement.txt'
    first_target.write_text('first sandbox\n', encoding='utf-8')
    second_target.write_text('replacement sandbox\n', encoding='utf-8')
    module = backend_fixtures.sys.modules['file_state_test_provider']
    second = module.FixedProjectEnvironment(second_project, tmp_path / 'replacement-lease', 'private', timeout=4)
    scope = Scope('tenant', 'replacement', Principal('owner'), owner=Principal('owner'), can_administer=True)
    identity = sandbox_identity(scope)
    first_env = ScopedEnvironment(HostedFileBackend(first), identity, owner=scope)
    second_env = ScopedEnvironment(HostedFileBackend(second), identity, owner=scope)
    monkeypatch.setitem(terminal_tool._active_environments, identity.key, first_env)
    monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(first_env))
    path = '/workspace/replacement.txt'
    try:
        with bind_state(scope):
            assert 'first sandbox' in json.loads(file_tools.read_file_tool(path, task_id=task)).get('content', '')
            monkeypatch.setitem(terminal_tool._active_environments, identity.key, second_env)
            stale = json.loads(file_tools.write_file_tool(path, 'stale write\n', task_id=task))
            assert stale.get('error') and not stale.get('verified'), stale
            assert first_target.read_text(encoding='utf-8') == 'first sandbox\n'
            assert second_target.read_text(encoding='utf-8') == 'replacement sandbox\n'
            fresh = json.loads(file_tools.read_file_tool(path, task_id=task))
            assert 'replacement sandbox' in fresh.get('content', ''), fresh
            written = json.loads(file_tools.write_file_tool(path, 'fresh write\n', task_id=task))
            assert written.get('verified'), written
        assert second_target.read_text(encoding='utf-8') == 'fresh write\n'
    finally:
        second.cleanup()
        assert second.proc.poll() is not None


def test_hosted_read_authority_is_separate_for_each_sandbox(remote_project, monkeypatch, tmp_path):
    first_project, first, task = remote_project
    second_project = tmp_path / 'other-sandbox'
    second_project.mkdir()
    first_path = first_project / 'shared.txt'
    second_path = second_project / 'shared.txt'
    first_path.write_text('first content\n', encoding='utf-8')
    second_path.write_text('second content\n', encoding='utf-8')
    module = backend_fixtures.sys.modules['file_state_test_provider']
    second = module.FixedProjectEnvironment(second_project, tmp_path / 'other-lease', 'private', timeout=4)
    scopes = [Scope('tenant', agent, Principal('owner'), owner=Principal('owner'), can_administer=True)
              for agent in ('first-agent', 'second-agent')]
    for scope, inner in zip(scopes, (first, second)):
        identity = sandbox_identity(scope)
        env = ScopedEnvironment(HostedFileBackend(inner), identity, owner=scope)
        monkeypatch.setitem(terminal_tool._active_environments, identity.key, env)
        monkeypatch.setitem(file_tools._file_ops_cache, identity.key, ShellFileOperations(env))
    path = '/workspace/shared.txt'
    try:
        with bind_state(scopes[0]):
            first_read = json.loads(file_tools.read_file_tool(path, task_id=task))
            assert 'first content' in first_read.get('content', ''), first_read
        with bind_state(scopes[1]):
            denied = json.loads(file_tools.write_file_tool(path, 'not read\n', task_id=task))
            assert denied.get('error') and not denied.get('verified'), denied
            assert second_path.read_text(encoding='utf-8') == 'second content\n'
            second_read = json.loads(file_tools.read_file_tool(path, task_id=task))
            assert 'second content' in second_read.get('content', ''), second_read
            written = json.loads(file_tools.write_file_tool(path, 'second updated\n', task_id=task))
            assert written.get('verified'), written
        with bind_state(scopes[0]):
            written = json.loads(file_tools.write_file_tool(path, 'first updated\n', task_id=task))
            assert written.get('verified'), written
        assert first_path.read_text(encoding='utf-8') == 'first updated\n'
        assert second_path.read_text(encoding='utf-8') == 'second updated\n'
    finally:
        second.cleanup()
        assert second.proc.poll() is not None