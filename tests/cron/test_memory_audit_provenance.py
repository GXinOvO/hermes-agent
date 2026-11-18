import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from persistence_assembly import open_persistence, profile_scope, store_location
from persistence.conversation.sessions import NewSession
from persistence.conversation.messages import NewMessage
from persistence.sqlite.conversation._store.messages import append_messages_batch as _append_batch
from tests.fixtures.storage_holder import raw_holder


CANDIDATE = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo('Asia/Shanghai')
START = dt.datetime(2026, 11, 12, 3, 15, tzinfo=ZONE)
END = dt.datetime(2026, 11, 13, 3, 15, tzinfo=ZONE)


@pytest.fixture
def collector(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = CANDIDATE / 'scripts' / 'builtin-memory-audit-input.py'
    spec = importlib.util.spec_from_file_location('audit_provenance_test_collector', script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, home


def append(db, session, content, uid):
    _append_batch(raw_holder(db), session, [{'role': 'user', 'content': content,
                                        'timestamp': START.timestamp() + 60, 'message_uid': uid}])


def test_legacy_interactive_row_without_origin_is_uncertain(collector):
    module, home = collector
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession('desktop', source='desktop'))
        append(db, 'desktop', 'Unmarked message could be a background event.', 'legacy-1')
    finally:
        db.close()
    result = module.collect_history(home / 'state.db', START, END)
    assert result['complete_within_declared_scope'] is False, result
    assert result['uncertain_source_count'] == 1, result
    assert result['messages'] == [], result


@pytest.mark.parametrize('created_source', ['desktop', None, 'unknown'])
def test_local_session_rows_are_not_silently_excluded(collector, created_source):
    module, home = collector
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession('visible', source='desktop'))
        db.sessions.create(NewSession('local', source='local'))
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET created_source=? WHERE id=?', (created_source, 'local')))
        append(db, 'visible', 'Unmarked ordinary desktop row.', 'visible-1')
        append(db, 'local', 'Unmarked local row may be a human turn.', 'local-1')
    finally:
        db.close()
    result = module.collect_history(home / 'state.db', START, END)
    assert result['raw_user_records'] == 2, result
    assert result['uncertain_source_count'] == 2, result
    assert result['complete_within_declared_scope'] is False, result
    assert result['messages'] == [], result


def test_out_of_scope_cron_does_not_erase_interactive_unknown(collector):
    module, home = collector
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession('visible', source='desktop'))
        db.sessions.create(NewSession('automation', source='cron'))
        append(db, 'visible', 'Unmarked interactive row.', 'visible-1')
        append(db, 'automation', 'Synthetic cron machinery.', 'cron-1')
    finally:
        db.close()
    result = module.collect_history(home / 'state.db', START, END)
    assert result['uncertain_source_count'] == 1, result
    assert result['complete_within_declared_scope'] is False, result
    assert all('cron machinery' not in item['text'] for item in result['messages'])


def test_malformed_admission_principal_is_uncertain_not_collection_failure(collector):
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch

    module, home = collector
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession('malformed', source='desktop'))
        epoch = begin_runtime_epoch(db, instance_id='malformed-fixture')
        receipt = admit_session_input(
            db, epoch=epoch, principal_id='human:fixture', session_id='malformed',
            request_id='malformed-request', payload={'text': 'malformed principal input'})
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            'UPDATE session_admissions SET principal_id=? WHERE admission_id=?',
            ('', receipt['admission_id'])))
        db.messages.append(
            'malformed', NewMessage(role='user', content='malformed principal input',
            timestamp=START.timestamp() + 60, message_uid='malformed-uid'))
    finally:
        db.close()
    result = module.collect_history(home / 'state.db', START, END)
    assert result['complete_within_declared_scope'] is False, result
    assert result['uncertain_source_count'] == 1, result
    assert result['uncertain_sources'][0]['reason'] == 'invalid_admission_principal', result


def test_compression_child_restatement_uses_parent_witness(collector):
    from agent.context_compressor import _INFLIGHT_TASK_REPLAY_HEADER
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch

    module, home = collector
    db = open_persistence(profile_scope(home=home))
    request = 'Continue the unfinished deployment investigation.'
    replay = _INFLIGHT_TASK_REPLAY_HEADER + '\n' + request
    try:
        db.sessions.create(NewSession('compression-root', source='desktop'))
        db.messages.append(
            'compression-root', NewMessage(role='user', content=request,
                                           timestamp=START.timestamp() - 60,
                                           message_uid='compression-uid'))
        db.sessions.end('compression-root', 'compression')
        db.sessions.create(NewSession('compression-child', source='desktop', parent_session_id='compression-root'))
        epoch = begin_runtime_epoch(db, instance_id='compression-fixture')
        admit_session_input(
            db, epoch=epoch, principal_id='human:fixture', session_id='compression-root',
            request_id='compression-replay', payload={'text': replay})
        from agent.message_metadata import INFLIGHT_TASK_RESTATEMENT
        db.messages.append(
            'compression-child', NewMessage(role='user', content=replay,
                                            timestamp=START.timestamp() + 60,
                                            message_uid='compression-uid',
                                            display_metadata={'submission_id': 'compression-replay',
                                                              INFLIGHT_TASK_RESTATEMENT: True}))
    finally:
        db.close()
    result = module.collect_history(home / 'state.db', START, END)
    assert result['complete_within_declared_scope'] is True, result
    assert result['excluded_derived_records'] == 1, result
    assert result['uncertain_source_count'] == 0, result
    assert result['messages'] == [], result


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['admitted', 'no_receipt', 'wrong_text', 'wrong_session',
                                  'hidden', 'kanban', 'authorized_operator'])
