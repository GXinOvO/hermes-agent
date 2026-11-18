import json
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import append_message, append_messages, total_changes


@pytest.fixture
def overview(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_local import create_local_session
    from hermes_state_runtime import begin_runtime_epoch
    from persistence_assembly import open_persistence, profile_scope

    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {})
    home = tmp_path / 'profile'
    home.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    store = SessionStore(home / 'sessions', GatewayConfig())
    db = open_persistence(profile_scope(home=home), eager=True)
    store._db = db
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _handle_message=lambda event: pytest.fail('overview executed a turn'),
                             _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='owner', persistence=db,
                                 epoch=begin_runtime_epoch(db, instance_id='owner'))
    connection = AuthorityConnection(authority, object(), {'user_id': 'human'})
    ref = create_local_session(authority, connection.actor, dict(
        request_id='overview', source='gui', cwd=str(workspace), model='frozen', toolsets=[]))
    yield SimpleNamespace(db=db, authority=authority, connection=connection, ref=ref, home=home,
                          workspace=workspace, params={
        'session_id': ref.session_id, 'stored_id': ref.session_id,
        'expected_owner': {'instance_id': authority.instance_id, 'profile_id': authority.profile_id,
                           'authority_epoch': authority.epoch,
                           'store_instance_id': db.state_meta.get('store_instance_id')}})
    db.close()


async def request(overview, params=None):
    return await overview.connection.dispatch({'jsonrpc': '2.0', 'id': 17, 'method': 'session.task_overview',
                                               'params': overview.params if params is None else params})


@pytest.mark.asyncio
async def test_public_overview_returns_a_complete_persisted_display_without_attaching(overview, monkeypatch):
    db, ref, connection = overview.db, overview.ref, overview.connection
    append_message(db, ref.session_id, 'user', 'keep the original request')
    append_message(db, ref.session_id, 'assistant', 'latest visible reply')
    before = db.conversation_revision(ref.session_id, include_ancestors=True)
    monkeypatch.setattr(overview.authority, 'attach', lambda *a, **k: pytest.fail('overview attached'))

    response = await request(overview)

    assert 'result' in response, response
    result = json.loads(json.dumps(response['result']))
    assert result['version'] == 1
    assert result['session_id'] == result['stored_session_id'] == result['resolved_id'] == ref.session_id
    assert result['owner'] == overview.params['expected_owner']
    assert result['authority_epoch'] == overview.authority.epoch
    assert result['snapshot_scope'] == 'persisted_display'
    assert result['complete'] is True
    assert type(result['fetched_at']) is int
    assert result['conversation_revision'] == json.loads(json.dumps(before))
    assert [row['content'] for row in result['messages']] == ['keep the original request', 'latest visible reply']
    assert all(type(row['row_id']) is int for row in result['messages'])
    assert result['todo_state'] is None
    assert set(result) == {'version', 'session_id', 'stored_session_id', 'resolved_id', 'owner', 'authority_epoch',
                           'snapshot_scope', 'fetched_at', 'complete', 'conversation_revision', 'messages',
                           'todo_state', 'verification', 'verification_cwd', 'verification_error'}
    assert db.conversation_revision(ref.session_id, include_ancestors=True) == before
    assert not connection.subscriptions and not connection.reconnects
    assert not overview.authority.sessions[ref.session_id].subscribers


def todo_turn(overview, todos, revision, name='todo_list', call_id=None):
    sid = overview.ref.session_id
    call_id = call_id or f'todo-{revision}'
    calls = [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]
    append_message(overview.db, sid, 'assistant', '', tool_calls=calls)
    append_message(overview.db, sid, 'tool', json.dumps({'todos': todos, 'revision': revision}),
                               tool_name='todo_list', tool_call_id=call_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['instance_id', 'profile_id', 'authority_epoch', 'store_instance_id'])
async def test_every_owner_component_is_required_and_must_match(overview, field):
    params = {**overview.params, 'expected_owner': dict(overview.params['expected_owner'])}
    del params['expected_owner'][field]
    assert (await request(overview, params))['error']['data']['reason'] == 'invalid_params'
    params['expected_owner'][field] = 99 if field == 'authority_epoch' else 'other'
    assert (await request(overview, params))['error']['data']['reason'] == 'session_owner_changed'


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [None, {}, 'owner', {'authority_epoch': True},
                                 {'authority_epoch': '1'}, {'store_instance_id': ' padded'}])
