"""Native regressions for writable runtime aliases and interrupted construction."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from agent.terminal_env_registry import get_provider
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home
from tests.tools import test_owned_process_native as native_fixtures
from tools.terminal_tool_backends import _create_environment

PLUGIN = Path(__file__).resolve().parents[2] / 'plugins/owned-process'
native = native_fixtures.native


def observe(request, name, value):
    request.node.user_properties.append((name, json.dumps(value)))


def fd_open(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF
        return False
    return True


@pytest.fixture
def configured(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from hermes_cli import plugins as plugins_mod
    home = get_hermes_home()
    destination = home / 'plugins/owned-process'
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN, destination)
    project, state = tmp_path / 'project', tmp_path / 'state'
    project.mkdir()
    state.mkdir(mode=0o700)
    bundled = tmp_path / 'empty-bundled'
    bundled.mkdir()
    monkeypatch.setattr(plugins_mod, 'get_bundled_plugins_dir', lambda: bundled)
    managers = []

    def load(project_root=project, state_root=state):
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'plugins': {'enabled': ['owned-process'], 'isolation': 'in_process',
                        'entries': {'owned-process': {'settings': {
                            'project_root': str(project_root), 'state_root': str(state_root)}}}},
            'terminal': {'backend': 'owned_process', 'cwd': '/workspace', 'timeout': 12}}))
        manager = PluginManager()
        manager.discover_and_load()
        managers.append(manager)
        assert manager._plugins['owned-process'].error is None
        return get_provider('owned_process')
    yield project, state, destination, load
    for manager in reversed(managers):
        manager.unload()


@pytest.mark.parametrize('root,alias', [
    ('project', 'equal'), ('project', 'parent'),
    ('state', 'equal'), ('state', 'parent'),
    ('project', 'hardlink'), ('state', 'hardlink'),
])
def test_native_provider_refuses_writable_runtime_alias(configured, tmp_path, root, alias, request):
    project, state, runtime, load = configured
    before = (runtime / 'scope_init.py').read_bytes()
    if alias == 'hardlink':
        writable = project if root == 'project' else state
        os.link(runtime / 'scope_init.py', writable / 'helper-alias')
        assert os.stat(writable / 'helper-alias').st_ino == os.stat(runtime / 'scope_init.py').st_ino
    else:
        writable = runtime if alias == 'equal' else runtime.parent
        if root == 'project':
            project = writable
        else:
            state = writable
    provider = load(project, state)
    # Config/discovery are real; require public admission to reject before Popen/payload.
    assert provider.skip_container_guards is False
    available = provider.is_available()
    refused = None
    env = None
    try:
        try:
            env = provider.create_environment(cwd='/workspace', timeout=10)
        except (ValueError, RuntimeError) as exc:
            refused = str(exc)
        observation = {'root': root, 'alias': alias, 'available': available,
                       'refused': refused, 'payload_environment_created': env is not None,
                       'helper_unchanged': (runtime / 'scope_init.py').read_bytes() == before}
        observe(request, 'runtime_admission', observation)
        assert not available and env is None and refused, observation
        assert 'runtime' in refused or 'trusted' in refused, observation
        assert observation['helper_unchanged']
    finally:
        if env is not None:
            env.cleanup()


@pytest.mark.parametrize('root', ['project', 'state'])
@pytest.mark.parametrize('runtime_alias', [True, False])
def test_native_provider_rejects_bind_alias_in_private_mount_namespace(configured, tmp_path, root, runtime_alias, request):
    project, state, runtime, load = configured
    load()
    writable = project if root == 'project' else state
    alias = writable / 'runtime-bind'
    alias.mkdir()
    data = tmp_path / 'ordinary-data'
    data.mkdir()
    (data / 'value').write_text('bind-data')
    mounted = runtime if runtime_alias else data
    source = str(PLUGIN.parents[1])
    script = """
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins import PluginManager
from agent.terminal_env_registry import get_provider
manager = PluginManager()
manager.discover_and_load()
provider = get_provider('owned_process')
available = provider.is_available()
refused = None
try:
    env = provider.create_environment(cwd='/workspace', timeout=10)
except (RuntimeError, ValueError) as exc:
    refused = str(exc)
else:
    assert env.execute('printf BIND_CONTROL')['output'] == 'BIND_CONTROL'
    env.cleanup()
print(json.dumps({'available': available, 'refused': refused,
                  'root_identity': list(os.stat(sys.argv[2])[1:3]),
                  'alias_identity': list(os.stat(sys.argv[3])[1:3])}), flush=True)
manager.unload()
if sys.argv[4] == 'True':
    assert not available and refused and ('runtime' in refused or 'trusted' in refused)
else:
    assert available and refused is None
