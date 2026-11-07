import json

import pytest

from cron import delivery_queue as queue
from tests.cron.test_strict_thread_lifecycle import peer, process, setup
from tests.cron.test_strict_mixed_fanout import enqueue, owner, peer as fanout_peer


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['control', 'content', 'job_json', 'home', 'for_failure', 'execution_id'])
async def test_terminal_compaction_preserves_row_provenance(tmp_path, monkeypatch, damage):
    job = setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with peer() as (url, events):
        await process(tmp_path, 'root_ack', url)
    assert len(events) == 1
    from cron.strict_thread_delivery import digest, recoverable
    row = queue.get_status(job['execution_id'])
    assert recoverable(row)
    saved = json.loads(row['thread_json'])
    changed_job = dict(json.loads(row['job_json']), execution_id='foreign-execution', id='foreign-job')
    if damage == 'home':
        saved['binding']['home'] += '-foreign'
        saved['binding_sha256'] = digest(saved['binding'])
        saved['items'][0]['receipt_sha256'] = digest({'binding': saved['binding_sha256'],
            'target': saved['binding']['targets'][0], 'ack': saved['items'][0]['receipt']})
    changes = {
        'content': ('content', 'different retained result'),
        'job_json': ('job_json', json.dumps(changed_job)),
        'home': ('thread_json', json.dumps(saved)),
        'for_failure': ('for_failure', 1),
        'execution_id': ('execution_id', 'foreign-row-id'),
    }
    with queue._transaction() as conn:
        conn.execute("UPDATE deliveries SET status='unknown' WHERE execution_id=?", (job['execution_id'],))
        if damage in changes:
            column, value = changes[damage]
            conn.execute('UPDATE deliveries SET ' + column + '=? WHERE execution_id=?', (value, job['execution_id']))
    execution = 'foreign-row-id' if damage == 'execution_id' else job['execution_id']
    before = queue.get_status(execution)
    if damage in {'content', 'job_json', 'home', 'for_failure'}:
        with pytest.raises(ValueError, match='provenance mismatch'):
            recoverable(before)
    queue.enqueue('healthy-sibling', {'id': 'healthy-sibling', 'deliver': 'local'}, 'healthy sibling')
    assert queue.drain(lambda *args: None) == 1
    after = queue.get_status(execution)
    assert queue.get_status('healthy-sibling')['status'] == 'delivered'
    assert (tmp_path / 'script-calls').read_text().splitlines() == ['executed']
    if damage == 'control':
        assert json.loads(after['thread_receipts_json'])['items'][0]['root'] == '1700000000.000001'
        assert after['thread_json'] is None and after['content'] == ''
    else:
        assert after == before, 'provenance drift must preserve original terminal row'
    monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
    with queue._transaction() as conn:
        queue._prune_terminal_unlocked(conn)
    if damage != 'control':
        assert queue.get_status(execution) == before
    assert queue.get_status('healthy-sibling')['status'] == 'delivered'


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['control', 'content', 'job_json', 'for_failure', 'execution_id'])
async def test_v2_terminal_provenance_keeps_bad_raw_and_valid_prefix(tmp_path, monkeypatch, damage):
    enqueue(tmp_path, monkeypatch, deliver='slack:C12345678:1700000777.000001,origin,telegram:555', cold_telegram=True)
    async with fanout_peer() as (url, events):
        await owner(tmp_path, url, 'after:1:root_acked')
    row = queue.get_status('fanout-exec')
    from cron.strict_thread_delivery import recoverable
    assert recoverable(row)
    changed = dict(json.loads(row['job_json']), execution_id='foreign-execution')
    mutations = {'content': ('content', 'changed result'), 'job_json': ('job_json', json.dumps(changed)),
                 'for_failure': ('for_failure', 1), 'execution_id': ('execution_id', 'foreign-row-id')}
    with queue._transaction() as conn:
        conn.execute("UPDATE deliveries SET status='unknown' WHERE execution_id='fanout-exec'")
        if damage in mutations:
            column, value = mutations[damage]
            conn.execute('UPDATE deliveries SET ' + column + "=? WHERE execution_id='fanout-exec'", (value,))
    execution = 'foreign-row-id' if damage == 'execution_id' else 'fanout-exec'
    before = queue.get_status(execution)
    queue.enqueue('v2-sibling', {'id': 'sibling', 'deliver': 'local'}, 'sibling')
    assert queue.drain(lambda *args: None) == 1
    after = queue.get_status(execution)
    if damage == 'control':
        audit = json.loads(after['thread_receipts_json'])
        assert audit['version'] == 2 and audit['items'][0]['stage'] == 'done'
        assert audit['items'][1]['root'] == '1700000000.000002'
        assert audit['items'][1]['stage'] == 'root_acked'
        assert after['content'] == '' and after['thread_json'] is None
    else:
        assert after == before
    assert queue.get_status('v2-sibling')['status'] == 'delivered' and len(events) == 2