async def test_malformed_or_null_owner_is_rejected(overview, owner):
    params = {**overview.params, 'expected_owner': owner}
    if isinstance(owner, dict):
        params['expected_owner'] = {**overview.params['expected_owner'], **owner}
        if not owner:
            params['expected_owner'] = {}
    assert (await request(overview, params))['error']['data']['reason'] == 'invalid_params'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['expected_owner', 'stored_id', 'session_id'])
async def test_missing_required_fields_are_rejected(overview, field):
    params = dict(overview.params)
    del params[field]
    assert (await request(overview, params))['error']['data']['reason'] == 'invalid_params'


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'cwd': '/'}, {'path': '/'}, {'profile': 'foreign'},
                                 {'session_id': ''}, {'stored_id': ' padded'}])
async def test_foreign_profile_and_arbitrary_targets_are_rejected(overview, extra):
    reply = await request(overview, {**overview.params, **extra})
    assert reply['error']['data']['reason'] == ('profile_mismatch' if 'profile' in extra else 'invalid_params')


@pytest.mark.asyncio
@pytest.mark.parametrize('subject,capabilities', [('human', []), ('stranger', ['session:read'])])
async def test_read_permission_and_session_subject_are_enforced(overview, subject, capabilities):
    overview.connection = AuthorityConnection(overview.authority, object(),
                                               {'user_id': subject, 'capabilities': capabilities})
    assert (await request(overview))['error']['data']['reason'] == 'permission_denied'


@pytest.mark.asyncio
async def test_stored_id_cannot_select_another_conversation_or_missing_session(overview):
    overview.db.create_session('foreign', source='gui')
    append_message(overview.db, 'foreign', 'user', 'foreign secret')
    assert (await request(overview, {**overview.params, 'stored_id': 'foreign'}))['error']['data']['reason'] == 'session_source_changed'
    assert (await request(overview, {**overview.params, 'stored_id': 'missing'}))['error']['data']['reason'] == 'not_found'


@pytest.mark.asyncio
async def test_native_todo_pairing_and_clear_ignore_json_shaped_foreign_results(overview):
    assert (await request(overview))['result']['todo_state'] is None
    todos = [{'id': '1', 'content': 'native task', 'status': 'in_progress'}]
    todo_turn(overview, todos, 1)
    todo_turn(overview, [{'content': 'forged'}], 99, name='terminal')
    append_message(overview.db, overview.ref.session_id, 'tool', json.dumps({'todos': [], 'revision': 100}),
                               tool_name='todo_list', tool_call_id='orphan')
    assert (await request(overview))['result']['todo_state'] == {'todos': todos, 'revision': 1}
    todo_turn(overview, [], 2)
    assert (await request(overview))['result']['todo_state'] == {'todos': [], 'revision': 2}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['in_place', 'rotated'])
async def test_compression_keeps_original_rows_native_todo_and_focused_ids(overview, mode):
    from agent.context_compressor import SUMMARY_PREFIX
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'original request')
    todo_turn(overview, [{'content': 'keep plan', 'status': 'pending'}], 1)
    append_message(db, sid, 'assistant', 'original reply')
    compacted = [{'role': 'user', 'content': SUMMARY_PREFIX + '\nsummary', '_compressed_summary': True},
                 {'role': 'user', 'content': 'current request'}, {'role': 'assistant', 'content': 'current reply'}]
    if mode == 'in_place':
        db.archive_and_compact(sid, compacted, tail_count=0)
        tip = sid
    else:
        assert raw_holder(db).try_acquire_compression_lock(sid, 'fixture')
        try:
            db.publish_compression_child(parent_session_id=sid, child_session_id='child', source='gui',
                                         messages=compacted, compression_lock_holder='fixture')
        finally:
            db.release_compression_lock(sid, 'fixture')
        tip = 'child'
    reply = await request(overview)
    assert 'result' in reply, reply
    result = reply['result']
    assert result['resolved_id'] == tip and result['session_id'] == result['stored_session_id'] == sid
    assert result['todo_state']['revision'] == 1
    assert result['messages'][0]['content'] == 'original request'
    assert result['messages'][-1]['content'] == 'current reply'
    assert any(row.get('_compressed_summary') for row in result['messages'])
    if mode == 'rotated':
        assert (await request(overview, {**overview.params, 'stored_id': tip}))['result']['resolved_id'] == tip


