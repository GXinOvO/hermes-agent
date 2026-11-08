"""KQR3-002: real owned readers retain the terminal tail across kill races."""

import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, cast

import pytest

from tests.tools import test_owned_process_native as native_fixtures
from tools import process_registry as pr
from tools.registry import registry as tools

native = native_fixtures.native


@pytest.fixture
def owned(native, tmp_path, request):
    from tools.terminal_tool_backends import _create_environment
    import sys
    project, registry, _ = native
    env = _create_environment('owned_process', image='', cwd='/workspace', timeout=8)
    plugin = sys.modules[env.__class__.__module__]
    sessions, releases = [], []
    receipts = {'entry':'native discovery -> configured factory -> owned reader -> process_manage'}
    yield plugin, env, registry, project, receipts, sessions, releases
    for release in releases:
        release.set()
    for session in sessions:
        if not session.owned_handle.done.is_set():
            env.signal_owned(session.owned_handle)
        session._reader_thread.join(8)
        assert not session._reader_thread.is_alive()
    env.cleanup()
    assert env.proc.poll() is not None
    receipts['cleanup'] = {'launcher_exit':env.proc.returncode, 'readers_stopped':True}
    request.node.user_properties.append(('migrated_receipts',json.dumps(receipts)))


def manage(action, session, **arguments):
    response = tools.dispatch('process_manage', {
        'action': action, 'session_id': session.id, **arguments})
    assert isinstance(response, str)
    return json.loads(response)


def stalled_job(owned, monkeypatch, *, naturally_exits=False):
    plugin, env, registry, project, receipts, sessions, releases = owned
    entered, release, remote_tail = threading.Event(), threading.Event(), threading.Event()
    releases.append(release)
    observed = []

    def sink(_session, chunk):
        observed.append(chunk)
        if 'HEAD' in chunk:
            entered.set()
            if not release.wait(30):
                raise RuntimeError('test consumer release missing')

    original = plugin.RemoteHandle.output

    def observe(handle, raw):
        original(handle, raw)
        if b'TAIL' in raw:
            remote_tail.set()

    monkeypatch.setattr(plugin.RemoteHandle, 'output', observe)
    registry.on_output = sink
    end = 'exit 17' if naturally_exits else 'sleep 30'
    session = registry.spawn_via_env(
        env, 'printf HEAD; while [ ! -f /workspace/tail-go ]; do :; done; printf TAIL; ' + end,
        cwd='/workspace', task_id='tail-owner', owner_task_id='tail-owner')
    sessions.append(session)
    session.notify_on_complete = True
    registry._write_checkpoint()
    assert entered.wait(8), 'real reader did not enter HEAD consumer'
    (project / 'tail-go').touch()
    assert remote_tail.wait(8), 'real remote owner did not acknowledge TAIL'
    if naturally_exits:
        assert session.owned_handle.done.wait(8)
    receipts.update(remote_tail_ack=True, reader_entered=True, namespace=env.receipt)
    return session, release, observed


def snapshot(registry, session):
    durable = Path(os.environ['HERMES_HOME']) / 'logs/process-results' / (session.id + '.json')
    checkpoint = json.loads(pr._checkpoint_path().read_text())
    return {
        'reader_alive': session._reader_thread.is_alive(), 'exited': session.exited,
        'completion_set': session._completion_event.is_set(),
        'consumed': registry.is_completion_consumed(session.id),
        'running_table': session.id in registry._running,
        'finished_table': session.id in registry._finished,
        'durable': json.loads(durable.read_text()) if durable.exists() else None,
        'checkpoint': [e for e in checkpoint if e['session_id'] == session.id],
        'event_count': registry.completion_queue.qsize(), 'output': session.output_buffer,
    }


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('naturally_exits', [False, True], ids=['cancelled', 'already_exited'])
def test_slow_consumer_keeps_reader_as_final_completion_owner(owned, monkeypatch, naturally_exits):
    _, _, registry, _, receipts, _, _ = owned
    session, release, observed = stalled_job(owned, monkeypatch, naturally_exits=naturally_exits)
    start = time.monotonic()
    result = manage('kill', session)
    receipts['kill_seconds'] = time.monotonic() - start
    before = snapshot(registry, session)
    receipts.update(kill=result, before=before)
    assert receipts['kill_seconds'] < 8, 'kill waited indefinitely for its output consumer'
    assert result['owned_lifecycle']['descendant_empty'] is True
    assert result['owned_lifecycle']['cancelled'] is not naturally_exits
    assert result['status'] == ('already_exited' if naturally_exits else 'killed')
    assert before['reader_alive'] and before['output'] == 'HEAD'
    assert before['durable'] is None, 'published incomplete HEAD before reader drained TAIL'
    assert not before['completion_set'] and not before['consumed']
    assert before['event_count'] == 0 and before['running_table'] and not before['finished_table']
    assert len(before['checkpoint']) == 1
    assert result['output_pending'] is True and result['process_running'] is False
    polled = manage('poll', session)
    logged = manage('log', session)
    waited = manage('wait', session, timeout=1)
    listing = tools.dispatch('process_manage', {'action': 'list'})
    assert isinstance(listing, str)
    listed = json.loads(listing)['processes']
    receipts.update(pending_poll=polled, pending_log=logged, pending_wait=waited, pending_list=listed)
    assert len(listed) == 1
    for response in (polled, logged, waited, listed[0]):
        assert response['output_pending'] is True and response['process_running'] is False
    assert not registry.is_completion_consumed(session.id)
    release.set()
    session._reader_thread.join(8)
    assert not session._reader_thread.is_alive()
    after = snapshot(registry, session)
    event = registry.completion_queue.get_nowait()
    receipts.update(after=after, completion=event, consumer_chunks=observed)
    assert ''.join(observed) == 'HEADTAIL'
    assert after['durable']['output'] == after['output'] == event['output'] == 'HEADTAIL'
    assert after['completion_set'] and not after['consumed'] and after['finished_table']
    assert after['checkpoint'] == [] and registry.completion_queue.empty()
    expected_reason = 'exited' if naturally_exits else 'killed'
    assert after['durable']['completion_reason'] == event['completion_reason'] == expected_reason
    assert after['durable']['owned_lifecycle'] == event['owned_lifecycle'] == result['owned_lifecycle']
    from tools.process_registry_notifications import format_process_notification
    formatted = format_process_notification(event)
    receipts['formatted_notification'] = formatted
    assert formatted is not None and 'HEADTAIL' in formatted
    assert registry.unread_completions_owned_by('tail-owner') == [session]
    final = manage('kill', session)
    receipts['final_kill'] = final
    assert final['status'] == 'already_exited' and final['output'] == 'HEADTAIL'
    assert registry.is_completion_consumed(session.id)
    assert registry.unread_completions_owned_by('tail-owner') == []
    assert registry.completion_queue.empty()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('consume', [False, True], ids=['unread', 'consumed'])
