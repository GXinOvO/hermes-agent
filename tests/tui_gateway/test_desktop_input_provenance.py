import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from persistence.conversation.lineage import CompressionChild
from persistence.conversation.messages import ConversationRequest, NewMessage, TranscriptRead, TranscriptView
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope, store_location
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import conversation, session_row, transcript_rows
from tests.tui_gateway.test_submit_time_user_row import _desktop_session
from tui_gateway import server


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    db = open_persistence(profile_scope(home=tmp_path), eager=True)
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
    return collector.collect_history(store_location(db.scope), before, dt.datetime.now(dt.UTC))


def sql(db, statement, params=()):
    with raw_holder(db)._read_ctx() as conn:
        return conn.execute(statement, tuple(params)).fetchall()


def write_sql(db, statement):
    raw_holder(db)._execute_write(lambda conn: conn.execute(statement))


def admissions(db):
    return [dict(row) for row in sql(db, 'SELECT * FROM session_admissions ORDER BY seq')]


def rows(db, key, view=TranscriptView.ALL):
    return transcript_rows(db, key, TranscriptRead(view=view))


_UNSET = object()


def desktop_identity():
    return {'provider': 'local', 'user_id': 'desktop-fixture',
            'profile_id': 'fixture', 'instance_id': 'fixture',
            'capabilities': ['session:create'], 'native_bootstrap': True}