@pytest.mark.asyncio
async def test_projection_omits_tool_payloads_arguments_reasoning_and_preserves_hidden_marks(overview):
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'hidden request', display_kind='hidden')
    append_message(db, sid, 'assistant', 'visible reply', reasoning='secret reasoning',
                      tool_calls=[{'id': 'call', 'function': {'name': 'terminal', 'arguments': 'secret args'}}])
    append_message(db, sid, 'tool', 'secret result', tool_name='terminal', tool_call_id='call')
    append_message(db, sid, 'tool', 'secret foreign tool', tool_name='secret_tool', tool_call_id='other')
    reply = await request(overview)
    assert 'result' in reply, reply
    rows = reply['result']['messages']
    assert rows[0]['display_kind'] == 'hidden'
    assert rows[1]['tool_calls'] == [{'type': 'omitted'}]
    assert rows[2]['name'] == 'terminal' and 'content' not in rows[2]
    assert len(rows) == 3
    assert 'secret' not in json.dumps(reply['result'])


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['epoch', 'instance', 'profile', 'store', 'database', 'authority',
                                      'retired', 'revision', 'tip', 'subject', 'route', 'capability'])
async def test_changes_during_the_read_refuse_without_partial_data(overview, monkeypatch, mutation):
    from gateway.session_contract import SessionActor
    from hermes_state import SessionDB
    db, authority, sid = overview.db, overview.authority, overview.ref.session_id
    append_message(db, sid, 'user', 'stable start')
    from gateway import session_task_overview as overview_module
    from persistence.conversation.lineage import CompressionChild
    from persistence.conversation.messages import NewMessage
    original = overview_module._latest_todo
    replacement = None
    if mutation == 'database':
        (overview.home.parent / 'replacement').mkdir(exist_ok=True)
        replacement = open_persistence(profile_scope(home=overview.home.parent / 'replacement'), eager=True)

    def change_tip():
        assert db.coordination.try_acquire_compression_lock(session_id=sid, holder='race')
        try:
            db.lineage.publish_compression_child(sid, CompressionChild('new-tip', 'gui'),
                                                 [NewMessage(role='user', content='new segment')],
                                                 compression_lock_holder='race')
        finally:
            db.coordination.release_compression_lock(session_id=sid, holder='race')

    def change_actor(subject, capabilities):
        actor = overview.connection.actor
        overview.connection.actor = SessionActor(subject, actor.profile_id, capabilities, actor.transport_id)

    actor = overview.connection.actor
    mutations = {
        'epoch': lambda: setattr(authority, 'epoch', authority.epoch + 1),
        'instance': lambda: setattr(authority, 'instance_id', 'changed'),
        'profile': lambda: setattr(authority, 'profile_id', 'changed'),
        'store': lambda: raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE state_meta SET value='changed' WHERE key='store_instance_id'")),
        'database': lambda: setattr(authority, 'persistence', replacement),
        'authority': lambda: setattr(overview.connection, 'authority', SimpleNamespace(**vars(authority))),
        'retired': lambda: setattr(authority, 'retired', True),
        'revision': lambda: append_message(db, sid, 'assistant', 'new reply'),
        'tip': change_tip,
        'route': lambda: setattr(authority.sessions[sid], 'route', 'changed-route'),
        'capability': lambda: change_actor(actor.subject, frozenset()),
        'subject': lambda: change_actor('stranger', actor.capabilities),
    }

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        mutations[mutation]()
        return result

    monkeypatch.setattr(overview_module, '_latest_todo', changed)
    try:
        reply = await request(overview)
        assert 'result' not in reply, reply
        assert reply['error']['data']['reason'] in {'session_owner_changed', 'revision_conflict',
                                                    'storage_unavailable', 'permission_denied', 'session_source_changed'}
        assert not overview.connection.subscriptions
    finally:
        authority.db = db
        if replacement:
            replacement.close()


