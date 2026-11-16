import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_submit_time_user_row import _desktop_session
from tui_gateway import server


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    sid, key = _desktop_session(monkeypatch, db)
    starts = []
    monkeypatch.setattr(server, '_start_agent_build', lambda *_: None)
    monkeypatch.setattr(server, '_restart_completed_failed_agent_build', lambda *_: False)
    monkeypatch.setattr(server, '_run_after_agent_ready', lambda *_: starts.append(True))
    monkeypatch.setattr('tui_gateway.ws._start_backend_liveness', lambda: None)
    monkeypatch.setattr('tui_gateway.ws._note_dashboard_client_activity', lambda **_: None)
    monkeypatch.setattr(server, 'resolve_skin', dict)
    try:
        yield db, sid, key, server._sessions[sid], starts
    finally:
        server._sessions.pop(sid, None)
        db.close()


def history(db, before):
    path = Path(__file__).resolve().parents[2] / 'scripts/builtin-memory-audit-input.py'
    spec = importlib.util.spec_from_file_location('desktop_input_collector', path)
    assert spec is not None and spec.loader is not None
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    return collector.collect_history(db.db_path, before, dt.datetime.now(dt.UTC))


def admissions(db):
    return [dict(row) for row in db._conn.execute('SELECT * FROM session_admissions ORDER BY seq')]


_UNSET = object()


def desktop_identity():
    return {'provider': 'local', 'user_id': 'desktop-fixture',
            'profile_id': 'fixture', 'instance_id': 'fixture',
            'capabilities': ['session:create'], 'native_bootstrap': True}


async def receive(sid, text, *, operator=True, identity=_UNSET, params=None):
    from tui_gateway.ws import handle_ws
    from starlette.websockets import WebSocketDisconnect

    frames = []
    request = {'jsonrpc': '2.0', 'id': 'prompt', 'method': 'prompt.submit',
               'params': {'session_id': sid, 'text': text, **(params or {})}}

    local_desktop = identity is _UNSET or (
        isinstance(identity, dict) and identity.get('provider') == 'local'
        and identity.get('native_bootstrap') is True)

    class Socket:
        client = SimpleNamespace(host='127.0.0.1', port=12345)
        headers = {}
        scope = {}

        def __init__(self):
            self.sent = 0
            self.auth_identity = {'provider': 'local', 'user_id': 'desktop-fixture',
                                  'profile_id': 'fixture', 'instance_id': 'fixture',
                                  'capabilities': ['session:create'], 'native_bootstrap': True}
            self.local_desktop = local_desktop

        async def accept(self, **_):
            return None

        async def send_text(self, raw):
            frames.append(json.loads(raw))

        async def receive_text(self):
            if self.sent == 0:
                self.sent = 1
                return json.dumps({'jsonrpc': '2.0', 'id': 'capabilities',
                                   'method': 'client.capabilities',
                                   'params': {'server_requests': True, 'desktop_local': self.local_desktop}})
            if self.sent == 1:
                self.sent = 2
                return json.dumps(request)
            raise WebSocketDisconnect()

        async def close(self, **_):
            return None

    if identity is _UNSET:
        identity = desktop_identity()
    await handle_ws(Socket(), auth_identity=identity, operator=operator)
    return next(frame for frame in frames if frame.get('id') == 'prompt')