"""
    # All mounts are in this child namespace; host directories/inodes are untouched.
    argv = ['/usr/bin/bwrap', '--unshare-user', '--unshare-pid', '--unshare-net',
            '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
            '--bind', str(get_hermes_home()), str(get_hermes_home()),
            '--bind', str(project), str(project), '--bind', str(state), str(state),
            '--bind', str(mounted), str(alias), '--proc', '/proc', '--dev', '/dev',
            sys.executable, '-I', '-B', '-c', script, source, str(mounted), str(alias), str(runtime_alias)]
    proc = subprocess.run(argv, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                          capture_output=True, text=True, timeout=30, check=False)
    observe(request, 'bind_alias', {'root': root, 'argv': argv, 'exit': proc.returncode,
                                    'stdout': proc.stdout, 'stderr': proc.stderr})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    result = json.loads(proc.stdout)
    assert result['root_identity'] == result['alias_identity']
    assert result['available'] is not runtime_alias
    assert bool(result['refused']) is runtime_alias


def test_registered_terminal_disjoint_project_rw_and_private_namespace(native, request):
    project, _, _ = native
    (project / 'data-original').write_text('original')
    os.link(project / 'data-original', project / 'data-alias')
    result = native_fixtures.terminal('printf updated > data-alias; export TOKEN=kept; mkdir -p sub; cd sub; printf writable > data')
    assert (project / 'data-original').read_text() == 'updated'
    assert result['exit_code'] == 0, result
    again = native_fixtures.terminal('printf "$TOKEN"; pwd')
    assert again['exit_code'] == 0 and 'kept/workspace/sub' in again['output'], again
    assert (project / 'sub/data').read_text() == 'writable'
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    try:
        assert env.receipt['namespaces']['net'] != os.readlink('/proc/self/ns/net')
        assert get_provider('owned_process').skip_container_guards is False
        observe(request, 'normal_control', {'result': again, 'worker': env.receipt})
    finally:
        env.cleanup()


@pytest.mark.parametrize('fault', ['popen', 'thread-create', 'thread-start'])
def test_constructor_failure_preserves_error_and_closes_streams(native, tmp_path, monkeypatch, request, fault):
    provider = get_provider('owned_process')
    project, _, _ = native
    cls = sys.modules[type(provider).__module__].OwnedProjectEnvironment
    module = sys.modules[cls.__module__]
    env = cls.__new__(cls)
    error = RuntimeError('injected constructor ' + fault)
    real_thread, real_start = threading.Thread, threading.Thread.start
    def fail_popen(*args, **kwargs):
        raise error
    def make_thread(*args, **kwargs):
        if kwargs.get('name') == 'owned-demultiplexer':
            raise error
        return real_thread(*args, **kwargs)
    def start(thread, *args, **kwargs):
        if thread.name == 'owned-demultiplexer':
            raise error
        return real_start(thread, *args, **kwargs)
    try:
        with monkeypatch.context() as patches:
            if fault == 'popen':
                patches.setattr(module.subprocess, 'Popen', fail_popen)
            elif fault == 'thread-create':
                patches.setattr(module.threading, 'Thread', make_thread)
            else:
                patches.setattr(threading.Thread, 'start', start)
            with pytest.raises(RuntimeError) as info:
                env.__init__(project, tmp_path / ('failed-' + fault))
        # Keep info/error and the entire constructor traceback alive. No gc/rescue yet.
        proc = getattr(env, 'proc', None)
        streams = [env.stderr_file] + ([proc.stdin, proc.stdout] if proc is not None else [])
        reader = getattr(env, 'reader', None)
        observation = {'fault': fault, 'original_exception': info.value is error,
                       'raised': str(info.value), 'streams_closed': [s.closed for s in streams],
                       'launcher_exit': proc.poll() if proc is not None else None,
                       'reader_alive': reader.is_alive() if reader is not None else False,
                       'pump_alive': sum(h._pump.is_alive() for h in env.all_handles)}
        observe(request, 'construction_before_rescue', observation)
        assert observation['original_exception'] and all(observation['streams_closed']), observation
        assert proc is None or observation['launcher_exit'] is not None, observation
        assert not observation['reader_alive'] and not observation['pump_alive'], observation
        env.cleanup()
        env.cleanup()
        retry = provider.create_environment(cwd='/workspace', timeout=10)
        try:
            assert retry.execute('printf RETRY_OK')['output'] == 'RETRY_OK'
        finally:
            retry.cleanup()
    finally:
        proc = getattr(env, 'proc', None)
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
        if hasattr(env, 'stderr_file'):
            env.stderr_file.close()


@pytest.mark.parametrize('fault', ['fdopen', 'thread-create', 'thread-start'])
def test_pump_partial_acquisition_closes_both_fds_before_gc(native, monkeypatch, request, fault):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    module = sys.modules[type(env).__module__]
    real_pipe, real_fdopen = os.pipe, os.fdopen
    real_thread, real_start = threading.Thread, threading.Thread.start
    acquired, errors, streams = [], [], []
    initial = (len(env.handles), len(env.all_handles))
    held = []
    def pipe():
        pair = real_pipe()
        acquired.append(pair)
        return pair
    def fdopen(fd, *args, **kwargs):
        if fault == 'fdopen' and acquired and fd == acquired[-1][0]:
            raise errors[-1]
        stream = real_fdopen(fd, *args, **kwargs)
        streams.append(stream)
        return stream
    def make_thread(*args, **kwargs):
        if getattr(kwargs.get('target'), '__name__', '') == '_pump_output':
            raise errors[-1]
        return real_thread(*args, **kwargs)
    def start(thread, *args, **kwargs):
        if getattr(getattr(thread, '_target', None), '__name__', '') == '_pump_output':
            raise errors[-1]
        return real_start(thread, *args, **kwargs)
    try:
        with monkeypatch.context() as patches:
            patches.setattr(module.os, 'pipe', pipe)
            patches.setattr(module.os, 'fdopen', fdopen)
            if fault == 'thread-create':
                patches.setattr(module.threading, 'Thread', make_thread)
            else:
                patches.setattr(threading.Thread, 'start', start)
            fd_count = len(list(Path('/proc/self/fd').iterdir()))
            for attempt in range(6):
                errors.append(RuntimeError('injected pump ' + fault + ' ' + str(attempt)))
                with pytest.raises(RuntimeError) as info:
                    env.start_owned_process('printf NEVER_SENT')
                held.append(info)  # retain self through the original exception traceback
                pair = acquired[-1]
                observation = {'fault': fault, 'attempt': attempt,
                               'original_exception': info.value is errors[-1],
                               'fd_open': [fd_open(fd) for fd in pair],
                               'handle_counts': [len(env.handles), len(env.all_handles)],
                               'fd_count': len(list(Path('/proc/self/fd').iterdir())),
                               'initial_fd_count': fd_count}
                observe(request, 'pump_before_rescue', observation)
                assert observation['original_exception'] and not any(observation['fd_open']), observation
                assert tuple(observation['handle_counts']) == initial, observation
                assert observation['fd_count'] == fd_count, observation
        retry = env.start_owned_process('printf PUMP_RETRY')
        assert env.wait_owned(retry, 10)['root_exit'] == 0
        assert retry.stdout.read() == 'PUMP_RETRY'
        retry.stdout.close()
        env.cleanup()
        env.cleanup()
    finally:
        env.cleanup()
        for stream in streams:
            stream.close()
        for pair in acquired:
            for fd in pair:
                if fd_open(fd):
                    os.close(fd)


def test_cleanup_failure_releases_other_streams_and_retry_finishes(native, request):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    real_stdin = env.proc.stdin
    class CloseOnce:
        def __init__(self):
            self.failed = False
        def __getattr__(self, name):
            return getattr(real_stdin, name)
        def close(self):
            if not self.failed:
                self.failed = True
                raise OSError('injected close failure')
            real_stdin.close()
    env.proc.stdin = CloseOnce()
    try:
        with pytest.raises((RuntimeError, OSError)) as info:
            env.cleanup()
        observation = {'raised': str(info.value), 'stdin_closed': real_stdin.closed,
                       'stdout_closed': env.proc.stdout.closed, 'stderr_closed': env.stderr_file.closed,
                       'launcher_exit': env.proc.poll(), 'reader_alive': env.reader.is_alive()}
        observe(request, 'cleanup_before_retry', observation)
        assert observation['stdout_closed'] and observation['stderr_closed'], observation
        assert observation['launcher_exit'] is not None and not observation['reader_alive'], observation
        env.cleanup()
        env.cleanup()
        assert real_stdin.closed
    finally:
        real_stdin.close()
        env.proc.stdout.close()
        env.stderr_file.close()
        env.cleanup()


def test_handle_limit_remains_32_with_working_background_pumps(native, request):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    handles = []
    try:
        for _ in range(32):
            handles.append(env.start_owned_process('printf READY; sleep 60'))
        with pytest.raises(RuntimeError, match='handle limit 32'):
            env.start_owned_process('printf OVER_LIMIT')
        assert len(env.handles) == 32 and all(h._pump.is_alive() for h in handles)
        first = handles[0]
        assert first.stdout.read(5) == 'READY'
        assert env.signal_owned(first)['descendant_empty']
        first.stdout.read()
        first._pump.join(5)
        first.stdout.close()
        replacement = env.start_owned_process('printf REPLACEMENT')
        handles.append(replacement)
        assert env.wait_owned(replacement, 10)['root_exit'] == 0
        assert replacement.stdout.read() == 'REPLACEMENT'
        observe(request, 'handle_limit', {'bound': 32, 'overflow_refused': True, 'retry_output': 'REPLACEMENT'})
    finally:
        env.cleanup()
        for handle in handles:
            handle.stdout.close()