async def receive(sid, text, *, operator=True, identity=_UNSET, params=None, ssh_input=None):
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
    await handle_ws(Socket(), auth_identity=identity, operator=operator, ssh_input=ssh_input)
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
    stored = conversation(db, key, ConversationRequest(include_row_ids=True))
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
    assert len(rows(db, key, TranscriptView.DISPLAY)) == 1
    assert [message['content'] for message in rows(db, key, TranscriptView.DISPLAY)] == [text]


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('boundary', ['normal', 'file-api', 'runtime'])
async def test_private_ssh_ticket_binds_only_its_connection(desktop, monkeypatch, tmp_path, boundary):
    import re
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocket
    from hermes_cli import web_server as web
    from hermes_cli.web_server_dashboard import mount_spa
    from hermes_cli.web_server_session_token import ssh_input_bootstrap
    from hermes_cli.web_routers.chat_ws import gateway_ws

    db, sid, key, session, _starts = desktop
    from hermes_constants import get_process_hermes_home
    monkeypatch.setattr(server, '_hermes_home', get_process_hermes_home())
    nonce = '0123456789abcdef'
    owner = get_process_hermes_home() / 'desktop-ssh' / ('a' * 32)
    owner.mkdir(mode=0o700, parents=True)
    runtime_marker = tmp_path / 'runtime-marker'
    runtime_marker.touch()
    monkeypatch.setattr(web, '_SSH_RUNTIME_MARKER', str(runtime_marker))
    monkeypatch.setattr(web, '_SESSION_TOKEN', 'public-fixture-token')
    monkeypatch.setattr(web, '_SSH_OWNER_NONCE', nonce)
    for name, value in (('auth_required', False), ('bound_host', '127.0.0.1'),
                        ('bound_port', 9120), ('session_authority', None),
                        ('withhold_session_token', False)):
        monkeypatch.setattr(web.app.state, name, value, raising=False)
    monkeypatch.setattr('hermes_cli.mcp_startup.start_deferred_mcp_discovery_now', lambda: None)
    monkeypatch.setenv('HERMES_SERVE_HEADLESS', '1')
    page_app = FastAPI()
    page_app.state.auth_required = False
    mount_spa(page_app)
    page = TestClient(page_app).get('/')
    public = json.loads(re.search(r'window\.__HERMES_SESSION_TOKEN__=("[^"\n]+");', page.text)[1])
    client = TestClient(web.app, base_url='http://127.0.0.1:9120',
                        headers={'X-Hermes-Session-Token': public})

    async def dial(ticket, text, profile='current'):
        request = {'jsonrpc': '2.0', 'id': 'p', 'method': 'prompt.submit',
                   'params': {'session_id': sid, 'text': text}}
        events = iter([{'type': 'websocket.connect'},
                       {'type': 'websocket.receive', 'text': json.dumps(request)},
                       {'type': 'websocket.disconnect', 'code': 1000}])
        frames = []

        async def incoming():
            return next(events)

        async def outgoing(frame):
            frames.append(frame)

        headers = [(b'host', b'127.0.0.1:9120')]
        if ticket is not None:
            headers.append((b'x-hermes-ssh-input-ticket', ticket.encode()))
            headers.append((b'x-hermes-ssh-input-profile', profile.encode()))
        ws = WebSocket({'type': 'websocket', 'path': '/api/ws', 'scheme': 'ws',
                        'headers': headers, 'query_string': ('token=' + public).encode(),
                        'app': web.app, 'client': ('127.0.0.1', 12345),
                        'server': ('127.0.0.1', 9120)}, incoming, outgoing)
        session['running'] = False
        await gateway_ws(ws)
        return ws, frames

    before = dt.datetime.now(dt.UTC)
    with ssh_input_bootstrap(owner, nonce) as proof:
        assert proof is not None
        monkeypatch.setattr(web.app.state, 'ssh_input', proof, raising=False)
        private = json.loads(next(owner.glob('*.input-key')).read_text())
        assert private['instance_id'] == proof['instance_id']
        assert next(owner.glob('*.input-key')).stat().st_mode & 0o777 == 0o600
        assert private['key'] not in page.text
        if boundary == 'file-api':
            leaf = next(owner.glob('*.input-key'))
            alias = tmp_path / 'ordinary.txt'
            alias.symlink_to(leaf)
            listed = client.get('/api/fs/list', params={'path': str(owner.parent)})
            statuses = [client.get(route, params={'path': str(path)}).status_code
                        for path in (leaf, alias)
                        for route in ('/api/fs/read-text', '/api/fs/read-data-url', '/api/fs/download', '/api/files/download')]
            assert set(statuses) == {403}
            assert listed.status_code == 200 and listed.json()['entries'] == []
            from agent.file_safety import get_read_block_error
            assert get_read_block_error(str(leaf)) and get_read_block_error(str(alias))
            normal = tmp_path / 'normal.txt'
            normal.write_text('ordinary read remains allowed')
            assert client.get('/api/fs/read-text', params={'path': str(normal)}).json()['text'] == normal.read_text()
            assert client.post('/api/ssh/input-ticket', json={'key': public, 'instance_id': proof['instance_id'], 'profile': 'current'}).status_code == 403
            await dial(None, 'public-token input after protected file refusal')
            assert admissions(db) == [] and history(db, before)['included_messages'] == 0
            # Control: with the path gate off, a public token that reads this file forges an SSH source.
            with monkeypatch.context() as gate:
                gate.setattr('hermes_cli.web_routers.files._is_sensitive_path', lambda _path: False)
                stolen = json.loads(client.get('/api/fs/read-text', params={'path': str(leaf)}).json()['text'])
                grant = client.post('/api/ssh/input-ticket', json={**stolen, 'profile': 'current'})
                assert grant.status_code == 200
                await dial(grant.json()['ticket'], 'control: file gate disabled')
            assert len(admissions(db)) == history(db, before)['included_messages'] == 1
            return
        ownership = client.get('/api/ssh/ownership').json()
        assert ownership['sshInputInstance'] == private['instance_id']
        assert private['key'] not in json.dumps(ownership)
        for secret in (None, public, 'forged'):
            response = client.post('/api/ssh/input-ticket', json={
                'profile': 'current', 'key': secret, 'instance_id': proof['instance_id']})
            assert response.status_code == 403, response.text
        _forged, frames = await dial('forged', 'forged SSH proof')
        assert any(f.get('code') == 4401 for f in frames)
        ordinary, _ = await dial(None, 'ordinary public-token client')
        assert ordinary.scope.get('hermes.ssh_input') is None
        assert admissions(db) == []
        assert history(db, before)['included_messages'] == 0
        if boundary == 'runtime':
            ticket = client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'}).json()['ticket']
            runtime_marker.unlink()
            assert client.get('/api/ssh/ownership').json()['runtimeIntact'] is False
            assert client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'}).status_code == 403
            _stale, frames = await dial(ticket, 'runtime invalidated before redeem')
            assert any(f.get('code') == 4401 for f in frames)
            assert all(not store.outstanding() for store in proof['tickets'].values())
            runtime_marker.touch()
            _revoked, frames = await dial(ticket, 'revoked ticket after runtime recovery')
            assert any(f.get('code') == 4401 for f in frames)
            assert admissions(db) == [] and history(db, before)['included_messages'] == 0
            return
        response = client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'})
        assert response.status_code == 200, response.text
        ticket = response.json()['ticket']
        trusted, frames = await dial(ticket, 'private SSH connection input')
        assert not any(f.get('code', 1000) != 1000 for f in frames), frames
        assert trusted.scope['hermes.ssh_input']['instance_id'] == proof['instance_id']
        row, = admissions(db)
        assert row['target_session_id'] == key
        assert json.loads(row['payload_json'])['text'] == 'private SSH connection input'
        stored = conversation(db, key, ConversationRequest(include_row_ids=True))
        source = next(m for m in stored if m['content'] == 'private SSH connection input')['display_metadata']['ssh_input_v1']
        assert source['instance_id'] == proof['instance_id']
        assert source['connection_id'] == trusted.scope['hermes.ssh_input']['connection_id']
        assert source['sequence'] == 1
        assert [m['text'] for m in history(db, before)['messages']] == ['private SSH connection input']
        _replay, frames = await dial(ticket, 'replayed SSH ticket')
        assert any(f.get('code') == 4401 for f in frames)
        await dial(None, 'public-token connection after private input')
        assert len(admissions(db)) == 1
        unused = client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'}).json()['ticket']
        other = tmp_path / 'other-profile'
        other.mkdir()
        monkeypatch.setattr('hermes_cli.web_server_profiles._resolve_profile_dir', lambda _name: other)
        _wrong_profile, frames = await dial(unused, 'wrong profile proof', profile='other')
        assert any(f.get('code') == 4401 for f in frames)
        session['profile_home'] = str(other)
        _wrong_target, frames = await dial(unused, 'private connection to wrong target profile')
        assert not any(f.get('code', 1000) != 1000 for f in frames)
        assert len(admissions(db)) == 1
        session['profile_home'] = None
        from gateway.runtime_bootstrap import TicketStore
        with monkeypatch.context() as clock:
            clock.setattr(TicketStore, 'TTL_SECONDS', 0)
            expired = client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'}).json()['ticket']
            _expired, frames = await dial(expired, 'expired SSH proof')
            assert any(f.get('code') == 4401 for f in frames)
        with ssh_input_bootstrap(owner, nonce) as successor:
            monkeypatch.setattr(web.app.state, 'ssh_input', successor)
            assert client.post('/api/ssh/input-ticket', json={**private, 'profile': 'current'}).status_code == 403
            _old_instance, frames = await dial(unused, 'old SSH instance proof')
            assert any(f.get('code') == 4401 for f in frames)
        assert len(admissions(db)) == 1
    assert not list(owner.glob('*.input-key'))


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
    db.sessions.create(NewSession('other', source='gui'))
    epoch = begin_runtime_epoch(db, instance_id='the-existing-gateway')
    before = dict(sql(db, 'SELECT * FROM runtime_epoch')[0])
    await receive(sid, 'ordinary local submit')
    assert dict(sql(db, 'SELECT * FROM runtime_epoch')[0]) == before
    assert session_row(db, key)['runtime_generation'] == 0
    assert claim_session_input(db, epoch=epoch, session_id=key) is None
    native = admit_session_input(db, epoch=epoch, principal_id='uid:fixture', session_id='other',
                                 request_id='canonical-next', payload={'text': 'other conversation'})
    assert claim_session_input(db, epoch=epoch, session_id='other')['admission_id'] == native['admission_id']


