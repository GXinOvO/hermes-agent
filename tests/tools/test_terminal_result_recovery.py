"""Restart recovery uses completed receipts and the native completion admission only."""
import asyncio
import json
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tools import process_registry as pr
from tools import process_registry_results as receipts


def _exit(registry, owner, key, *, notify=True, command='printf recovered'):
    with scoped_current_session_id(owner):
        session = registry.spawn_local(command, session_key=key, task_id='terminal-owner')
    session.notify_on_complete = notify
    session.watcher_platform = 'local'
    session.watcher_interval = 1
    assert session._completion_event.wait(15), 'real child did not finish'
    assert session.exited and session.exit_code == 0
    return session


def _runner_stub():
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._start_register_plugins_relay_hooks = MagicMock()
    runner._start_relay_provision_retry = MagicMock()
    runner._subscribe_plugin_rewire = MagicMock()
    runner.hooks = MagicMock()
    async def recover(*a, **kw):
        return (0, 0)
    async def clean(*a, **kw):
        return 0
    runner._recover_unclean_sessions = recover
    runner._consume_clean_shutdown_marker = clean
    runner._suspend_stuck_loop_sessions = lambda: 0
    return runner


@pytest.mark.asyncio
async def test_startup_terminal_receipt_reuses_identity_and_native_admission(tmp_path, monkeypatch):
    from gateway.session_automation import completion_admission
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_session_end_delegations import _local
    from tests.gateway.test_unviewed_completion_notes import _restart
    from tools import terminal_tool as terminal
    from tools.registry import registry as tool_registry

    runner, authority, ref, _ = await _local(tmp_path, monkeypatch)
    owner = runner.session_store.lookup_by_session_key(authority.sessions[ref.session_id].route).session_id
    key = authority.sessions[ref.session_id].route
    marker = tmp_path / 'executions'
    gate = tmp_path / 'release'
    script = ('import pathlib,time; p=pathlib.Path(' + repr(str(gate)) + '); '
              '\nwhile not p.exists(): time.sleep(.01)\n'
              'with open(' + repr(str(marker)) + ', "a") as f: f.write("one\\n")\n'
              'print("TERMINAL_RECOVERED")')
    command = shlex.join([sys.executable, '-c', script])
    producer = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', producer)
    monkeypatch.setattr(terminal, '_check_all_guards', lambda *a, **k: {'approved': True})
    # The terminal dispatch and local executor are real; only its environment factory is isolated.
    monkeypatch.setattr(terminal, '_active_environments', {'terminal-owner': SimpleNamespace(env={})})
    monkeypatch.setattr(terminal, '_last_activity', {'terminal-owner': 0})
    monkeypatch.setattr(terminal, '_task_env_overrides', {})
    monkeypatch.setattr(terminal, '_start_cleanup_thread', lambda: None)
    monkeypatch.setattr(terminal, '_resolve_container_task_id', lambda value: value or 'default')
    from tests.tools.test_terminal_error_redaction import _minimal_terminal_config
    monkeypatch.setattr(terminal, '_get_env_config', lambda: _minimal_terminal_config(str(tmp_path)))
    with scoped_current_session_id(owner):
        spawn = json.loads(tool_registry.get_entry('terminal').handler(
            {'command': command, 'background': True, 'notify': True, 'workdir': str(tmp_path)},
            task_id=key, session_id=key))
    assert spawn.get('notify_on_complete') is True, spawn
    session = producer.get(spawn['session_id'])
    # Same routing stamped by the local gateway's spawning turn.
    session.watcher_platform = 'local'
    session.watcher_interval = 1
    gate.touch()
    assert await asyncio.to_thread(session._completion_event.wait, 15)
    path = get_hermes_home() / 'logs' / 'process-results' / f'{session.id}.json'
    original = json.loads(path.read_text())
    live_event = next(e for e in producer.completion_queue.queue if e.get('session_id') == session.id)

    restarted = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', restarted)
    await _runner_stub()._start_recover_previous_run()
    assert len(restarted.pending_watchers) == 1, 'startup must resume a completed terminal notification'
    assert restarted.recover_completed_results() == 0
    assert len(restarted.pending_watchers) == 1 and restarted.completion_queue.qsize() == 1
    recovered_event = restarted.completion_queue.get_nowait()
    assert recovered_event['restored'] is True
    assert runner._completion_delivery_identity(recovered_event) == runner._completion_delivery_identity(live_event)
    assert recovered_event['result_fingerprint'] == original['result_fingerprint']
    restarted.completion_queue.put(recovered_event)
    # Restored entries cached for the watcher must still stay private to their owner.
    with scoped_current_session_id('unrelated'):
        assert restarted.poll(session.id)['status'] == 'not_found'
        assert restarted.list_sessions(include_retained=True) == []
        assert restarted.drain_notifications(session_key=key) == []
    assert restarted.drain_notifications() == []
    assert restarted.completion_queue.get_nowait()['result_fingerprint'] == original['result_fingerprint']
    with scoped_current_session_id(owner):
        monkeypatch.setattr(restarted, 'spawn_local', lambda *a, **k: pytest.fail('receipt recovery reran a process'))
        await runner._run_process_watcher(restarted.pending_watchers[0])
    rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
    assert len(rows) == 1 and 'TERMINAL_RECOVERED' in rows[0]['payload']['text']
    assert completion_admission(runner, recovered_event)['admission_id'] == rows[0]['admission_id']
    # Real owner epoch restart clears the in-memory delivery map; the SQLite admission remains.
    runner, authority = await _restart(tmp_path, monkeypatch, authority)
    again = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', again)
    await _runner_stub()._start_recover_previous_run()
    assert len(again.pending_watchers) == 1
    with scoped_current_session_id(owner):
        await runner._run_process_watcher(again.pending_watchers[0])
    assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 1
    assert marker.read_text() == 'one\n'
    assert not again._running
    authority.db.close()