def test_normal_owned_kill_preserves_consumption_and_repeat_contract(owned, consume):
    _, env, registry, _, receipts, sessions, _ = owned
    ready = threading.Event()
    registry.on_output = lambda _s, chunk: ready.set() if 'READY' in chunk else None
    session = registry.spawn_via_env(env, 'printf READY; sleep 30', cwd='/workspace',
                                     task_id='normal-owner')
    sessions.append(session)
    session.notify_on_complete = True
    assert ready.wait(8)
    result = registry.kill_process(session.id, consume_output=consume)
    final = snapshot(registry, session)
    receipts.update(kill=result, final=final)
    assert result['status'] == 'killed' and result['output_pending'] is False
    assert result['output'] == final['durable']['output'] == 'READY'
    assert final['consumed'] is consume and final['checkpoint'] == []
    assert final['event_count'] == 1 and final['completion_set']
    assert final['durable']['owned_lifecycle']['descendant_empty'] is True
    assert bool(registry.unread_completions_owned_by('normal-owner')) is not consume
    repeated = registry.kill_process(session.id, consume_output=False)
    assert repeated['status'] == 'already_exited' and repeated['output'] == 'READY'
    assert registry.is_completion_consumed(session.id) is consume
    assert registry.completion_queue.get_nowait()['output'] == 'READY'
    assert registry.completion_queue.empty()


@pytest.mark.platforms('linux')
def test_concurrent_owned_kill_wait_log_preserves_one_final_tail(owned, monkeypatch):
    _, _, registry, _, receipts, _, _ = owned
    session, release, _ = stalled_job(owned, monkeypatch)
    first = registry.kill_process(session.id, source='first-stop', consume_output=False)
    assert first['output_pending'] and not registry.is_completion_consumed(session.id)
    gate = threading.Barrier(4)
    results, errors = [], []

    def call(action):
        try:
            gate.wait(8)
            results.append(manage(action, session, timeout=1))
        except Exception as exc:
            errors.append(repr(exc))

    workers = [threading.Thread(target=call, args=(action,)) for action in ('kill', 'wait', 'log')]
    for worker in workers:
        worker.start()
    gate.wait(8)
    for worker in workers:
        worker.join(8)
        assert not worker.is_alive()
    assert not errors, errors
    before = snapshot(registry, session)
    receipts.update(first=first, concurrent=results, before=before)
    assert all(r['output_pending'] and r['process_running'] is False for r in results)
    assert not before['consumed'] and before['durable'] is None and before['event_count'] == 0
    release.set()
    session._reader_thread.join(8)
    after = snapshot(registry, session)
    assert after['durable']['output'] == 'HEADTAIL' and after['event_count'] == 1
    assert after['durable']['termination_source'] == 'first-stop'
    event = registry.completion_queue.get_nowait()
    assert event['output'] == 'HEADTAIL'
    assert not registry.is_completion_consumed(session.id)
    result_gate = threading.Barrier(4)
    completed = []

    def consume(action):
        result_gate.wait(8)
        completed.append(manage(action, session, timeout=1))

    consumers = [threading.Thread(target=consume, args=(a,)) for a in ('kill', 'wait', 'log')]
    for worker in consumers:
        worker.start()
    result_gate.wait(8)
    for worker in consumers:
        worker.join(8)
        assert not worker.is_alive()
    receipts.update(after=after, event=event, completed=completed)
    assert len(completed) == 3 and all(r['output'] == 'HEADTAIL' for r in completed)
    assert registry.is_completion_consumed(session.id) and registry.completion_queue.empty()
    consumed_record = snapshot(registry, session)['durable']
    assert consumed_record['completion_consumed'] is True
    for field in after['durable']:
        if field not in ('completion_consumed', 'consumption_fingerprint'):
            assert consumed_record[field] == after['durable'][field]