def test_receipt_and_message_rollback_together(tmp_path):
    from hermes_state_input_receipts import append_received_input
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
    import sqlite3

    with open_persistence(profile_scope(home=tmp_path), eager=True) as db:
        db.sessions.create(NewSession('s', source='desktop'))
        message = stamp_message_timestamp({'role': 'user', 'content': 'will not commit'})
        stamp_message_uid(message)
        write_sql(db, "CREATE TRIGGER reject_input BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected'):
            append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        assert admissions(db) == []
        assert session_row(db, 's')['message_count'] == 0
        assert sql(db, 'SELECT count(*) FROM messages')[0][0] == 0
        write_sql(db, 'DROP TRIGGER reject_input')
        first = append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        second = append_received_input(db, 's', message, principal_id='uid:fixture', request_id='receive-once')
        assert first == second
        assert len(admissions(db)) == 1
        assert session_row(db, 's')['message_count'] == 1


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
    assert conversation(db, key) == []
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
    write_sql(db, "CREATE TRIGGER reject_receipt BEFORE INSERT ON session_admissions BEGIN SELECT RAISE(ABORT,'receipt injected failure'); END")
    response = await receive(sid, 'already accepted correction')
    assert response['result']['status'] == 'redirected', response
    assert seen == ['already accepted correction']
    assert admissions(db) == []
    assert sql(db, 'SELECT count(*) FROM messages')[0][0] == 0


@pytest.mark.asyncio
async def test_ordinary_submit_receipt_failure_is_not_accepted(desktop, monkeypatch):
    import hermes_state_input_receipts as receipts

    db, sid, key, _session, _starts = desktop
    monkeypatch.setattr(receipts, 'append_received_input',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError('ordinary receipt failure')))
    response = await receive(sid, 'ordinary input with failed provenance')
    assert 'error' in response, response
    assert conversation(db, key, ConversationRequest(view=TranscriptView.ALL)) == []
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
@pytest.mark.parametrize('ssh', [False, True])
async def test_merged_queue_retains_each_received_original(desktop, monkeypatch, ssh):
    db, sid, key, session, _starts = desktop
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    session['running'] = True
    before = dt.datetime.now(dt.UTC)
    source = {'profile_home': str(Path(server._hermes_home).resolve()), 'instance_id': 'test-instance',
              'connection_id': 'test-connection'} if ssh else None
    for text in ('first queued', 'second queued'):
        await receive(sid, text, params={'queued': True}, ssh_input=source,
                      identity=None if ssh else desktop_identity())
    assert session['queued_prompt']['text'] == 'first queued\n\nsecond queued'
    assert len(admissions(db)) == 2
    result = history(db, before)
    assert [row['text'] for row in result['messages']] == ['first queued', 'second queued']
    assert result['complete_within_declared_scope'] is False
    assert result['uncertain_source_count'] > 0
    assert [row['content'] for row in conversation(db, key)] == ['first queued\n\nsecond queued']
    if ssh:
        witnesses = [row for row in rows(db, key) if row['active'] == 0]
        assert [row['content'] for row in witnesses] == ['first queued', 'second queued']
        assert all(row['display_metadata']['ssh_input_v1'] == {**source, 'sequence': 1}
                   for row in witnesses)


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
    assert session_row(db, key) is None
    assert admissions(db) == []
    assert sql(db, 'SELECT count(*) FROM messages')[0][0] == 0


