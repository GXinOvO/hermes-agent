import asyncio
import json
import sqlite3

import pytest

from cron import delivery_queue as queue
from tests.cron.test_strict_thread_lifecycle import peer, process, setup


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['none', 'root_ack', 'body_claim'])
async def test_terminal_receipt_keeps_exact_root_without_payload(tmp_path, monkeypatch, mode):
    job = setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with peer() as (url, events):
        if mode != 'none':
            await process(tmp_path, mode, url)
        one = await process(tmp_path, 'none', url)
        assert one['script_calls'] == ['executed']
        row = queue.get_status(job['execution_id'])
        assert row['status'] == ('unknown' if mode == 'body_claim' else 'delivered')
        audit = json.loads(row.get('thread_receipts_json') or '{}')
        assert audit.get('version') == 1, 'terminal pruning erased the exact root receipt'
        assert audit['items'][0]['root'] == '1700000000.000001'
        assert audit['items'][0]['workspace'] == 'TLOCAL' and audit['items'][0]['parent'] == 'CLOCAL'
        assert audit['items'][0]['stage'] == ('body_claimed' if mode == 'body_claim' else 'done')
        assert len(audit['binding_sha256']) == 64
        assert row['job_json'] == '{}' and row['content'] == '' and row['media_json'] is None and row['thread_json'] is None
        assert 'saved real producer brief' not in row['thread_receipts_json']
        assert 'inert-local-thread' not in row['thread_receipts_json']
        wire = list(events)
        monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
        with queue._transaction() as conn:
            queue._prune_terminal_unlocked(conn)
        tombstone = queue.get_status(job['execution_id'])
        assert tombstone['thread_receipts_json'] == row['thread_receipts_json']
        assert queue.enqueue(job['execution_id'], job, 'never resend')['status'] == row['status']
        await process(tmp_path, 'none', url)
    assert events == wire


def test_pre_thread_schema_migrates_atomically_and_retains_legacy_rows(tmp_path, monkeypatch):
    path = tmp_path / 'cron/deliveries.db'
    path.parent.mkdir()
    monkeypatch.setattr(queue, 'DELIVERY_DB', path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE deliveries (
            execution_id TEXT PRIMARY KEY, job_json TEXT NOT NULL, content TEXT NOT NULL,
            for_failure INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN
            ('pending','delivering','delivered','failed','unknown','suppressed')),
            owner_process_id TEXT, owner_pid INTEGER, owner_started_at INTEGER,
            created_at TEXT NOT NULL, finished_at TEXT, error TEXT, media_json TEXT)""")
        conn.execute("INSERT INTO deliveries(execution_id,job_json,content,status,created_at) VALUES ('legacy','{}','kept','pending','2026-01-01')")
    row = queue.get_status('legacy')
    assert row['content'] == 'kept' and row['status'] == 'pending'
    assert row['thread_json'] is None
    assert row.get('thread_receipts_json') is None
    assert queue.get_status('legacy') == row


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['json', 'envelope', 'binding', 'version', 'targets', 'item', 'message', 'receipt'])
async def test_bad_terminal_thread_receipt_keeps_raw_and_valid_sibling(tmp_path, monkeypatch, damage):
    from cron.strict_thread_delivery import digest
    job = setup(tmp_path, monkeypatch)
    await process(tmp_path, 'produce')
    async with peer() as (url, events):
        await process(tmp_path, 'root_ack', url)
        row = queue.get_status(job['execution_id'])
        saved = json.loads(row['thread_json'])
        changes = {
            'binding': lambda: saved.update(binding=[]),
            'version': lambda: saved['binding'].update(version=10 ** 400),
            'targets': lambda: saved['binding'].update(targets=['bad']),
            'item': lambda: saved.update(items=[None]),
            'message': lambda: saved['items'][0]['receipt'].update(message=[]),
            'receipt': lambda: saved['items'][0].update(receipt=True),
        }
        if damage in changes:
            changes[damage]()
        if damage in {'version', 'targets'}:
            saved['binding_sha256'] = digest(saved['binding'])
        raw = '{' if damage == 'json' else '[]' if damage == 'envelope' else json.dumps(saved)
        with queue._transaction() as conn:
            conn.execute("UPDATE deliveries SET thread_json=?, status='unknown' WHERE execution_id=?", (raw, job['execution_id']))
        before = queue.get_status(job['execution_id'])
        queue.enqueue('sibling', {'id': 'sibling', 'deliver': 'local'}, 'healthy sibling')
        assert queue.drain(lambda *args: None) == 1
        assert queue.get_status('sibling')['status'] == 'delivered'
        monkeypatch.setattr(queue, 'MAX_TERMINAL_DELIVERIES', 0)
        with queue._transaction() as conn:
            queue._prune_terminal_unlocked(conn)
        assert queue.get_status(job['execution_id']) == before
        assert queue.get_status('sibling')['status'] == 'delivered'
        await process(tmp_path, 'none', url)
    assert len(events) == 1
