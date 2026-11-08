import os
from pathlib import Path
import threading

import pytest

from gateway import session_context
from gateway.session_finite import finite_turn_scope
from tests.tools import test_process_registry_owned_completion as owned_fixtures
from tools import interrupt
from tools import process_registry_wait_policy as policy

owned = owned_fixtures.owned
native = owned_fixtures.native
pytestmark = pytest.mark.platforms('linux')


@pytest.mark.parametrize('action', ['wait', 'poll'])
@pytest.mark.parametrize('delivery', ['interactive', 'disabled', 'no-delivery'])
def test_pending_repeat_keeps_verified_termination(owned, monkeypatch, action, delivery):
    _, _, registry, _, receipts, _, _ = owned
    session, release, _ = owned_fixtures.stalled_job(owned, monkeypatch)
    killed = owned_fixtures.manage('kill', session)
    assert killed['output_pending'] is True and killed['process_running'] is False
    home = Path(os.environ['HERMES_HOME'])
    (home / 'config.yaml').write_text(
        'terminal:\n  process_wait_auto_notify: ' + ('false' if delivery == 'disabled' else 'true')
        + '\ndisplay:\n  background_process_notifications: full\n')
    tokens = session_context.set_session_vars(source='desktop', async_delivery=delivery != 'no-delivery')
    results = []
    receipts.update(delivery=delivery, action=action, calls=results)
    try:
        with finite_turn_scope(False):
            assert policy._enabled() is (delivery != 'disabled')
            assert policy._can_deliver_completion() is (delivery != 'no-delivery')
            assert policy._caller_receives_completion(session)
            for _ in range(3):
                result = owned_fixtures.manage(action, session, timeout=1)
                results.append(result)
            for result in results:
                note = result.get('timeout_note', '') + result.get('note', '')
                assert result['output_pending'] is True and result['process_running'] is False
                assert 'still running' not in note, result
                assert 'Do NOT call process_manage' not in note, result
                assert not registry.is_completion_consumed(session.id)
    finally:
        session_context.clear_session_vars(tokens)
    release.set()
    session._reader_thread.join(8)
    assert not session._reader_thread.is_alive()
    final = owned_fixtures.snapshot(registry, session)
    receipts['final'] = final
    assert final['durable']['output'] == 'HEADTAIL'
    assert final['event_count'] == 1 and not final['consumed']


@pytest.mark.parametrize('release_kind', ['interrupt', 'yield'])
def test_pending_wait_release_keeps_verified_termination(owned, monkeypatch, release_kind):
    _, _, registry, _, receipts, _, _ = owned
    session, release, _ = owned_fixtures.stalled_job(owned, monkeypatch)
    killed = owned_fixtures.manage('kill', session)
    assert killed['output_pending'] is True and killed['process_running'] is False
    monkeypatch.setenv('TERMINAL_TIMEOUT', '1')
    try:
        if release_kind == 'interrupt':
            interrupt.set_interrupt(True)
        else:
            thread_id = threading.current_thread().ident
            assert thread_id is not None
            interrupt.request_yield(thread_id)
        result = owned_fixtures.manage('wait', session, timeout=600)
    finally:
        interrupt.set_interrupt(False)
    before = owned_fixtures.snapshot(registry, session)
    receipts.update(release_kind=release_kind, result=result, before=before)
    assert result['status'] == 'interrupted'
    assert result.get('output_pending') is True and result.get('process_running') is False, result
    assert result['owned_lifecycle'] == killed['owned_lifecycle']
    assert result['timeout_note'].startswith('Requested wait of 600s was clamped')
    assert 'termination verified' in result['note'].lower()
    assert 'still running' not in result['note']
    assert not before['consumed'] and before['durable'] is None and before['event_count'] == 0
    release.set()
    session._reader_thread.join(8)
    final = owned_fixtures.snapshot(registry, session)
    receipts['final'] = final
    assert final['durable']['output'] == 'HEADTAIL' and final['event_count'] == 1
    assert not final['consumed']


@pytest.mark.parametrize('release_kind', ['interrupt', 'yield'])
def test_live_owned_wait_release_keeps_running_contract(owned, release_kind):
    _, env, registry, _, receipts, sessions, _ = owned
    ready = threading.Event()
    registry.on_output = lambda _session, chunk: ready.set() if 'READY' in chunk else None
    session = registry.spawn_via_env(env, 'printf READY; sleep 30', cwd='/workspace', task_id='live-release')
    sessions.append(session)
    session.notify_on_complete = True
    assert ready.wait(8)
    try:
        if release_kind == 'interrupt':
            interrupt.set_interrupt(True)
        else:
            thread_id = threading.current_thread().ident
            assert thread_id is not None
            interrupt.request_yield(thread_id)
        result = owned_fixtures.manage('wait', session, timeout=15)
    finally:
        interrupt.set_interrupt(False)
    receipts.update(release_kind=release_kind, result=result)
    assert result['status'] == 'interrupted' and not result.get('output_pending')
    assert 'termination verified' not in result['note'].lower()
    if release_kind == 'yield':
        assert result['process_running'] is True and 'still running' in result['note']
    assert session.owned_handle.poll() is None
    assert not registry.is_completion_consumed(session.id)
    killed = owned_fixtures.manage('kill', session)
    assert killed['status'] == 'killed' and killed['output_pending'] is False
    assert killed['output'] == 'READY'