@pytest.mark.platforms('linux')
def test_reader_wait_error_cannot_be_promoted_by_verified_kill(owned, monkeypatch):
    _, env, registry, _, receipts, _, _ = owned
    session, release, _ = stalled_job(owned, monkeypatch, naturally_exits=True)
    original = env.wait_owned
    reader_called = threading.Event()

    def readback(handle, *args, **kwargs):
        if threading.current_thread() is session._reader_thread:
            reader_called.set()
            raise RuntimeError('injected reader receipt read failure')
        return original(handle, *args, **kwargs)

    monkeypatch.setattr(env, 'wait_owned', readback)
    release.set()
    assert reader_called.wait(8)
    session._reader_thread.join(8)
    assert not session._reader_thread.is_alive()
    result = manage('kill', session)
    final = snapshot(registry, session)
    receipts.update(kill=result, final=final, reader_error_observed=reader_called.is_set())
    assert result['status'] == 'already_exited' and result['output_pending'] is True
    assert result['process_running'] is False
    assert final['output'] == 'HEADTAIL' and final['durable'] is None
    assert not final['exited'] and not final['completion_set'] and not final['consumed']
    assert final['running_table'] and final['event_count'] == 0


@pytest.mark.platforms('linux')
def test_unverified_owned_kill_keeps_real_reader_manageable(owned, monkeypatch):
    _, env, registry, _, receipts, _, _ = owned
    session, release, _ = stalled_job(owned, monkeypatch)

    def unavailable(_handle):
        raise RuntimeError('injected signal receipt unavailable')

    with monkeypatch.context() as failure:
        failure.setattr(env, 'signal_owned', unavailable)
        result = manage('kill', session)
    before = snapshot(registry, session)
    receipts.update(unverified=result, before=before)
    assert result['status'] == 'error' and result['process_running'] is True
    assert session.owned_handle.poll() is None
    assert not before['exited'] and not before['consumed'] and before['durable'] is None
    assert before['event_count'] == 0 and before['running_table']
    release.set()
    verified = manage('kill', session)
    final = snapshot(registry, session)
    receipts.update(verified=verified, final=final)
    assert verified['output'] == 'HEADTAIL' and verified['output_pending'] is False
    assert final['durable']['output'] == 'HEADTAIL' and final['event_count'] == 1


@pytest.mark.platforms('linux')
def test_reader_publication_in_progress_cannot_consume_completion(owned, monkeypatch):
    _, env, registry, project, receipts, sessions, releases = owned
    publishing, release = threading.Event(), threading.Event()
    releases.append(release)
    checkpoint = registry._write_checkpoint

    def blocked_checkpoint(*args, **kwargs):
        if threading.current_thread().name.startswith('proc-owned-reader-'):
            publishing.set()
            if not release.wait(30):
                raise RuntimeError('reader publication release missing')
        return checkpoint(*args, **kwargs)

    monkeypatch.setattr(registry, '_write_checkpoint', blocked_checkpoint)
    session = registry.spawn_via_env(
        env, 'while [ ! -f /workspace/finish ]; do :; done; printf HEADTAIL; exit 17',
        cwd='/workspace', task_id='publishing-owner')
    sessions.append(session)
    session.notify_on_complete = True
    (project / 'finish').touch()
    assert publishing.wait(8), 'real reader did not reach completion publication'
    killed = manage('kill', session)
    logged = manage('log', session)
    waited = manage('wait', session, timeout=1)
    before = snapshot(registry, session)
    receipts.update(kill=killed, log=logged, wait=waited, before=before)
    assert all(r['output_pending'] and r['output'] == 'HEADTAIL' for r in (killed, logged, waited))
    assert before['exited'] and before['durable']['output'] == 'HEADTAIL'
    assert not before['completion_set'] and not before['consumed'] and before['event_count'] == 0
    release.set()
    session._reader_thread.join(8)
    event = registry.completion_queue.get_nowait()
    assert event['output'] == 'HEADTAIL' and event['exit_code'] == 17
    assert registry.completion_queue.empty()
    assert not registry.is_completion_consumed(session.id)
    receipts.update(event=event, after=snapshot(registry, session))
