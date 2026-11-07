"""Damaged checkpoint entries do not block native startup recovery of siblings."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import psutil
import pytest

from gateway.session_context import scoped_current_session_id
from hermes_constants import get_hermes_home
from tests.tools import test_process_result_retention as native
from tests.tools.test_terminal_result_recovery import _runner_stub
from tools import process_registry as pr

native_terminal = native.native_terminal


def _worker_handoff(tmp_path):
    script = textwrap.dedent('''\
        import json, os, pathlib, sys
        import pytest
        from agent import runtime_session_store
        from tests.tools import test_process_result_retention as native
        with pytest.MonkeyPatch.context() as patches:
            producer = native.native_terminal.__wrapped__(pathlib.Path(sys.argv[1]), patches)
            patches.setattr(runtime_session_store, '_worker_process', True)
            session, gate = native.launch_native(producer, pathlib.Path(sys.argv[1]), 1, notify=False, worker=True)
            producer._write_checkpoint()
            print(json.dumps({'id': session.id, 'pid': session.pid,
                              'host_start_time': session.host_start_time,
                              'output_log': session.output_log, 'gate': str(gate)}), flush=True)
            os._exit(0)
    ''')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
           'HERMES_HOME': str(get_hermes_home())}
    run = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=tmp_path,
                         env=env, stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=30, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    handoff = json.loads(run.stdout)
    assert handoff['output_log'] and handoff['host_start_time']
    assert psutil.pid_exists(handoff['pid'])
    return handoff


@pytest.mark.asyncio
@pytest.mark.platforms('linux', 'macos')
@pytest.mark.parametrize('damage', ['null', 'missing_id', 'bad_pid', 'bad_interval', 'bad_patterns'])
async def test_startup_isolates_bad_checkpoint_and_recovers_worker_and_completed(
    native_terminal, tmp_path, monkeypatch, damage,
):
    from hermes_state import SessionDB

    db = SessionDB()
    db.create_session('receipt-owner', 'cli')
    completed = native.launch_native(native_terminal, tmp_path, 0)[0]
    checkpoint = get_hermes_home() / 'processes.json'
    handoff = _worker_handoff(tmp_path)
    original = json.loads(checkpoint.read_text())
    valid = next(entry for entry in original if entry['session_id'] == handoff['id'])
    missing_id = dict(valid)
    missing_id.pop('session_id')
    broken = {
        'null': None,
        'missing_id': missing_id,
        'bad_pid': {**valid, 'session_id': 'proc_bad', 'pid': 'bad'},
        'bad_interval': {**valid, 'session_id': 'proc_bad', 'watcher_interval': []},
        'bad_patterns': {**valid, 'session_id': 'proc_bad', 'watch_patterns': [None]},
    }[damage]
    raw = (' \n' + json.dumps([broken, valid], indent=3) + '\n').encode()
    checkpoint.write_bytes(raw)
    reader = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', reader)
    stopped = []
    monkeypatch.setattr(pr, '_stop_systemd_unit', lambda name: stopped.append(name) or True)
    def forbidden(*a, **kw):
        pytest.fail('startup attempted execution or kill')
    monkeypatch.setattr(reader, 'spawn_local', forbidden)
    monkeypatch.setattr(reader, 'spawn_via_env', forbidden)
    monkeypatch.setattr(reader, '_terminate_host_pid', forbidden)
    try:
        await _runner_stub()._start_recover_previous_run()
        assert completed.id in reader._finished, 'bad checkpoint entry blocked completed receipt startup recovery'
        assert handoff['id'] in reader._running, 'valid worker handoff was not adopted'
        adopted = reader._running[handoff['id']]
        assert adopted.pid == handoff['pid'] and adopted.host_start_time == handoff['host_start_time']
        assert adopted.detached and adopted.process is None and adopted.output_log == handoff['output_log']
        assert psutil.pid_exists(handoff['pid']) and stopped == []
        assert reader.poll(handoff['id'])['status'] == 'running'
        assert (tmp_path / 'executions').read_text() == '0\n'
        # The exact damaged input must remain reconcilable after checkpoint rewrites and another startup.
        reader._write_checkpoint()
        await _runner_stub()._start_recover_previous_run()
        assert any(p.read_bytes() == raw for p in checkpoint.parent.glob('processes.json.corrupt-*'))
        assert len([e for e in reader.completion_queue.queue if e['session_id'] == completed.id]) == 1
        Path(handoff['gate']).touch()
        assert await asyncio.to_thread(adopted._completion_event.wait, 20)
        adopted._reader_thread.join(10)
        assert not adopted._reader_thread.is_alive()
        with scoped_current_session_id('receipt-owner'):
            for sid, expected in [(completed.id, 'RETAINED_0'), (handoff['id'], 'RETAINED_1')]:
                result = reader.wait(sid, timeout=1)
                assert result['status'] == 'exited' and result['exit_code'] == 0
                assert expected in result['output']
        assert (tmp_path / 'executions').read_text() == '0\n1\n'
        assert reader.recover_completed_results() == 0
        assert stopped == []
        print(json.dumps({'damage': damage, 'completed_recovered': completed.id,
                          'handoff_recovered': handoff['id'], 'executions': 2,
                          'raw_preserved': True, 'startup_repeated_without_reexecution': True}))
    finally:
        Path(handoff['gate']).touch()
        if handoff['id'] in reader._running:
            await asyncio.to_thread(reader._running[handoff['id']]._completion_event.wait, 20)
        try:
            process = psutil.Process(handoff['pid'])
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        except psutil.NoSuchProcess:
            pass
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [b'null', b'{}', b'{'])
async def test_bad_checkpoint_document_preserves_raw_and_completed_recovery(native_terminal, tmp_path, monkeypatch, damage):
    from hermes_state import SessionDB

    db = SessionDB()
    db.create_session('receipt-owner', 'cli')
    try:
        completed = native.launch_native(native_terminal, tmp_path, 0)[0]
        checkpoint = get_hermes_home() / 'processes.json'
        checkpoint.write_bytes(damage)
        reader = pr.ProcessRegistry()
        monkeypatch.setattr(pr, 'process_registry', reader)
        await _runner_stub()._start_recover_previous_run()
        assert completed.id in reader._finished
        reader._write_checkpoint()
        assert any(p.read_bytes() == damage for p in checkpoint.parent.glob('processes.json.corrupt-*'))
        assert (tmp_path / 'executions').read_text() == '0\n'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_corruption_archive_keeps_original_but_recovers_completed(native_terminal, tmp_path, monkeypatch):
    import utils
    from hermes_state import SessionDB

    db = SessionDB()
    db.create_session('receipt-owner', 'cli')
    try:
        completed = native.launch_native(native_terminal, tmp_path, 0)[0]
        checkpoint = get_hermes_home() / 'processes.json'
        damaged = b'[null]'
        checkpoint.write_bytes(damaged)
        def fail(*args, **kwargs):
            raise OSError('injected archive write failure')
        monkeypatch.setattr(utils, 'atomic_write_bytes', fail)
        reader = pr.ProcessRegistry()
        monkeypatch.setattr(pr, 'process_registry', reader)
        await _runner_stub()._start_recover_previous_run()
        assert completed.id in reader._finished
        reader._write_checkpoint()
        assert checkpoint.read_bytes() == damaged
        assert (tmp_path / 'executions').read_text() == '0\n'
    finally:
        db.close()