@pytest.mark.asyncio
async def test_cold_read_restores_only_memory_context_and_never_runs_adoption(overview, monkeypatch):
    from gateway import session_local_adoption
    sid = overview.ref.session_id
    append_message(overview.db, sid, 'user', 'cold stored request')
    overview.authority.sessions.pop(sid)
    before = total_changes(overview.db)
    monkeypatch.setattr(session_local_adoption, 'adopt_before_restore', lambda *a, **k: pytest.fail('adoption write'))
    reply = await request(overview)
    assert 'result' in reply, reply
    assert reply['result']['messages'][0]['content'] == 'cold stored request'
    assert total_changes(overview.db) == before
    assert not overview.connection.subscriptions


@pytest.mark.asyncio
@pytest.mark.parametrize('size', ['rows', 'bytes'])
async def test_oversized_reads_refuse_explicitly_instead_of_returning_a_partial_overview(overview, size):
    sid = overview.ref.session_id
    if size == 'rows':
        append_messages(overview.db, sid, [{'role': 'user', 'content': f'row {index}'} for index in range(20001)])
    else:
        append_message(overview.db, sid, 'user', 'x' * (2 * 1024 * 1024))
    reply = await request(overview)
    assert 'result' not in reply
    assert reply['error']['data']['reason'] == 'overview_too_large'