@pytest.mark.asyncio
async def test_authenticated_desktop_submit_is_collectable_before_the_agent_runs(monkeypatch, desktop):
    from hermes_cli import web_server as web
    from hermes_cli.web_server_chat import _ws_auth_reason
    from starlette.websockets import WebSocket

    db, sid, key, _session, _starts = desktop
    monkeypatch.setattr(web.app.state, 'auth_required', False, raising=False)
    monkeypatch.setattr(web, '_SESSION_TOKEN', 'fixture-session-token')
    ws = WebSocket({'type': 'websocket', 'path': '/api/ws', 'headers': [],
                    'query_string': b'token=fixture-session-token', 'client': ('127.0.0.1', 12345)}, None, None)
    assert _ws_auth_reason(ws)[0] is None
    before = dt.datetime.now(dt.UTC)
    text = '我更喜欢先给结论，再给证据。'
    reply = await receive(sid, text, identity=desktop_identity())
    assert reply['result']['status'] == 'streaming', reply
    stored = db.get_messages_as_conversation(key, include_row_ids=True)
    assert [row['content'] for row in stored] == [text]
    result = history(db, before)
    assert result['complete_within_declared_scope'] is True, result
    assert [row['text'] for row in result['messages']] == [text]
    assert result['uncertain_source_count'] == 0
    row, = admissions(db)
    assert row['status'] == 'terminal' and row['outcome'] == 'recorded'
    assert row['generation'] is None and row['owner_epoch'] is None
    assert json.loads(row['payload_json'])['text'] == text
    assert stored[0]['display_metadata']['submission_id'] == row['request_id']
    assert db.display_message_count(key) == 1
    assert [message['content'] for message in db.get_messages(key, include_compacted=True)] == [text]


@pytest.mark.asyncio
async def test_reused_rpc_id_is_two_distinct_inputs(desktop):
    db, sid, _key, session, _starts = desktop
    before = dt.datetime.now(dt.UTC)
    for text in ('first receive', 'second receive'):
        session['running'] = False
        await receive(sid, text)
    result = history(db, before)
    assert [r['text'] for r in result['messages']] == ['first receive', 'second receive']
    assert result['complete_within_declared_scope'] is True
    rows = admissions(db)
    assert len({r['request_id'] for r in rows}) == len(rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('params,operator,identity', [
    ({}, False, None),
    ({'display_kind': 'hidden'}, True, None),
    ({}, True, {'provider': 'server-internal', 'user_id': 'server-internal'}),
    ({}, True, {'provider': 'local', 'user_id': 'automation:cron:fixture'}),
    ({'auth_identity': {'provider': 'local', 'user_id': 'uid:1'}, 'desktop_local': True}, False, None),
])
async def test_untrusted_or_synthetic_inputs_never_gain_human_receipts(desktop, params, operator, identity):
    db, sid, _key, _session, _starts = desktop
    before = dt.datetime.now(dt.UTC)
    await receive(sid, 'not proven as an interactive input', params=params, operator=operator, identity=identity)
    assert admissions(db) == []
    assert history(db, before)['included_messages'] == 0


@pytest.mark.asyncio
async def test_direct_internal_handler_does_not_inherit_a_logged_in_session(desktop):
    db, sid, _key, session, _starts = desktop
    session['auth_user_id'] = 'uid:fixture'
    before = dt.datetime.now(dt.UTC)
    server.handle_request({'id': 'prompt', 'method': 'prompt.submit',
                           'params': {'session_id': sid, 'text': 'an internal call'}})
    assert admissions(db) == []
    assert history(db, before)['included_messages'] == 0


@pytest.mark.asyncio
async def test_terminal_receipt_does_not_take_over_the_live_gateway_epoch(desktop):
    from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input

    db, sid, key, _session, _starts = desktop
    db.create_session('other', source='gui')
    epoch = begin_runtime_epoch(db, instance_id='the-existing-gateway')
    before = dict(db._conn.execute('SELECT * FROM runtime_epoch').fetchone())
    await receive(sid, 'ordinary local submit')
    assert dict(db._conn.execute('SELECT * FROM runtime_epoch').fetchone()) == before
    assert db.get_session(key)['runtime_generation'] == 0
    assert claim_session_input(db, epoch=epoch, session_id=key) is None
    native = admit_session_input(db, epoch=epoch, principal_id='uid:fixture', session_id='other',
                                 request_id='canonical-next', payload={'text': 'other conversation'})
    assert claim_session_input(db, epoch=epoch, session_id='other')['admission_id'] == native['admission_id']


def test_receipt_and_message_rollback_together(tmp_path):
    from hermes_state_input_receipts import append_received_input
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
    import sqlite3

    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('s', source='desktop')
        message = stamp_message_timestamp({'role': 'user', 'content': 'will not commit'})
        stamp_message_uid(message)
        db._conn.execute("CREATE TRIGGER reject_input BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected'):
            append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        assert admissions(db) == []
        assert db.get_session('s')['message_count'] == 0
        assert db._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        db._conn.execute('DROP TRIGGER reject_input')
        first = append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        second = append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        assert first == second
        assert len(admissions(db)) == 1
        assert db.get_session('s')['message_count'] == 1


@pytest.mark.asyncio
async def test_queued_replacement_never_fabricates_a_second_input(desktop, monkeypatch):
    db, sid, _key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    session['running'] = True
    before = dt.datetime.now(dt.UTC)
    response = await receive(sid, 'queued receive', params={'queued': True})
    assert response['result']['status'] == 'queued'
    assert history(db, before)['included_messages'] == 1
    server._replace_queued_user_row_for_turn(session, session['queued_prompt'], is_dispatching=True)
    result = history(db, before)
    assert [row['text'] for row in result['messages']] == ['queued receive']
    assert result['complete_within_declared_scope'] is True
    assert len(admissions(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interrupt', 'steer'])
async def test_busy_correction_keeps_the_original_received_input(desktop, monkeypatch, mode):
    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: mode)
    seen = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True,
        steer=lambda text: seen.append(text) or True))
    server._ensure_session_db_row(session)
    before = dt.datetime.now(dt.UTC)
    reply = await receive(sid, 'this is my correction')
    assert reply['result']['status'] == ('redirected' if mode == 'interrupt' else 'steered')
    assert seen == ['this is my correction']
    assert db.get_messages_as_conversation(key) == []
    assert [row['text'] for row in history(db, before)['messages']] == ['this is my correction']
    assert len(admissions(db)) == 1


@pytest.mark.asyncio
async def test_source_receipt_failure_does_not_misreport_an_accepted_correction(desktop, monkeypatch):
    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    seen = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True))
    server._ensure_session_db_row(session)
    db._conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON session_admissions BEGIN SELECT RAISE(ABORT,'receipt injected failure'); END")
    response = await receive(sid, 'already accepted correction')
    assert response['result']['status'] == 'redirected', response
    assert seen == ['already accepted correction']
    assert admissions(db) == []
    assert db._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_ordinary_submit_receipt_failure_is_not_accepted(desktop, monkeypatch):
    import hermes_state_input_receipts as receipts

    db, sid, key, _session, _starts = desktop
    monkeypatch.setattr(receipts, 'append_received_input',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError('ordinary receipt failure')))
    response = await receive(sid, 'ordinary input with failed provenance')
    assert 'error' in response, response
    assert db.get_messages_as_conversation(key, include_inactive=True) == []
    assert admissions(db) == []