async def test_gateway_admission_does_not_certify_unbound_or_machine_input(
        collector, tmp_path, monkeypatch, case):
    from gateway.session_contract import SessionActor, Submission
    from gateway.session_display import admission_submission, submission_turn_scope
    from hermes_state_runtime import claim_session_input, settle_session_input
    from tests.gateway import test_session_policy_verbs as gateway_fixtures

    module, _home = collector
    store, db, authority, owner, ref = gateway_fixtures._owner(tmp_path, monkeypatch, None)
    monkeypatch.setattr(authority, '_schedule', lambda *_: None)
    text = 'A genuinely submitted synthetic request.'
    kwargs = {'_kanban_card': {'board': 'synthetic', 'task_id': 'task', 'run_id': 'run'}} if case == 'kanban' else {}
    actor = owner.actor
    if case == 'authorized_operator':
        actor = SessionActor('synthetic-operator', authority.profile_id,
                             frozenset({'session:submit', 'session:operator'}), 'test-operator')
    try:
        receipt = await authority.submit(actor, Submission('test-request', ref, {'text': text}, 'queue'), **kwargs)
        row = claim_session_input(db, epoch=authority.epoch, session_id=ref.session_id)
        assert row and row['admission_id'] == receipt.admission_id
        with submission_turn_scope(receipt.admission_id, 'test-request'):
            metadata = admission_submission(SimpleNamespace(message_id=receipt.admission_id))
        assert metadata == {'submission_id': 'test-request'}
        if case == 'no_receipt':
            metadata = {'submission_id': 'unmatched-request'}
        content = 'Different synthetic text.' if case == 'wrong_text' else text
        session = ref.session_id
        if case == 'wrong_session':
            session = 'other-session'
            db.sessions.create(NewSession(session, source='desktop'))
        from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
        prepared = SimpleNamespace(
            persist_user_message=None, message_text=content,
            persist_user_timestamp=None,
            persist_user_display_kind='hidden' if case == 'hidden' else None,
            persistence_owner=None)
        event = SimpleNamespace(message_id=receipt.admission_id, internal=False, metadata={}, text=content)
        with submission_turn_scope(receipt.admission_id, 'test-request'):
            message = GatewayTurnPersistenceMixin._hmwa_user_transcript_entry(
                event, prepared, START.timestamp() + 60)
        if case == 'no_receipt':
            message['display_metadata'] = {'submission_id': 'unmatched-request'}
        _append_batch(raw_holder(db), session, [message])
        settle_session_input(db, epoch=authority.epoch, admission_id=receipt.admission_id,
                             generation=row['generation'], outcome='completed')
        result = module.collect_history(store_location(db.scope), START, END)
        if case in ('admitted', 'authorized_operator'):
            assert result['complete_within_declared_scope'] is True, result
            assert [item['text'] for item in result['messages']] == [text]
        else:
            assert result['complete_within_declared_scope'] is False, result
            assert result['uncertain_source_count'] >= 1, result
            assert result['messages'] == [], result
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_gateway_rejects_unauthorized_principal_before_admission(collector, tmp_path, monkeypatch):
    from gateway.session_contract import SessionActor, Submission
    from persistence.core.errors import AuthorityRefusal
    from tests.gateway import test_session_policy_verbs as gateway_fixtures

    module, _home = collector
    store, db, authority, owner, ref = gateway_fixtures._owner(tmp_path, monkeypatch, None)
    actor = SessionActor('unauthorized', authority.profile_id, frozenset(), 'test-unauthorized')
    try:
        with pytest.raises(AuthorityRefusal, match='permission_denied'):
            await authority.submit(actor, Submission('rejected-request', ref, {'text': 'must not commit'}, 'queue'))
        from tests.fixtures.storage_holder import raw_holder
        with raw_holder(db)._read_ctx() as conn:
            assert not conn.execute(
                'SELECT * FROM session_admissions WHERE request_id=?', ('rejected-request',)).fetchall()
        assert module.collect_history(store_location(db.scope), START, END)['raw_user_records'] == 0
    finally:
        await owner.close()
        store.close_all_db_handles()