@pytest.mark.asyncio
async def test_verification_uses_the_source_home_and_workspace_without_writing(overview, monkeypatch):
    from agent import verification_evidence as evidence
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setenv('HERMES_VERIFY_ON_STOP', '1')
    (overview.workspace / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
    ambient = overview.home.parent / 'ambient-profile'
    token = set_hermes_home_override(ambient)
    try:
        evidence.record_terminal_result(command='pytest tests/test_foreign.py', cwd=overview.workspace,
                                        session_id=overview.ref.session_id, exit_code=1, output='ambient secret')
    finally:
        reset_hermes_home_override(token)
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    token = set_hermes_home_override(overview.home)
    try:
        evidence.record_terminal_result(command='pytest tests/test_own.py', cwd=overview.workspace,
                                        session_id=overview.ref.session_id, exit_code=0, output='own passed')
    finally:
        reset_hermes_home_override(token)
    path = overview.home / 'verification_evidence.db'
    before = path.read_bytes()
    ambient_path = ambient / 'verification_evidence.db'
    ambient_before = ambient_path.read_bytes()
    reply = await request(overview)
    assert 'result' in reply, reply
    result = reply['result']
    assert result['verification_cwd'] == str(overview.workspace)
    assert result['verification_error'] is None
    assert result['verification']['evidence']['command'] == 'pytest tests/test_own.py'
    assert result['verification']['status'] == 'passed'
    assert 'ambient secret' not in json.dumps(result)
    assert path.read_bytes() == before
    assert ambient_path.read_bytes() == ambient_before


@pytest.mark.asyncio
async def test_unavailable_ledger_leaves_the_real_transcript_visible(overview, monkeypatch):
    monkeypatch.setenv('HERMES_VERIFY_ON_STOP', '1')
    (overview.workspace / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
    append_message(overview.db, overview.ref.session_id, 'user', 'real request')
    result = (await request(overview))['result']
    assert result['messages'][0]['content'] == 'real request'
    assert result['verification'] is None
    assert result['verification_error'] == 'Session verification unavailable'
    assert not (overview.home / 'verification_evidence.db').exists()


@pytest.mark.asyncio
async def test_no_session_workspace_never_borrows_process_cwd(overview, monkeypatch):
    from gateway import session_policy
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda *a: SimpleNamespace(cwd=''))
    result = (await request(overview))['result']
    assert result['verification_cwd'] is None
    assert result['verification'] is None
    assert result['verification_error'] == 'Session workspace unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('preflight_detects', [True, False])
async def test_cold_untracked_compression_refuses_without_catchup_or_scheduled_write(overview, monkeypatch, preflight_detects):
    from gateway import session_local_adoption
    from tests.gateway.test_local_receipt_untracked_compression import _compact_like_0_22_2
    sid, db = overview.ref.session_id, overview.db
    _compact_like_0_22_2(db, sid, 'untracked-child')
    overview.authority.sessions.pop(sid)
    before = total_changes(db)
    if not preflight_detects:
        monkeypatch.setattr(session_local_adoption, '_due', lambda *a: None)
    monkeypatch.setattr(session_local_adoption, 'adopt_before_restore', lambda *a, **k: pytest.fail('catchup'))
    monkeypatch.setattr(session_local_adoption, 'schedule_adoption', lambda *a, **k: pytest.fail('scheduled catchup'))
    reply = await request(overview)
    assert reply['error']['data']['reason'] == 'storage_unavailable'
    assert total_changes(db) == before
    assert sid not in overview.authority.sessions


@pytest.mark.asyncio
async def test_unpinned_read_snapshot_refuses_before_cold_restore(overview, monkeypatch):
    from contextlib import contextmanager

    sid = overview.ref.session_id
    overview.authority.sessions.pop(sid)

    @contextmanager
    def unpinned():
        from hermes_state_stored_reads import _ReadCheck
        yield _ReadCheck(lambda: True, pinned=False)

    monkeypatch.setattr(overview.db, 'transcript_read_snapshot', unpinned)
    monkeypatch.setattr(overview.authority, 'authorize', lambda *a, **k: pytest.fail('cold restore ran'))

    reply = await request(overview)

    assert 'result' not in reply
    assert reply['error']['data']['reason'] == 'storage_unavailable'
    assert sid not in overview.authority.sessions


@pytest.mark.asyncio
async def test_authority_store_wins_over_an_ambient_session_database(overview, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    ambient = tmp_path / 'ambient'
    ambient.mkdir()
    with SessionDB(db_path=ambient / 'state.db') as other:
        other.create_session(overview.ref.session_id, source='gui')
        append_message(other,overview.ref.session_id, 'user', 'ambient secret')
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    append_message(overview.db, overview.ref.session_id, 'user', 'authority request')
    result = (await request(overview))['result']
    assert result['messages'][0]['content'] == 'authority request'
    assert 'ambient secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_legacy_display_metadata_is_never_backfilled_by_the_read(overview):
    sid, db = overview.ref.session_id, overview.db
    append_message(db, sid, 'user', 'legacy visible request')
    db._execute_write(lambda conn: conn.execute(
        'UPDATE messages SET display_order=NULL, display_identity=NULL WHERE session_id=?', (sid,)))
    before = total_changes(db)
    assert (await request(overview))['result']['messages'][0]['content'] == 'legacy visible request'
    assert total_changes(db) == before
    assert db._read_one('SELECT display_order FROM messages WHERE session_id=?', (sid,))[0] is None


@pytest.mark.asyncio
async def test_bound_session_profile_route_accepts_null_and_current_profile(overview, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, '_get_profiles_root', lambda: overview.home.parent)
    for profile in (None, overview.home.name):
        reply = await request(overview, {**overview.params, 'profile': profile})
        assert 'result' in reply, (profile, reply)


@pytest.mark.asyncio
async def test_removed_native_todo_copy_does_not_restore_the_archived_plan(overview):
    sid, db = overview.ref.session_id, overview.db
    todo_turn(overview, [{'content': 'removed task'}], 1)
    history = db.get_messages_as_conversation(sid)
    db.archive_and_compact(sid, history, tail_count=len(history))
    newest = db._read_one("SELECT MAX(id) FROM messages WHERE session_id=? AND role='tool'", (sid,))[0]
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0, compacted=0 WHERE id=?', (newest,)))
    assert (await request(overview))['result']['todo_state'] is None


@pytest.mark.asyncio
async def test_model_only_and_multimodal_projection_are_safe(overview):
    sid, db = overview.ref.session_id, overview.db
    append_message(db, sid, 'user', 'internal merged prompt', display_metadata={'model_only': True})
    append_message(db, sid, 'user', [{'type': 'text', 'text': 'visible words'},
                                   {'type': 'image_url', 'image_url': {'url': 'secret image'}}])
    result = (await request(overview))['result']
    assert [row['content'] for row in result['messages']] == ['visible words']
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_wire_contract_validates_the_actual_projection_and_compaction_alias(overview):
    from tui_gateway.contracts.registry import CANONICAL_METHODS
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'summary', compressed_summary=True)
    reply = await request(overview)
    result = CANONICAL_METHODS['session.task_overview'].result.model_validate(reply['result'])
    assert result.model_dump(by_alias=True)['messages'][0]['_compressed_summary'] is True
    params = CANONICAL_METHODS['session.task_overview'].params.model_validate(overview.params)
    assert params.session_id == sid and params.stored_id == sid
