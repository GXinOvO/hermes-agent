import json

import pytest

from cron import delivery_queue as queue
from tests.cron.test_strict_mixed_fanout import enqueue, owner, peer


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,status,initial', [
    ('after:0:done', 'delivered', 1),
    ('after:1:create_claimed', 'unknown', 1),
    ('after:1:root_acked', 'delivered', 2)])
async def test_existing_thread_prefix_before_new_root(tmp_path, monkeypatch, stop, status, initial):
    enqueue(tmp_path, monkeypatch, deliver='slack:C12345678:1700000777.000001,origin,telegram:555', cold_telegram=True)
    async with peer() as (url, events):
        await owner(tmp_path, url, stop)
        saved = json.loads(queue.get_status('fanout-exec')['thread_json'])
        assert saved['items'][0]['stage'] == 'done' and len(events) == initial
        one = await owner(tmp_path, url)
        wire = list(events)
        two = await owner(tmp_path, url)
    assert one['row']['status'] == two['row']['status'] == status
    assert events == wire
    assert len(events) == (4 if status == 'delivered' else initial)
    assert events[0]['data']['channel'] == 'C12345678'
    if status == 'delivered':
        assert [e['data'].get('thread_ts') for e in events[:3]] == ['1700000777.000001', None, '1700000000.000002']
        assert events[-1]['path'].endswith('/sendMessage')


@pytest.mark.asyncio
@pytest.mark.parametrize('stop,root,stage,status', [
    ('none', '1700000000.000002', 'done', 'delivered'),
    ('after:1:create_claimed', None, 'create_claimed', 'unknown'),
    ('after:1:body_claimed', '1700000000.000002', 'body_claimed', 'unknown')])
async def test_v2_terminal_audit_keeps_root_and_nonthread_prefix(tmp_path, monkeypatch, stop, root, stage, status):
    job = enqueue(tmp_path, monkeypatch,
        deliver='slack:C12345678:1700000777.000001,origin,telegram:555', cold_telegram=True)
    async with peer() as (url, events):
        if stop != 'none':
            await owner(tmp_path, url, stop)
        await owner(tmp_path, url)
        row = queue.get_status('fanout-exec')
        assert row['status'] == status
        audit = json.loads(row.get('thread_receipts_json') or '{}')
        assert audit.get('version') == 2, 'valid v2 row must compact instead of retaining body indefinitely'
        assert len(audit['items']) == 3
        first, thread, last = audit['items']
        assert first['kind'] == 'slack' and first['stage'] == 'done'
        assert 'root' not in first and 'root' not in last
        assert thread['kind'] == 'thread' and thread['root'] == root and thread['stage'] == stage
        assert thread['workspace'] == 'TLOCAL' and thread['parent'] == 'CLOCAL'
        assert last['kind'] == 'standalone' and last['stage'] == ('done' if status == 'delivered' else 'initial')
        assert all(len(item['route_sha256']) == 64 for item in audit['items'])
        assert row['content'] == '' and row['job_json'] == '{}' and row['thread_json'] is None
        assert 'sealed mixed result' not in row['thread_receipts_json']
        assert 'inert' not in row['thread_receipts_json']
        wire = list(events)
        monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
        with queue._transaction() as conn:
            queue._prune_terminal_unlocked(conn)
        compact = queue.get_status('fanout-exec')
        assert compact['thread_receipts_json'] == row['thread_receipts_json']
        assert queue.enqueue('fanout-exec', job, 'cannot replay')['status'] == status
        await owner(tmp_path, url)
    assert events == wire
