"""Further native qualifications reuse the real discovery fixture."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shlex
import threading
import time

import pytest

from gateway.session_context import scoped_current_session_id
from tools import process_registry as pr
from tools.terminal_tool_backends import _create_environment
from tests.tools import test_owned_process_native as native_fixtures

native = native_fixtures.native
terminal = native_fixtures.terminal
process = native_fixtures.process
wait_for = native_fixtures.wait_for


def test_unread_stdout_stderr_does_not_park_independent_invocations(native, request):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    handle = env.start_owned_process("python3 -B -c \"import os; os.write(1,b'a'*200000); os.write(2,b'b'*200000); print('TAIL_UNREAD',flush=True)\"")
    try:
        capacity = fcntl.fcntl(handle.stdout.fileno(), fcntl.F_GETPIPE_SZ)
        receipt = env.wait_owned(handle, 10)  # Deliberately no consumer yet.
        assert handle._queued > capacity and 400000 > capacity
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(lambda n: env.execute('printf parallel-' + str(n), timeout=5), range(3)))
        assert all(v['returncode'] == 0 and 'parallel-' in v['output'] for v in outputs)
        out = handle.stdout.read()
        assert out.count('a') == 200000 and out.count('b') == 200000
        assert out.endswith('TAIL_UNREAD\n')
        request.node.user_properties.append(('unread_receipt', json.dumps({'capacity':capacity, 'output_bytes':len(out), 'peak_spool':handle._peak, 'lifecycle':receipt})))
    finally:
        handle.stdout.close()
        env.cleanup()


def test_unread_spool_overflow_is_explicit_and_other_calls_continue(native, request):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    handle = env.start_owned_process("python3 -B -c \"import os; os.write(1,b'x'*12000000)\"")
    try:
        env.wait_owned(handle, 10)
        result = env.execute('printf STILL_USABLE', timeout=5)
        output = handle.stdout.read()
        assert handle.returncode == 122 and handle._overflow is True
        assert handle._peak <= handle.SPOOL_LIMIT
        assert 'output_backpressure_limit' in output and 'call cancelled' in output
        assert result['returncode'] == 0 and 'STILL_USABLE' in result['output']
        request.node.user_properties.append(('overflow', json.dumps({'peak':handle._peak, 'limit':handle.SPOOL_LIMIT, 'diagnostic':handle.diagnostic,'lifecycle':handle.lifecycle})))
    finally:
        handle.stdout.close()
        env.cleanup()


def test_root_exit_before_detached_child_background_and_command_cleanup(native, request):
    project, _, _ = native
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    script = '''import os,pathlib,time
pid=os.fork()
if pid: os._exit(0)
os.setsid()
pathlib.Path('/workspace/root-gone-ready').write_text(str(os.getpid()))
time.sleep(.4)
pathlib.Path('/workspace/root-gone-finished').write_text('done')
'''
    try:
        handle = env.start_owned_process('python3 -B -c ' + shlex.quote(script))
        wait_for(lambda: (project/'root-gone-ready').exists())
        assert not handle.done.is_set(), 'background lifetime must retain child after root exit'
        env.execute('export OWNED_TOKEN=value; mkdir -p /workspace/sub; cd /workspace/sub')
        exported = env.execute('printf "$OWNED_TOKEN"; pwd')
        assert 'value/workspace/sub' in exported['output']
        receipt = env.wait_owned(handle, 10)
        assert receipt['root_exit'] == 0 and receipt['reaped_count'] >= 2
        assert (project/'root-gone-finished').read_text() == 'done'
        handle.stdout.close()
        cleanup = env.execute('sleep 60 & printf COMMAND_ROOT_GONE')
        assert cleanup['owned_lifecycle']['root_exit'] == 0
        assert cleanup['owned_lifecycle']['reaped_count'] >= 2
        request.node.user_properties.append(('root_exit_receipts', json.dumps({'background':receipt, 'foreground':cleanup['owned_lifecycle']})))
    finally:
        env.cleanup()


def test_cleanup_has_exact_owned_receipts_and_leaves_other_environment_live(native, request):
    first = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    second = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    handle = first.start_owned_process('sleep 60')
    other = second.start_owned_process('sleep 60')
    try:
        first.cleanup()
        assert first.proc.poll() == 0 and not first.reader.is_alive()
        assert len(first.cleanup_receipts) == 1
        receipt = first.cleanup_receipts[0]
        assert receipt['owner_generation'] == handle.identifier and receipt['launcher_reaped'] is True
        assert receipt['waitpid_echild'] is True
        assert other.poll() is None and second.proc.poll() is None
        assert second.execute('printf OTHER_ALIVE')['returncode'] == 0
        request.node.user_properties.append(('cleanup_scope', json.dumps(receipt)))
    finally:
        first.cleanup()
        second.cleanup()
        handle.stdout.close()
        other.stdout.close()


def test_payload_cannot_reach_ancestor_control_fds_and_positive_control(native, request):
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=10)
    command = '''import json,os,pathlib
paths={}
for p in pathlib.Path('/proc/self/fd').iterdir():
 try: paths[p.name]=os.readlink(p)
 except FileNotFoundError: pass
ancestor={}
for p in pathlib.Path('/proc/1/fd').iterdir():
 try: ancestor[p.name]=os.readlink(p)
 except PermissionError: ancestor[p.name]='denied'
print(json.dumps({'fds':paths,'ancestor':ancestor,'pid_ns':os.readlink('/proc/self/ns/pid'),'host_path_visible':pathlib.Path(%s).exists()}))
'''
    try:
        result = env.execute('python3 -B -c ' + shlex.quote(command % repr(str(Path.home()))))
        data = json.loads(result['output'].strip())
        assert set(data['fds']) <= {'0','1','2'}
        assert data['host_path_visible'] is False
        assert data['pid_ns'] != env.receipt['namespaces']['pid']
        assert all(value == 'denied' for value in data['ancestor'].values())
        # Sensitivity control: an explicitly inherited pipe is visible. The actual payload above receives none.
        positive = env.execute("python3 -B -c \"import os,subprocess; r,w=os.pipe(); subprocess.run(['/usr/bin/python3','-B','-c','import os,sys; print(os.readlink(\\\"/proc/self/fd/\\\"+sys.argv[1]))',str(r)],pass_fds=(r,)); os.close(r); os.close(w)\"")
        assert 'pipe:[' in positive['output']
        request.node.user_properties.append(('control_boundary', json.dumps(data)))
    finally:
        env.cleanup()


@pytest.mark.parametrize('kind',['interrupt','yield'])
def test_output_pending_interrupt_yield_envelopes(native, monkeypatch, kind):
    from tools.interrupt import request_yield, set_interrupt
    _, registry, _ = native
    entered, release = threading.Event(), threading.Event()
    original = registry._ingest_output
    def slow(session, text, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(session, text, **kwargs)
    monkeypatch.setattr(registry, '_ingest_output', slow)
    try:
        with scoped_current_session_id('native-owner'):
            started = terminal('printf PENDING; sleep 60', background=True)
            sid = started['session_id']
            assert entered.wait(5)
            killed = process('kill', sid)
            assert killed['output_pending']
            if kind == 'interrupt':
                set_interrupt(True)
            else:
                request_yield(threading.current_thread().ident)
            result = process('wait', sid, timeout=1)
            assert result['status'] == 'interrupted' and result['process_running'] is False and result['output_pending']
            assert not registry.is_completion_consumed(sid)
    finally:
        set_interrupt(False)
        release.set()
        if 'sid' in locals():
            assert registry.get(sid)._completion_event.wait(5)


def test_unverified_owned_completion_does_not_publish_fake_exit(native, monkeypatch):
    from types import SimpleNamespace
    _, registry, _ = native
    with scoped_current_session_id('native-owner'):
        started = terminal('printf FAULT; sleep .2', background=True)
        session = registry.get(started['session_id'])
        env = session.env_ref
        original = env.wait_owned
        def reject(*args, **kwargs):
            raise RuntimeError('injected missing trusted reaper receipt')
        monkeypatch.setattr(env, 'wait_owned', reject)
        assert session.owned_handle.done.wait(5)
        session._reader_thread.join(5)
        assert not session.exited and not session._completion_event.is_set()
        assert registry.completion_queue.empty()
        failed = process('kill', session.id)
        assert failed['status'] == 'error' and failed['process_running'] is True
        monkeypatch.setattr(env, 'wait_owned', original)
        assert original(session.owned_handle)['waitpid_echild']
        # Explicit test teardown after inspecting the failed-closed live entry.
        registry._running.pop(session.id)


def test_natural_exit_reports_pending_without_signalling(native, monkeypatch, request):
    _, registry, _ = native
    entered, release = threading.Event(), threading.Event()
    original = registry._ingest_output

    def slow(session, text, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(session, text, **kwargs)

    monkeypatch.setattr(registry, '_ingest_output', slow)
    try:
        with scoped_current_session_id('native-owner'):
            started = terminal('printf NATURAL_PENDING; exit 17', background=True)
            sid = started['session_id']
            session = registry.get(sid)
            assert entered.wait(5) and session.owned_handle.done.wait(5)

            def forbidden(*args, **kwargs):
                pytest.fail('a natural output-pending query must never signal')

            monkeypatch.setattr(session.env_ref, 'signal_owned', forbidden)
            responses = [process('poll', sid), process('log', sid), process('wait', sid, timeout=1)]
            assert all(r['process_running'] is False and r['output_pending'] for r in responses)
            assert session.owned_lifecycle['cancelled'] is False and session.owned_lifecycle['root_exit'] == 17
            assert not session.exited and not registry.is_completion_consumed(sid)
            assert registry.completion_queue.empty()
            release.set()
            assert session._completion_event.wait(5)
            final = process('wait', sid, timeout=1)
            assert final['exit_code'] == 17 and final['output'] == 'NATURAL_PENDING'
            request.node.user_properties.append(('natural_pending', json.dumps({'responses':responses,'final':final})))
    finally:
        release.set()
