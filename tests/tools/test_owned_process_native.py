"""Native owned backend discovery, real terminal/processes, and durable recovery."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_constants import get_hermes_home
from hermes_cli.plugins import PluginManager
from tools import process_registry as pr
from tools.registry import registry as tool_registry
from tools.terminal_tool_backends import _create_environment

PLUGIN = Path(__file__).resolve().parents[2] / 'plugins' / 'owned-process'

def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('owned test deadline expired')

@pytest.fixture
def native(tmp_path, monkeypatch, request):
    import hermes_yaml as yaml
    from hermes_cli import plugins as plugins_mod
    from tools import terminal_tool as terminal
    project = tmp_path / 'project'
    project.mkdir()
    home = get_hermes_home()
    destination = home / 'plugins' / 'owned-process'
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Missing shipped plugin is the intentional native-entry RED at this base.
    assert PLUGIN.is_dir(), 'owned-process must ship as a real discoverable plugin'
    shutil.copytree(PLUGIN, destination)
    (tmp_path / 'state').mkdir(mode=0o700)
    settings = {'project_root': str(project), 'state_root': str(tmp_path / 'state')}
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['owned-process'], 'isolation': 'in_process',
                    'entries': {'owned-process': {'settings': settings}}},
        'terminal': {'backend': 'owned_process', 'cwd': '/workspace', 'timeout': 12}}))
    bundled = tmp_path / 'empty-bundled'
    bundled.mkdir()
    monkeypatch.setattr(plugins_mod, 'get_bundled_plugins_dir', lambda: bundled)
    monkeypatch.setattr(terminal, '_active_environments', {})
    monkeypatch.setattr(terminal, '_last_activity', {})
    monkeypatch.setattr(terminal, '_task_env_overrides', {})
    monkeypatch.setattr(terminal, '_session_cwd', {})
    monkeypatch.setenv('TERMINAL_ENV', 'owned_process')
    monkeypatch.setenv('TERMINAL_CWD', '/workspace')
    monkeypatch.setenv('TERMINAL_TIMEOUT', '12')
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins['owned-process'].error is None
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    try:
        yield project, registry, manager
    finally:
        active = list(terminal._active_environments.values())
        for session in list(registry._running.values()):
            registry.kill_process(session.id, consume_output=False)
        terminal.cleanup_all_environments()
        request.node.user_properties.append(('owned_receipts', json.dumps([
            {'session_id': s.id, 'pid_scope': s.pid_scope, 'exit_code': s.exit_code,
             'completion': s._completion_event.is_set(), 'lifecycle': s.owned_lifecycle,
             'total_output_chars': s.total_output_chars, 'result_durability': s.result_durability}
            for s in list(registry._finished.values()) if s.owned_handle is not None])))
        request.node.user_properties.append(('launcher_cleanup', json.dumps([
            {'pid': e.proc.pid, 'returncode': e.proc.returncode, 'reader_alive': e.reader.is_alive(),
             'pump_alive': sum(h._pump.is_alive() for h in e.all_handles),
             'worker_receipt': e.receipt, 'cleanup_receipts': e.cleanup_receipts} for e in active])))
        manager.unload()


def terminal(command, **kwargs):
    return json.loads(tool_registry.get_entry('terminal').handler(
        {'command': command, **kwargs}, task_id='owned-native', session_id='owned-native'))


def process(action, session_id, **kwargs):
    return json.loads(tool_registry.get_entry('process_manage').handler(
        {'action': action, 'session_id': session_id, **kwargs}))


def test_native_discovery_factory_and_real_terminal(native):
    from agent.terminal_env_registry import get_provider, provider_flag
    project, registry, _ = native
    provider = get_provider('owned_process')
    assert provider is not None and provider.is_available()
    assert provider.probe()[0] == 'ready'
    assert provider_flag('owned_process', 'is_container') is True
    assert provider_flag('owned_process', 'skip_container_guards') is False
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    try:
        assert env._hermes_backend_name == 'owned_process'
        direct = env.execute('printf FACTORY; pwd', timeout=5)
        assert direct['returncode'] == 0 and 'FACTORY/workspace' in direct['output']
    finally:
        env.cleanup()
    with scoped_current_session_id('native-owner'):
        result = terminal("printf TERMINAL; printf file-value > /workspace/file")
    assert result['exit_code'] == 0 and 'TERMINAL' in result['output'], result
    assert (project / 'file').read_text() == 'file-value'
    assert not registry._running


def test_background_process_manage_and_large_utf8_output(native):
    _project, registry, _ = native
    with scoped_current_session_id('native-owner'):
        started = terminal("python3 -B -c \"import sys; print('中🙂'*90000); print('FINAL_UTF8',flush=True)\"", background=True)
        assert started.get('session_id'), started
        sid = started['session_id']
        assert process('poll', sid)['status'] in ('running', 'exited')
        final = process('wait', sid, timeout=10)
        log = process('log', sid, limit=200)
    session = registry.get(sid)
    assert session.owned_handle is not None and session.pid is None
    assert session._completion_event.is_set()
    assert final['exit_code'] == 0 and 'FINAL_UTF8' in final['output'], final
    assert 'FINAL_UTF8' in log['output'] and '\ufffd' not in session.output_buffer
    assert final['result_durability'] == 'persisted'
    assert session.owned_lifecycle['descendant_empty'] is True
    assert session.owned_lifecycle['waitpid_echild'] is True
    assert session.total_output_chars > 180000


DESCENDANTS = '''import os,pathlib,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pid=os.fork()
if pid == 0:
 os.setsid()
 pid=os.fork()
 if pid: os._exit(0)
 pathlib.Path('/workspace/descendant-ready').write_text(str(os.getpid()))
 while True:
  with open('/workspace/heartbeat','a') as stream: stream.write('tick\\n')
  time.sleep(.02)
while True: time.sleep(.1)
'''


def test_kill_waits_for_real_setsid_double_fork_scope(native):
    project, registry, _ = native
    with scoped_current_session_id('native-owner'):
        started = terminal('python3 -B -c ' + shlex.quote(DESCENDANTS), background=True)
        sid = started['session_id']
        wait_for(lambda: (project / 'descendant-ready').exists())
        wait_for(lambda: (project / 'heartbeat').exists())
        result = process('kill', sid)
    assert result['status'] == 'killed', result
    receipt = result['owned_lifecycle']
    assert receipt['root_exit'] == -9 and receipt['descendant_empty'] is True
    assert receipt['waitpid_echild'] is True
    assert receipt['launcher_reaped'] is True and receipt['launcher_identity']['start_ticks'] > 0
    assert receipt['owner_generation'] == registry.get(sid).owned_handle.identifier
    assert receipt['reaped_count'] >= 2
    size = (project / 'heartbeat').stat().st_size
    time.sleep(.2)  # health: allow HX013 -- fixed observation interval checks that a reaped descendant emits no further heartbeat
    assert (project / 'heartbeat').stat().st_size == size
    assert registry.get(sid)._completion_event.wait(5)


def test_foreground_timeout_reaps_descendants(native):
    project, _, _ = native
    with scoped_current_session_id('native-owner'):
        result = terminal('python3 -B -c ' + shlex.quote(DESCENDANTS), timeout=1)
    assert result['exit_code'] == 124, result
    assert (project / 'descendant-ready').exists()
    size = (project / 'heartbeat').stat().st_size
    time.sleep(.2)  # health: allow HX013 -- fixed observation interval checks that a reaped descendant emits no further heartbeat
    assert (project / 'heartbeat').stat().st_size == size


def test_kill_output_pending_preserves_reader_and_durable_completion(native, monkeypatch):
    _project, registry, _ = native
    entered, release = threading.Event(), threading.Event()
    original = registry._ingest_output
    def delayed(session, text, **kwargs):
        if 'READY_PENDING' in text and not entered.is_set():
            entered.set()
            assert release.wait(15), 'test release not received'
        return original(session, text, **kwargs)
    monkeypatch.setattr(registry, '_ingest_output', delayed)
    try:
        with scoped_current_session_id('native-owner'):
            started = terminal("printf READY_PENDING; sleep 60", background=True)
            sid = started['session_id']
            session = registry.get(sid)
            session.notify_on_complete = True
            assert entered.wait(5)
            killed = process('kill', sid)
            assert killed['output_pending'] is True and killed['process_running'] is False
            assert not session.exited and not session._completion_event.is_set()
            assert not registry.is_completion_consumed(sid)
            assert registry.completion_queue.empty()
            for value in (process('poll', sid), process('log', sid), process('wait', sid, timeout=1)):
                assert value['output_pending'] is True and value['process_running'] is False, value
            listed = json.loads(tool_registry.get_entry('process_manage').handler({'action':'list'}))
            assert any(s.get('output_pending') and s['process_running'] is False for s in listed['processes'])
            assert not registry.is_completion_consumed(sid)
            release.set()
            assert session._completion_event.wait(5)
            notices = [v for v in registry.completion_queue.queue if v['session_id'] == sid]
            assert len(notices) == 1 and 'READY_PENDING' in notices[0]['output']
            final = process('wait', sid, timeout=1)
            assert 'READY_PENDING' in final['output'] and final['result_durability'] == 'persisted'
            assert registry.is_completion_consumed(sid)
            again = process('kill', sid)
            assert again['status'] == 'already_exited' and again['output_pending'] is False
    finally:
        release.set()


def test_new_reader_loads_receipt_without_reexecution(native, monkeypatch, tmp_path, request):
    project, registry, _ = native
    with scoped_current_session_id('native-owner'):
        started = terminal("sleep .15; printf one >> /workspace/executions; printf RECOVER_ME", background=True)
        sid = started['session_id']
        session = registry.get(sid)
        session.notify_on_complete = True
        assert session._completion_event.wait(10)
    path = get_hermes_home() / 'logs' / 'process-results' / (sid + '.json')
    original = json.loads(path.read_text())
    assert original['completion_consumed'] is False
    script = '''import json,sys
from gateway.session_context import scoped_current_session_id
from tools import process_registry as pr
from tools.registry import registry as tools
registry=pr.ProcessRegistry()
pr.process_registry=registry
with scoped_current_session_id('native-owner'):
 poll=json.loads(tools.get_entry('process_manage').handler({'action':'poll','session_id':sys.argv[1]}))
 log=json.loads(tools.get_entry('process_manage').handler({'action':'log','session_id':sys.argv[1]}))
 final=json.loads(tools.get_entry('process_manage').handler({'action':'wait','session_id':sys.argv[1],'timeout':1}))
assert not registry._running and registry.completion_queue.empty() and not registry.pending_watchers
with scoped_current_session_id('wrong-owner'): assert registry.poll(sys.argv[1])['status']=='not_found'
print(json.dumps({'poll':poll,'log':log,'final':final,'running':len(registry._running)}))
'''
    child = subprocess.run([sys.executable, '-B', '-c', script, sid], capture_output=True, text=True, check=True, env=os.environ.copy(), timeout=15)
    recovered = json.loads(child.stdout)
    assert recovered['poll']['result_verification'] == 'verified'
    assert recovered['poll']['result_fingerprint'] == original['result_fingerprint']
    assert recovered['final']['exit_code'] == 0 and 'RECOVER_ME' in recovered['final']['output']
    assert recovered['final']['owned_generation'] == original['owned_generation'] == session.owned_handle.identifier
    assert recovered['final']['owned_lifecycle'] == original['owned_lifecycle'] == session.owned_lifecycle
    assert (project / 'executions').read_text() == 'one'
    consumed = json.loads(path.read_text())
    assert consumed['completion_consumed'] is True
    assert consumed['source_fingerprint'] == original['source_fingerprint']
    assert consumed['result_fingerprint'] == original['result_fingerprint']
    # Both a cross-profile copy and altered owned evidence remain unreadable.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    other = tmp_path / 'other-profile'
    copied = other / 'logs/process-results' / path.name
    copied.parent.mkdir(parents=True)
    copied.write_text(path.read_text())
    token = set_hermes_home_override(other)
    try:
        with scoped_current_session_id('native-owner'):
            assert pr.ProcessRegistry().poll(sid)['status'] == 'not_found'
    finally:
        reset_hermes_home_override(token)
    corrupted = dict(consumed)
    corrupted['owned_lifecycle'] = {**consumed['owned_lifecycle'], 'root_exit': 31}
    path.write_text(json.dumps(corrupted))
    with scoped_current_session_id('native-owner'):
        assert pr.ProcessRegistry().poll(sid)['status'] == 'not_found'
    path.write_text(json.dumps(consumed))
    request.node.user_properties.append(('fresh_reader', json.dumps({'response':recovered,'receipt_before':original,'receipt_after':consumed,'executions':'one','cross_profile_rejected':True,'owned_tamper_rejected':True})))


def test_stale_generation_and_cancel_races_are_fail_closed(native):
    from types import SimpleNamespace
    _, _, _ = native
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            env.signal_owned(SimpleNamespace(identifier='stale-generation'))
        for _ in range(3):
            handle = env.start_owned_process('sleep .03; printf race', lifetime='background')
            receipt = env.signal_owned(handle)
            assert receipt['descendant_empty'] is True
            assert receipt['root_exit'] is not None
            assert env.signal_owned(handle) == receipt
            handle.stdout.close()
    finally:
        env.cleanup()
