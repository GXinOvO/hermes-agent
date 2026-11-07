import time

import pytest

from gateway import delivery_ledger as dl


@pytest.mark.parametrize('state', ['unknown', 'delivered'])
def test_historical_crash_reply_is_quarantined_before_sweep_and_never_reopened(tmp_path, monkeypatch, state):
    monkeypatch.setattr(dl, '_db_path', lambda:tmp_path / 'outbound.db')
    values = {'obligation_id':'old-reply', 'session_key':'route', 'platform':'slack', 'chat_id':'original',
        'thread_id':'thread', 'content':'saved response', 'since':time.time() - 5, 'adapter_profile':'default'}
    dl.record_crash_left_reply(**values)
    with dl._connect() as conn:
        current = conn.execute('SELECT state,attempts,content,last_error,recovery_policy,provenance_json '
                               'FROM delivery_obligations WHERE obligation_id=?', ('old-reply',)).fetchone()
    assert current == ('unknown', 0, 'saved response', 'legacy_delivery_unproven: reconciliation required', None, None)
    if state == 'delivered':
        dl.mark_delivered('old-reply')
    for _ in range(2):
        dl.record_crash_left_reply(**values)
        assert dl.sweep_recoverable() == []
        assert dl.sweep_failed_for_runtime('slack') == []
    with dl._connect() as conn:
        current = conn.execute('SELECT state,attempts,content FROM delivery_obligations WHERE obligation_id=?',
                               ('old-reply',)).fetchone()
    assert current == (state, 0, 'saved response')


def test_proven_unsent_legacy_obligation_keeps_first_send_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, '_db_path', lambda:tmp_path / 'outbound.db')
    dl.record_obligation(obligation_id='pending', session_key='route', platform='slack', chat_id='original',
        thread_id=None, content='saved response')
    with dl._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET recovery_policy=NULL,provenance_json=NULL,"
                     "owner_pid=NULL,owner_started_at=NULL WHERE obligation_id='pending'")
    rows = dl.sweep_recoverable()
    assert len(rows) == 1 and rows[0]['content'] == 'saved response' and not rows[0]['needs_marker']
