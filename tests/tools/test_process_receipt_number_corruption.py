import json
import queue

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_constants import get_hermes_home
from tests.tools import test_process_result_retention as native
from tools import process_registry_results as receipts

native_terminal = native.native_terminal


@pytest.mark.platforms('linux', 'macos')
@pytest.mark.parametrize('started_at', [10**400, -(10**400)], ids=['huge-positive', 'huge-negative'])
def test_oversized_receipt_number_does_not_block_native_finish_or_fresh_reader(native_terminal, tmp_path, started_at):
    damaged_session = native.launch_native(native_terminal, tmp_path, 0)[0]
    directory = get_hermes_home() / 'logs' / 'process-results'
    damaged = directory / (damaged_session.id + '.json')
    record = json.loads(damaged.read_text())
    fingerprint = record['source_fingerprint'], record['result_fingerprint'], record['consumption_fingerprint']
    record['started_at'] = started_at
    raw = json.dumps(record).encode()
    damaged.write_bytes(raw)
    assert native_terminal.completion_queue.get_nowait()['session_id'] == damaged_session.id
    good, gate = native.launch_native(native_terminal, tmp_path, 1, worker=True)
    gate.touch()
    try:
        assert good._completion_event.wait(10), 'bad sibling prevented actual native completion publication'
        good._reader_thread.join(10)
        assert not good._reader_thread.is_alive()
        assert good.result_durability == 'persisted'
        event = native_terminal.completion_queue.get_nowait()
        assert event['session_id'] == good.id and event['exit_code'] == 0
        with pytest.raises(queue.Empty):
            native_terminal.completion_queue.get_nowait()
        assert good.id not in native_terminal._running
        original = json.loads((directory / (good.id + '.json')).read_text())
        with scoped_current_session_id('receipt-owner'):
            assert set(receipts.load_completed_results()) == {good.id}
        marker = (tmp_path / 'executions').read_bytes()
        recovered = native.read_in_fresh_owner(tmp_path, [good.id])
        assert {r['session_id'] for r in recovered['listed']['processes']} == {good.id}
        assert recovered['results'][0]['status'] == 'exited'
        assert recovered['results'][0]['exit_code'] == 0
        assert 'RETAINED_1' in recovered['results'][0]['output']
        acknowledged = recovered['acks'][0]
        assert acknowledged['source_fingerprint'] == original['source_fingerprint']
        assert acknowledged['result_fingerprint'] == original['result_fingerprint']
        assert acknowledged['consumption_fingerprint'] != original['consumption_fingerprint']
        assert acknowledged['completion_consumed'] is True
        again = native.read_in_fresh_owner(tmp_path, [good.id])
        assert again['acks'][0]['consumption_fingerprint'] == acknowledged['consumption_fingerprint']
        assert recovered['running'] == again['running'] == []
        assert recovered['queued'] == again['queued'] == 0
        assert damaged.read_bytes() == raw
        unchanged = json.loads(raw)
        assert (unchanged['source_fingerprint'], unchanged['result_fingerprint'], unchanged['consumption_fingerprint']) == fingerprint
        assert (tmp_path / 'executions').read_bytes() == marker == b'0\n1\n'
    finally:
        if good._reader_thread.is_alive():
            good._reader_thread.join(10)
        if good.process is not None:
            for stream in [good.process.stdout, good.process.stderr, good.process.stdin]:
                if stream is not None:
                    stream.close()


@pytest.mark.platforms('linux', 'macos')
def test_live_wait_reports_durability_failure_without_rewriting_damaged_receipt(native_terminal, tmp_path):
    session = native.launch_native(native_terminal, tmp_path, 0)[0]
    path = get_hermes_home() / 'logs' / 'process-results' / (session.id + '.json')
    record = json.loads(path.read_text())
    record['started_at'] = 10**400
    raw = json.dumps(record).encode()
    path.write_bytes(raw)
    with scoped_current_session_id('receipt-owner'):
        result = native_terminal.wait(session.id, timeout=1)
    assert result['status'] == 'exited' and result['exit_code'] == 0
    assert result['result_durability'] == 'failed'
    assert 'RETAINED_0' in result['output']
    assert path.read_bytes() == raw
    assert (tmp_path / 'executions').read_bytes() == b'0\n'
