"""Pending terminal receipts survive history pressure and a fresh result reader."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_constants import get_hermes_home
from tools import process_registry as pr
from tools import process_registry_results as receipts
from tools.registry import registry as tools


@pytest.fixture
def native_terminal(tmp_path, monkeypatch):
    from tests.tools.test_terminal_error_redaction import _minimal_terminal_config
    from tools import terminal_tool as terminal

    producer = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', producer)
    monkeypatch.setattr(terminal, '_check_all_guards', lambda *a, **k: {'approved': True})
    monkeypatch.setattr(terminal, '_active_environments', {'receipt-task': SimpleNamespace(env={})})
    monkeypatch.setattr(terminal, '_last_activity', {'receipt-task': 0})
    monkeypatch.setattr(terminal, '_task_env_overrides', {})
    monkeypatch.setattr(terminal, '_start_cleanup_thread', lambda: None)
    monkeypatch.setattr(terminal, '_resolve_container_task_id', lambda value: 'receipt-task')
    monkeypatch.setattr(terminal, '_get_env_config', lambda: _minimal_terminal_config(str(tmp_path)))
    return producer


def launch_native(producer, tmp_path, index, *, notify=True, worker=False):
    gate = tmp_path / f'release-{index}'
    marker = tmp_path / 'executions'
    script = textwrap.dedent(f'''\
        import pathlib, time
        gate = pathlib.Path({str(gate)!r})
        deadline = time.monotonic() + 20
        while not gate.exists():
            if time.monotonic() > deadline:
                raise SystemExit(91)
            time.sleep(.01)
        with open({str(marker)!r}, 'a') as stream:
            stream.write({str(index)!r} + '\\n')
        print('RETAINED_{index}')
    ''')
    with scoped_current_session_id('receipt-owner'):
        result = json.loads(tools.get_entry('terminal').handler({
            'command': shlex.join([sys.executable, '-c', script]),
            'background': True, 'notify': notify, 'workdir': str(tmp_path),
        }, task_id='receipt-task', session_id='local:receipt'))
    assert result.get('error') is None, result
    session = producer.get(result['session_id'])
    assert session is not None
    if notify:
        assert result.get('notify_on_complete') is True, result
    if not worker:
        gate.touch()
        assert session._completion_event.wait(20), 'native command did not settle'
        session._reader_thread.join(10)
        assert not session._reader_thread.is_alive()
        assert session.exit_code == 0 and session.result_durability == 'persisted'
    return session, gate


def read_in_fresh_owner(tmp_path, ids):
    script = textwrap.dedent('''\
        import json, sys
        from gateway.session_context import scoped_current_session_id
        from hermes_constants import get_hermes_home
        from tools import process_registry as pr
        from tools.registry import registry
        def forbidden(*args, **kwargs):
            raise AssertionError('result lookup attempted execution')
        pr.process_registry.spawn_local = forbidden
        pr.process_registry.spawn_via_env = forbidden
        with scoped_current_session_id('receipt-owner'):
            rows = json.loads(registry.get_entry('process_manage').handler({'action': 'list'}))
            results, acks = [], []
            for sid in json.loads(sys.argv[1]):
                results.append(json.loads(registry.get_entry('process_manage').handler(
                    {'action': 'wait', 'session_id': sid, 'timeout': 1})))
                path = get_hermes_home() / 'logs' / 'process-results' / (sid + '.json')
                acks.append(json.loads(path.read_text()) if path.exists() else None)
        print(json.dumps({'listed': rows, 'results': results, 'acks': acks,
                          'queued': pr.process_registry.completion_queue.qsize(),
                          'running': list(pr.process_registry._running)}))
    ''')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
           'HERMES_HOME': str(get_hermes_home())}
    run = subprocess.run([sys.executable, '-c', script, json.dumps(ids)], cwd=tmp_path,
                         env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=60, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    return json.loads(run.stdout)


@pytest.mark.platforms('linux', 'macos')
def test_native_65_pending_results_survive_fresh_owner_and_ack(native_terminal, tmp_path):
    sessions = [launch_native(native_terminal, tmp_path, index)[0] for index in range(65)]
    marker = (tmp_path / 'executions').read_bytes()
    assert marker.splitlines() == [str(i).encode() for i in range(65)]
    directory = get_hermes_home() / 'logs' / 'process-results'
    first = directory / f'{sessions[0].id}.json'
    assert first.exists(), 'history trimming deleted the earliest unconsumed native receipt'
    original = {s.id: json.loads((directory / f'{s.id}.json').read_text()) for s in sessions}
    assert all(r['completion_consumed'] is False for r in original.values())
    recovered = read_in_fresh_owner(tmp_path, [s.id for s in sessions])
    assert {row['session_id'] for row in recovered['listed']['processes']} == set(original)
    assert len(recovered['results']) == 65
    for index, result in enumerate(recovered['results']):
        assert result['status'] == 'exited' and result['exit_code'] == 0, result
        assert f'RETAINED_{index}' in result['output']
        record = recovered['acks'][index]
        assert record['completion_consumed'] is True
        assert record['source_fingerprint'] == original[sessions[index].id]['source_fingerprint']
        assert record['result_fingerprint'] == original[sessions[index].id]['result_fingerprint']
        assert record['consumption_fingerprint'] != original[sessions[index].id]['consumption_fingerprint']
    assert recovered['queued'] == 0 and recovered['running'] == []
    assert (tmp_path / 'executions').read_bytes() == marker
    assert pr.ProcessRegistry().recover_completed_results() == 0
    print(json.dumps({'native_commands': 65, 'fresh_owner_results': len(recovered['results']),
                      'history_cap': receipts.MAX_RETAINED_RESULTS, 'execution_bytes_unchanged': True}))


def test_pending_and_unreadable_receipts_do_not_use_consumed_history_slots(native_terminal, tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, 'MAX_RETAINED_RESULTS', 2)
    pending = launch_native(native_terminal, tmp_path, 0, notify=False)[0]
    directory = get_hermes_home() / 'logs' / 'process-results'
    damaged = directory / 'proc_damaged.json'
    damaged.write_text('null')
    consumed = []
    for index in range(1, 5):
        session = launch_native(native_terminal, tmp_path, index)[0]
        with scoped_current_session_id('receipt-owner'):
            assert native_terminal.wait(session.id, timeout=1)['exit_code'] == 0
        consumed.append(session)
    with scoped_current_session_id('receipt-owner'):
        listed = receipts.load_completed_results()
    assert pending.id in listed, 'notify=false is still an unconsumed result'
    assert {s.id for s in consumed if s.id in listed} == {s.id for s in consumed[-2:]}
    assert damaged.read_text() == 'null'
    assert listed[pending.id]._result_consumed is False
    assert len(list(directory.glob('proc_*.json'))) == 4


@pytest.mark.parametrize('damaged_text', ['null', '[]', '{', '{"id": []}'])
def test_bad_receipt_does_not_block_valid_sibling(native_terminal, tmp_path, damaged_text):
    session = launch_native(native_terminal, tmp_path, 0)[0]
    damaged = get_hermes_home() / 'logs' / 'process-results' / 'proc_damaged.json'
    damaged.write_text(damaged_text)
    with scoped_current_session_id('receipt-owner'):
        results = receipts.load_completed_results()
    assert set(results) == {session.id}
    assert results[session.id].output_buffer.strip() == 'RETAINED_0'
    assert damaged.read_text() == damaged_text


@pytest.mark.parametrize('consumed', [False, True], ids=['pending', 'consumed'])
def test_ttl_still_expires_native_receipts(native_terminal, tmp_path, consumed):
    import time

    session = launch_native(native_terminal, tmp_path, 0, notify=False)[0]
    path = get_hermes_home() / 'logs' / 'process-results' / f'{session.id}.json'
    if consumed:
        with scoped_current_session_id('receipt-owner'):
            assert native_terminal.wait(session.id, timeout=1)['exit_code'] == 0
    expired = time.time() - receipts.RESULT_RETENTION_SECONDS - 10
    os.utime(path, (expired, expired))
    with scoped_current_session_id('receipt-owner'):
        assert pr.ProcessRegistry().poll(session.id)['status'] == 'not_found'
    assert not path.exists()
    assert (tmp_path / 'executions').read_text() == '0\n'


@pytest.mark.platforms("linux", "macos")
def test_silent_background_guidance_keeps_the_original_execution(tmp_path, native_terminal):
    producer = native_terminal
    terminal_entry = tools.get_entry("terminal")
    process_entry = tools.get_entry("process_manage")
    assert terminal_entry is not None and process_entry is not None
    marker = tmp_path / "executions"
    command = shlex.join([
        sys.executable, "-c",
        f"from pathlib import Path; p=Path({str(marker)!r}); "
        r"p.open('a').write('once\n'); print('FINISHED')",
    ])

    with scoped_current_session_id("silent-guidance-owner"):
        result = json.loads(terminal_entry.handler(
            {"command": command, "background": True, "notify": False, "workdir": str(tmp_path)},
            task_id="silent-guidance-task", session_id="silent-guidance-session"))
        assert result.get("error") is None, result
        session = producer.get(result["session_id"])
        try:
            hint = result["hint"]
            assert "Do NOT re-launch" in hint, hint
            assert "notify_on_complete=true" not in hint, hint
            assert "session_id" in hint and "wait" in hint and "poll" in hint, hint
            waited = json.loads(process_entry.handler(
                {"action": "wait", "session_id": result["session_id"], "timeout": 10}))
            assert waited["status"] == "exited" and waited["exit_code"] == 0, waited
            assert "FINISHED" in waited["output"], waited
            assert marker.read_text() == "once\n"
            assert producer.get(result["session_id"]) is session
        finally:
            producer.kill_all(source="test")