@pytest.mark.parametrize('variant', ['notify_false', 'consumed', 'legacy', 'tampered', 'profile_copy',
                                    'wrong_owner', 'bad_id', 'live_incarnation', 'missing_owner'])
def test_recovery_is_fail_closed_but_queries_stay_read_only(tmp_path, monkeypatch, variant):
    from hermes_state import SessionDB
    home = get_hermes_home()
    db = SessionDB()
    db.create_session('receipt-owner', 'cli', model='test')
    db.create_session('unrelated', 'cli', model='test')
    producer = pr.ProcessRegistry()
    session = _exit(producer, 'receipt-owner', 'local:terminal-test', notify=variant != 'notify_false',
                    command='sleep .1; printf recovered')
    path = home / 'logs' / 'process-results' / f'{session.id}.json'
    if variant == 'consumed':
        before = json.loads(path.read_text())
        with scoped_current_session_id('receipt-owner'):
            assert producer.wait(session.id, timeout=1)['exit_code'] == 0
        after = json.loads(path.read_text())
        assert after['completion_consumed'] is True
        assert after['source_fingerprint'] == before['source_fingerprint']
        assert after['result_fingerprint'] == before['result_fingerprint']
    record = json.loads(path.read_text())
    if variant == 'legacy':
        for field in ('receipt_version', 'profile_home', 'source_fingerprint', 'result_fingerprint'):
            record.pop(field, None)
    if variant == 'tampered':
        record['output'] = 'substituted output'
    if variant == 'wrong_owner':
        record['parent_session_id'] = 'unrelated'
    if variant == 'bad_id':
        # A matching filename and a checksum cannot turn a noncanonical producer ID into a verified dispatch.
        record['id'] = 'proc_NOT_CANONICAL'
        record['source_fingerprint'] = receipts._fingerprint(record, receipts._SOURCE_FIELDS)
        record['result_fingerprint'] = receipts._fingerprint(record, ('source_fingerprint', *receipts._COMPLETED_FIELDS))
        record['consumption_fingerprint'] = receipts._fingerprint(record, ('result_fingerprint', 'completion_consumed'))
        path.unlink()
        path = path.with_name('proc_NOT_CANONICAL.json')
    path.write_text(json.dumps(record))
    target_home = home
    if variant == 'profile_copy':
        target_home = tmp_path / 'other-profile'
        copied = target_home / 'logs' / 'process-results' / path.name
        copied.parent.mkdir(parents=True)
        copied.write_text(path.read_text())
    token = set_hermes_home_override(target_home)
    try:
        reader = pr.ProcessRegistry()
        if variant == 'live_incarnation':
            reader._running[session.id] = pr.ProcessSession(id=session.id, command='still live', pid=123)
        if variant == 'missing_owner':
            db.delete_session('receipt-owner')
        with scoped_current_session_id('receipt-owner'):
            if variant in {'notify_false', 'consumed', 'legacy'}:
                result = reader.poll(session.id)
                assert result['status'] == 'exited' and result['exit_code'] == 0
                assert result['result_verification'] == ('legacy_unverified' if variant == 'legacy' else 'verified')
            assert reader.completion_queue.empty() and reader.pending_watchers == []
        assert reader.recover_completed_results() == 0
        assert reader.completion_queue.empty() and reader.pending_watchers == []
        # No unfiltered CLI consumer may take a restored receipt belonging to another owner.
        assert reader.drain_notifications() == []
    finally:
        reset_hermes_home_override(token)
        db.close()


@pytest.mark.parametrize('fail_write', [False, True], ids=['persisted', 'failed'])
def test_process_tool_exposes_durability_without_changing_exit(tmp_path, monkeypatch, fail_write):
    from tools.registry import registry as tool_registry
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    if fail_write:
        def fail(*a, **k):
            raise OSError('injected disk unavailable')
        monkeypatch.setattr(receipts, 'atomic_json_write', fail)
    session = _exit(registry, 'receipt-owner', 'local:terminal-test', command='sleep .1; printf durable')
    expected = 'failed' if fail_write else 'persisted'
    with scoped_current_session_id('receipt-owner'):
        result = json.loads(tool_registry.get_entry('process_manage').handler({'action': 'poll', 'session_id': session.id}))
    assert (result['status'], result['exit_code'], result['result_durability']) == ('exited', 0, expected)
    assert result['result_durability'] == session.result_durability
    queued = next(e for e in registry.completion_queue.queue if e.get('session_id') == session.id)
    assert queued['result_durability'] == expected
    if fail_write:
        assert not (get_hermes_home() / 'logs' / 'process-results' / f'{session.id}.json').exists()
