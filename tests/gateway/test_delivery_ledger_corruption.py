import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger as ledger
from gateway.config import Platform
from gateway.run_startup import GatewayStartupMixin


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, '_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setattr(ledger, 'ledger_enabled', lambda: True)


def record(oid, orphan=True):
    ledger.record_obligation(obligation_id=oid, session_key='route:' + oid, platform='slack',
        chat_id='C1', thread_id=None, content='accepted ' + oid)
    if orphan:
        with ledger._connect() as conn:
            conn.execute('UPDATE delivery_obligations SET owner_pid=?,owner_started_at=1 WHERE obligation_id=?',
                (999999999, oid))


def row(oid):
    with ledger._connect() as conn:
        return conn.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone()


@pytest.mark.parametrize('field,value', [('attempts', 'bad'), ('attempts', -1), ('created_at', 'bad'),
    ('created_at', float('inf')), ('updated_at', 'bad'), ('owner_pid', 'bad'), ('owner_started_at', 'bad')])
def test_startup_claim_skips_bad_scalar_without_losing_good_sibling(monkeypatch, field, value):
    record('broken')
    record('good')
    record('live', orphan=False)
    with ledger._connect() as conn:
        statements = {
            'attempts': 'UPDATE delivery_obligations SET attempts=? WHERE obligation_id=?',
            'created_at': 'UPDATE delivery_obligations SET created_at=? WHERE obligation_id=?',
            'updated_at': 'UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?',
            'owner_pid': 'UPDATE delivery_obligations SET owner_pid=? WHERE obligation_id=?',
            'owner_started_at': 'UPDATE delivery_obligations SET owner_started_at=? WHERE obligation_id=?',
        }
        conn.execute(statements[field], (value, 'broken'))
    damaged = row('broken')
    cleared = AsyncMock()
    runner = GatewayStartupMixin()
    runner.adapters = {Platform.SLACK: object()}
    runner._clear_resume_pending_for_claimed_obligations = cleared
    claimed = asyncio.run(GatewayStartupMixin._claim_pending_obligations(runner))
    assert [entry['obligation_id'] for entry in claimed] == ['good']
    cleared.assert_awaited_once_with(claimed)
    assert asyncio.run(GatewayStartupMixin._claim_pending_obligations(runner)) == []
    assert row('broken') == damaged
    assert row('live')[6] == 'pending'


@pytest.mark.parametrize('operation', ['sweep', 'timer'])
def test_runtime_retry_and_timer_skip_bad_scalar_sibling(operation):
    record('broken', orphan=False)
    record('good', orphan=False)
    for oid in ['broken', 'good']:
        ledger.mark_failed(oid, '503 temporarily unavailable')
    with ledger._connect() as conn:
        conn.execute('UPDATE delivery_obligations SET attempts=?,updated_at=? WHERE obligation_id=?', ('bad', 1000.0, 'broken'))
        conn.execute('UPDATE delivery_obligations SET attempts=0,updated_at=1000.0 WHERE obligation_id=?', ('good',))
    damaged = row('broken')
    if operation == 'sweep':
        assert [r['obligation_id'] for r in ledger.sweep_failed_for_runtime('slack', now=1031.0)] == ['good']
    else:
        assert ledger.pending_retries(now=1001.0) == [{'platform':'slack', 'profile':'default', 'not_before':1030.0}]
    assert row('broken') == damaged