@pytest.mark.asyncio
async def test_busy_queue_receipt_failure_rolls_back_the_queued_input(desktop, monkeypatch):
    import hermes_state_input_receipts as receipts

    db, sid, _key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    session['running'] = True
    monkeypatch.setattr(receipts, 'append_received_input',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError('queued receipt failure')))
    response = await receive(sid, 'queued input with failed provenance', params={'queued': True})
    assert 'error' in response, response
    assert session.get('queued_prompt') is None
    assert not session.get('queued_prompts')
    assert admissions(db) == []


@pytest.mark.asyncio
async def test_unexpected_receipt_failure_preserves_an_accepted_correction(desktop, monkeypatch, caplog):
    from tui_gateway import input_provenance

    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    delivered = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: delivered.append(text) or True))
    server._ensure_session_db_row(session)

    def fail_receipt(_response):
        raise LookupError('injected receipt failure')

    monkeypatch.setattr(input_provenance, 'record_accepted_input', fail_receipt)
    response = await receive(sid, 'delivered despite receipt failure')
    assert response['result']['status'] == 'redirected', response
    assert delivered == ['delivered despite receipt failure']
    assert admissions(db) == []
    assert 'accepted input source receipt failed' in caplog.text


@pytest.mark.asyncio
async def test_merged_queue_retains_each_received_original(desktop, monkeypatch):
    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    session['running'] = True
    before = dt.datetime.now(dt.UTC)
    await receive(sid, 'first queued', params={'queued': True})
    await receive(sid, 'second queued', params={'queued': True})
    assert session['queued_prompt']['text'] == 'first queued\n\nsecond queued'
    assert len(admissions(db)) == 2
    result = history(db, before)
    assert [row['text'] for row in result['messages']] == ['first queued', 'second queued']
    assert result['complete_within_declared_scope'] is False
    assert result['uncertain_source_count'] > 0
    assert [row['content'] for row in db.get_messages_as_conversation(key)] == ['first queued\n\nsecond queued']