@pytest.mark.asyncio
async def test_inflight_receipt_does_not_recreate_a_deleted_db_row(desktop, monkeypatch, tmp_path):
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
        assert db.sessions.delete(key, media_root=str(tmp_path / 'media'))
    finally:
        release.set()
    response = await pending
    assert response['result']['status'] == 'redirected', response
    assert session_row(db, key) is None
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
        await asyncio.to_thread(db.lineage.publish_compression_child, key,
            CompressionChild(session_id='rotated-child', source='desktop'),
            [NewMessage(role='assistant', content='compressed handoff')],
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
    db.sessions.create(NewSession(other_key, source='desktop'))
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
            conversation(db, other_key, ConversationRequest(view=TranscriptView.ALL)),
            server._sessions.get(sid) is other_session,
        )
        assert not conversation(db, other_key, ConversationRequest(view=TranscriptView.ALL))
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
    from persistence.sqlite.conversation._store.terminal import ADMISSION_PREFIX

    db, sid, key, _session, _starts = desktop
    await receive(sid, 'private received words')
    admission_id = admissions(db)[0]['admission_id']
    epoch = runtime.begin_runtime_epoch(db, instance_id='owner-after-local-turn')
    session = session_row(db, key)
    runtime.mutate_runtime_session(db, epoch=epoch, principal_id='uid:fixture', session_id=key,
        request_id='delete', expected_revision=session['runtime_revision'],
        expected_generation=session['runtime_generation'], operation='delete', payload={})
    assert session_row(db, key) is None
    assert admissions(db) == []
    assert sql(db, 'SELECT count(*) FROM messages')[0][0] == 0
    retained = sql(db, 'SELECT value FROM state_meta WHERE key=?',
                   (ADMISSION_PREFIX + admission_id,))[0]
    assert json.loads(retained[0])['payload_json'] == '{}'
    assert 'private received words' not in retained[0]


@pytest.mark.asyncio
async def test_rewinding_a_received_input_retires_its_source_witness(desktop):
    db, sid, key, _session, _starts = desktop
    before = dt.datetime.now(dt.UTC)
    await receive(sid, 'rewind this received input')
    db.messages.rewind_user_turn(key, 0)
    witness = sql(db, "SELECT display_metadata FROM messages WHERE session_id=? AND active=0 ORDER BY id DESC LIMIT 1",
                  (key,))[0][0]
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
async def test_source_binding_survives_the_real_turn_flush_and_cold_read(desktop, tmp_path):
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
    with open_persistence(profile_scope(home=tmp_path), eager=True) as cold:
        saved = conversation(cold, key)
        assert len([row for row in saved if row['role'] == 'user']) == 1
        assert saved[0]['display_metadata']['submission_id'] == admissions(db)[0]['request_id']


@pytest.mark.asyncio
async def test_expanded_text_is_not_certified_as_the_users_original_words(desktop):
    db, sid, key, session, _starts = desktop
    await receive(sid, 'read @private.txt')
    agent = SimpleNamespace(_pending_cli_user_message=None, _session_persist_lock=None)
    server._adopt_submit_user_row(session, agent, 'read @private.txt\n\nfile payload', 'read @private.txt')
    source = sql(db, 'SELECT content FROM messages WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1', (key,))[0][0]
    proof = json.loads(admissions(db)[0]['payload_json'])
    assert proof['text'] == 'read @private.txt'
    assert source != proof['text']
    result = history(db, dt.datetime.fromtimestamp(0, dt.UTC))
    assert [row['text'] for row in result['messages']] == ['read @private.txt']
    assert result['complete_within_declared_scope'] is False