@pytest.mark.asyncio
async def test_accepted_correction_does_not_recreate_a_deleted_session(desktop, monkeypatch):
    import asyncio
    import threading
    import hermes_state_input_receipts as receipts

    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    delivered = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: delivered.append(text) or True))
    server._ensure_session_db_row(session)
    blocked = threading.Event()
    release = threading.Event()
    original = receipts.append_received_input

    def pause_before_write(*args, **kwargs):
        blocked.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(receipts, 'append_received_input', pause_before_write)
    pending = asyncio.create_task(receive(sid, 'private correction'))
    try:
        assert await asyncio.to_thread(blocked.wait, 5)
        assert server._pop_session_by_id(sid) is session
        reply = await asyncio.to_thread(server.handle_request, {
            'id': 'delete', 'method': 'session.delete', 'params': {'session_id': key}})
        assert reply.get('result') == {'deleted': key}, reply
    finally:
        release.set()
    response = await pending
    assert response['result']['status'] == 'redirected'
    assert delivered == ['private correction']
    assert db.get_session(key) is None
    assert admissions(db) == []
    assert db._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_inflight_receipt_does_not_recreate_a_deleted_db_row(desktop, monkeypatch):
    import asyncio
    import hermes_state_input_receipts as receipts
    import threading

    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda _text: True))
    server._ensure_session_db_row(session)
    blocked, release = threading.Event(), threading.Event()
    original = receipts.append_received_input

    def pause_before_write(*args, **kwargs):
        blocked.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(receipts, 'append_received_input', pause_before_write)
    pending = asyncio.create_task(receive(sid, 'deleted before receipt commit'))
    try:
        assert await asyncio.to_thread(blocked.wait, 5)
        assert db.delete_session(key)
    finally:
        release.set()
    response = await pending
    assert response['result']['status'] == 'redirected', response
    assert db.get_session(key) is None
    assert admissions(db) == []


@pytest.mark.asyncio
async def test_accepted_correction_keeps_ack_when_session_rotates(desktop, monkeypatch):
    import asyncio
    import threading
    import hermes_state_input_receipts as receipts

    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    delivered = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: delivered.append(text) or True))
    server._ensure_session_db_row(session)
    original = receipts.append_received_input
    blocked = threading.Event()
    release = threading.Event()

    def pause_before_write(*args, **kwargs):
        blocked.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(receipts, 'append_received_input', pause_before_write)
    before = dt.datetime.now(dt.UTC)
    pending = asyncio.create_task(receive(sid, 'accepted before rotation'))
    try:
        assert await asyncio.to_thread(blocked.wait, 5)
        await asyncio.to_thread(db.publish_compression_child,
            parent_session_id=key, child_session_id='rotated-child', source='desktop',
            messages=[{'role': 'assistant', 'content': 'compressed handoff'}],
            require_compression_lease=False)
        session['agent'].session_id = 'rotated-child'
    finally:
        release.set()
    response = await pending
    assert response['result']['status'] == 'redirected', response
    assert delivered == ['accepted before rotation']
    result = history(db, before)
    assert [row['text'] for row in result['messages']] == ['accepted before rotation']
    assert len(admissions(db)) == 1
    assert admissions(db)[0]['target_session_id'] == 'rotated-child'


@pytest.mark.asyncio
async def test_accepted_correction_is_not_bound_to_a_replaced_runtime_session(desktop, monkeypatch):
    import asyncio
    import threading
    from tui_gateway import input_provenance

    db, sid, key, session, _starts = desktop
    other_sid, other_key = _desktop_session(monkeypatch, db)
    other_session = server._sessions[other_sid]
    db.create_session(other_key, source='desktop')
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    delivered = []
    session.update(running=True, agent=SimpleNamespace(
        session_id=key, _supports_active_turn_redirect=True,
        redirect=lambda text: delivered.append(text) or True))
    server._ensure_session_db_row(session)
    original = input_provenance.record_accepted_input
    blocked, release = threading.Event(), threading.Event()
    captured = []

    def pause_before_receipt(response):
        value = input_provenance._inbound.get()
        captured.append((id(value.session), value.session_key, id(session), id(other_session),
                         server._sessions.get(sid) is value.session))
        blocked.set()
        assert release.wait(5)
        return original(response)

    monkeypatch.setattr(input_provenance, 'record_accepted_input', pause_before_receipt)
    pending = asyncio.create_task(receive(sid, 'private original session input'))
    try:
        assert await asyncio.to_thread(blocked.wait, 5)
        with server._sessions_lock:
            server._sessions[sid] = other_session
        release.set()
        response = await pending
        assert captured
        assert response['result']['status'] in {'redirected', 'streaming'}, response
        if response['result']['status'] == 'redirected':
            assert delivered == ['private original session input']
        assert not [row for row in admissions(db) if row['target_session_id'] == other_key], (
            sid, key, other_sid, other_key,
            [(row['target_session_id'], row['request_id'], json.loads(row['payload_json'])) for row in admissions(db)],
            db.get_messages_as_conversation(other_key, include_inactive=True),
            server._sessions.get(sid) is other_session,
        )
        assert not db.get_messages_as_conversation(other_key, include_inactive=True)
    finally:
        release.set()
        with server._sessions_lock:
            server._sessions[sid] = session
            server._sessions.pop(other_sid, None)


@pytest.mark.asyncio
async def test_compute_dispatch_and_inline_fallback_keep_only_one_source(desktop, monkeypatch):
    db, sid, _key, session, _starts = desktop
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *_: True)
    monkeypatch.setattr(server, '_submit_prompt_to_compute_host', lambda rid, *_a, **_k:
                        {'id': rid, 'result': {'status': 'streaming', 'turn_isolation': True}})
    before = dt.datetime.now(dt.UTC)
    first = await receive(sid, 'isolated receive')
    assert first['result']['turn_isolation'] is True
    assert [row['text'] for row in history(db, before)['messages']] == ['isolated receive']
    session['running'] = False
    monkeypatch.setattr(server, '_submit_prompt_to_compute_host', lambda *_a, **_k:
                        {'error': {'message': 'synthetic dispatch failure'}})
    second = await receive(sid, 'inline fallback receive')
    assert second['result']['status'] == 'streaming'
    assert [row['text'] for row in history(db, before)['messages']] == ['isolated receive', 'inline fallback receive']
    assert len(admissions(db)) == 2


@pytest.mark.asyncio
async def test_receipt_row_never_appears_as_pending_or_native_work(desktop):
    from hermes_state_runtime import latest_session_admission, list_session_admissions

    db, sid, key, _session, _starts = desktop
    await receive(sid, 'recorded but never queued')
    assert list_session_admissions(db, session_id=key, pending_only=True) == []
    assert len(list_session_admissions(db, session_id=key, pending_only=False)) == 1
    assert latest_session_admission(db, session_id=key,
                                    accept=lambda payload: 'native_text_v1' in payload) is None


@pytest.mark.asyncio
async def test_recorded_receipt_can_be_retired_with_the_session(desktop):
    import hermes_state_runtime as runtime
    from hermes_state_terminal import ADMISSION_PREFIX

    db, sid, key, _session, _starts = desktop
    await receive(sid, 'private received words')
    admission_id = admissions(db)[0]['admission_id']
    epoch = runtime.begin_runtime_epoch(db, instance_id='owner-after-local-turn')
    session = db.get_session(key)
    runtime.mutate_runtime_session(db, epoch=epoch, principal_id='uid:fixture', session_id=key,
        request_id='delete', expected_revision=session['runtime_revision'],
        expected_generation=session['runtime_generation'], operation='delete', payload={})
    assert db.get_session(key) is None
    assert admissions(db) == []
    assert db._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    retained = db._conn.execute('SELECT value FROM state_meta WHERE key=?',
                                (ADMISSION_PREFIX + admission_id,)).fetchone()
    assert json.loads(retained[0])['payload_json'] == '{}'
    assert 'private received words' not in retained[0]


@pytest.mark.asyncio
async def test_rewinding_a_received_input_retires_its_source_witness(desktop):
    db, sid, key, _session, _starts = desktop
    before = dt.datetime.now(dt.UTC)
    await receive(sid, 'rewind this received input')
    active = db._conn.execute(
        "SELECT id FROM messages WHERE session_id=? AND active=1 AND role='user' ORDER BY id LIMIT 1", (key,)
    ).fetchone()[0]
    db.rewind_to_message(key, active)
    witness = db._conn.execute(
        "SELECT display_metadata FROM messages WHERE session_id=? AND active=0 ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()[0]
    assert 'submission_id' not in (json.loads(witness or '{}'))
    result = history(db, before)
    assert result['included_messages'] == 0
    assert result['uncertain_source_count'] >= 1


def test_closed_input_scope_cannot_be_inherited_by_a_delayed_callback(desktop):
    import contextvars
    from tui_gateway.input_provenance import bind_input, inbound_input_scope, take_input

    _db, sid, _key, session, _starts = desktop
    transport = SimpleNamespace(auth_identity=None, verified_operator=True, desktop_local=True)
    request = {'method': 'prompt.submit', 'params': {'session_id': sid, 'text': 'one receive'}}
    with inbound_input_scope(request, transport):
        bind_input(session, 'one receive', None)
        copied = contextvars.copy_context()
        assert take_input(session, 'one receive', None) is not None
    assert copied.run(take_input, session, 'one receive', None) is None


@pytest.mark.asyncio
async def test_source_binding_survives_the_real_turn_flush_and_cold_read(desktop):
    from tests.tui_gateway.test_submit_time_user_row import _flush_agent
    from agent.turn_context import _stage_turn_user_message

    db, sid, key, session, _starts = desktop
    before = dt.datetime.now(dt.UTC)
    text = 'keep the verified original text'
    await receive(sid, text)
    agent = _flush_agent(db, key)
    server._adopt_submit_user_row(session, agent, text, text)
    user, _ = _stage_turn_user_message(agent, text, text, None, None, None, None)
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db([user, {'role': 'assistant', 'content': 'done'}], [])
    result = history(db, before)
    assert result['complete_within_declared_scope'] is True
    assert [row['text'] for row in result['messages']] == [text]
    with SessionDB(db.db_path) as cold:
        saved = cold.get_messages_as_conversation(key)
        assert len([row for row in saved if row['role'] == 'user']) == 1
        assert saved[0]['display_metadata']['submission_id'] == admissions(db)[0]['request_id']


@pytest.mark.asyncio
async def test_expanded_text_is_not_certified_as_the_users_original_words(desktop):
    db, sid, key, session, _starts = desktop
    await receive(sid, 'read @private.txt')
    agent = SimpleNamespace(_pending_cli_user_message=None, _session_persist_lock=None)
    server._adopt_submit_user_row(session, agent, 'read @private.txt\n\nfile payload', 'read @private.txt')
    source = db._conn.execute('SELECT content FROM messages WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1', (key,)).fetchone()[0]
    proof = json.loads(admissions(db)[0]['payload_json'])
    assert proof['text'] == 'read @private.txt'
    assert source != proof['text']
    result = history(db, dt.datetime.fromtimestamp(0, dt.UTC))
    assert [row['text'] for row in result['messages']] == ['read @private.txt']
    assert result['complete_within_declared_scope'] is False
