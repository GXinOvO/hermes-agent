import json
from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import append_message, rotate_compression, total_changes

from tests.gateway import test_session_task_overview as overview_fixture

overview = overview_fixture.overview


async def rpc(env, method, params):
    return await env.connection.dispatch({'id': 31, 'method': method, 'params': params})


@pytest.mark.asyncio
async def test_source_resolves_without_attaching_or_writing(overview, monkeypatch):
    from gateway import session_local_adoption
    sid = overview.ref.session_id
    append_message(overview.db, sid, 'user', 'original requirement')
    overview.authority.sessions.pop(sid)
    before = total_changes(overview.db)
    monkeypatch.setattr(session_local_adoption, 'adopt_before_restore', lambda *a, **k: pytest.fail('adoption'))
    monkeypatch.setattr(overview.authority, 'attach', lambda *a, **k: pytest.fail('attach'))
    response = await rpc(overview, 'session.task_source', {'session_id': sid})
    assert 'result' in response, response
    result = response['result']
    assert result == {'session_id': sid, 'stored_session_id': sid, 'resolved_id': sid,
                      'owner': overview.params['expected_owner'], 'authority_epoch': overview.authority.epoch}
    assert total_changes(overview.db) == before
    assert not overview.connection.subscriptions


@pytest.mark.asyncio
async def test_analysis_read_is_empty_without_model_or_store_writes(overview, monkeypatch):
    from agent import oneshot
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: pytest.fail('read called model'))
    before = total_changes(overview.db)
    response = await rpc(overview, 'session.task_analysis', {**overview.params, 'task_id': 't', 'title': 'Build'})
    assert 'result' in response, response
    assert response['result'] == {'session_id': overview.ref.session_id,
                                  'stored_session_id': overview.ref.session_id,
                                  'owner': overview.params['expected_owner'],
                                  'authority_epoch': overview.authority.epoch,
                                  'task_id': 't', 'analysis': None}
    assert total_changes(overview.db) == before


class Transport:
    def __init__(self):
        self.replies = asyncio.Queue()

    async def write_async(self, frame):
        await self.replies.put(frame)


async def refresh(env, **extra):
    env.connection.transport = transport = Transport()
    response = await rpc(env, 'session.task_analysis', {**env.params, 'task_id': 't', 'title': 'Build',
                                                       'refresh': True, **extra})
    if response is not None:
        return response
    return await asyncio.wait_for(transport.replies.get(), 5)


def answer(rows):
    user = next(row for row in rows if row['role'] == 'user')
    tool = next(row for row in rows if row['role'] == 'tool')
    return {'status': 'awaiting_acceptance',
            'goal': {'text': 'Build the feature', 'evidence': [{'row_id': user['row_id'], 'quote': user['content']}]},
            'completed': [{'text': 'Tests passed', 'evidence': [{'row_id': tool['row_id'], 'quote': '3 passed'}]}],
            'remaining': [], 'needs_user': [], 'limitations': []}


@pytest.mark.asyncio
async def test_refresh_calls_session_model_saves_evidence_and_reopens_stale(overview, monkeypatch):
    from agent import oneshot
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    db, sid = overview.db, overview.ref.session_id
    overview.authority.runner.config = SimpleNamespace(multiplex_profiles=True)
    append_message(db, sid, 'system', 'system secret')
    append_message(db, sid, 'user', 'hidden secret', display_kind='hidden')
    append_message(db, sid, 'user', 'Build and test the feature')
    append_message(db, sid, 'assistant', 'I finished', reasoning='reasoning secret')
    append_message(db, sid, 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    captured = []
    from gateway import run_turn_prepare
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda runner, policy, **k: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))

    def model(**kwargs):
        assert get_hermes_home() == overview.home
        assert kwargs['main_runtime']['model'] == 'frozen'
        assert 'secret' not in kwargs['user_input']
        assert 'latest' in kwargs['instructions'].lower()
        captured.append(kwargs)
        return json.dumps(answer(json.loads(kwargs['user_input'])['messages']))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    response = await refresh(overview)
    assert 'result' in response, response
    analysis = response['result']['analysis']
    assert analysis is not None
    assert analysis['scope'] == 'single_conversation' and analysis['version'] == 1
    assert analysis['title'] == 'Build' and analysis['stale'] is False
    assert type(analysis['analyzed_at']) is int
    assert analysis['message_count'] == analysis['analyzed_messages'] == 3
    assert analysis['completed'][0]['evidence'][0]['quote'] == '3 passed'
    with open_persistence(profile_scope(home=Path(overview.home))) as fresh:
        records = fresh.state_meta.list_prefix('gateway.task_analysis.v1:')
        assert len(records) == 1
        assert json.loads(records[0].value)['analysis'] == analysis
    before = total_changes(db)
    read = await rpc(overview, 'session.task_analysis', {**overview.params, 'task_id': 't', 'title': 'Build'})
    assert read['result']['analysis'] == analysis
    assert total_changes(db) == before and len(captured) == 1
    append_message(db, sid, 'user', 'Please also check the docs')
    stale = await rpc(overview, 'session.task_analysis', {**overview.params, 'task_id': 't', 'title': 'Build'})
    assert stale['result']['analysis']['stale'] is True
    renamed = await rpc(overview, 'session.task_analysis', {**overview.params, 'task_id': 't', 'title': 'Renamed'})
    assert renamed['result']['analysis'] is None
    assert not overview.connection.subscriptions


@pytest.mark.asyncio
async def test_source_accepts_legacy_lineage_and_guards_owner(overview):
    from gateway.session_controls import AuthorityConnection
    db = overview.db
    overview.authority._native_legacy_transports = {overview.connection.actor.transport_id: overview.connection.actor}
    db.sessions.create(NewSession('legacy-root', source='cli'))
    db.sessions.create(NewSession('legacy-tip', source='cli', parent_session_id='legacy-root'))
    db.sessions.end('legacy-root', 'compression')
    append_message(db, 'legacy-tip', 'user', 'Continue the requested feature')
    before = total_changes(db)
    source = await rpc(overview, 'session.task_source', {'session_id': 'legacy-tip',
                                                       'expected_owner': overview.params['expected_owner']})
    assert 'result' in source, source
    assert source['result']['session_id'] == 'legacy-tip'
    assert source['result']['stored_session_id'] == source['result']['resolved_id'] == 'legacy-tip'
    assert total_changes(db) == before
    for owner in ({**overview.params['expected_owner'], 'store_instance_id': 'other'},
                  {**overview.params['expected_owner'], 'authority_epoch': True}):
        denied = await rpc(overview, 'session.task_source', {'session_id': 'legacy-tip', 'expected_owner': owner})
        assert 'error' in denied
    denied = await rpc(overview, 'session.task_source', {'session_id': 'legacy-tip', 'profile': 'not-this-profile'})
    assert 'error' in denied
    db.sessions.create(NewSession('private', source='cli', user_id='foreign-principal'))
    denied = await rpc(overview, 'session.task_source', {'session_id': 'private'})
    assert 'error' in denied
    connection = AuthorityConnection(overview.authority, object(), {'user_id': 'owner', 'capabilities': []})
    denied = await connection.dispatch({'id': 1, 'method': 'session.task_source', 'params': {'session_id': 'legacy-tip'}})
    assert denied['error']['data']['reason'] == 'permission_denied'
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['boolean_id', 'fake_quote', 'missing_evidence', 'done', 'assistant_completed', 'malformed', 'unsupported_status'])
async def test_invalid_analysis_preserves_success(overview, monkeypatch, bad):
    from agent import oneshot
    from gateway import run_turn_prepare
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'Build and test the feature')
    append_message(db, sid, 'assistant', 'I finished')
    append_message(db, sid, 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda *a, **k: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: json.dumps(answer(json.loads(k['user_input'])['messages'])))
    first = await refresh(overview)
    assert 'result' in first, first
    before = db.state_meta.list_prefix('gateway.task_analysis.v1:')

    def model(**kwargs):
        rows = json.loads(kwargs['user_input'])['messages']
        value = answer(rows)
        if bad == 'malformed':
            return 'not JSON'
        assistant = next(row for row in rows if row['role'] == 'assistant')
        actions = {
            'boolean_id': lambda: value['goal']['evidence'][0].__setitem__('row_id', True),
            'fake_quote': lambda: value['goal']['evidence'][0].__setitem__('quote', 'not in the source'),
            'missing_evidence': lambda: value['goal'].__setitem__('evidence', []),
            'done': lambda: value.__setitem__('status', 'done'),
            'assistant_completed': lambda: value['completed'][0].__setitem__(
                'evidence', [{'row_id': assistant['row_id'], 'quote': assistant['content']}]),
            'unsupported_status': lambda: value.update(goal=None, completed=[], remaining=[], needs_user=[]),
        }
        actions[bad]()
        return json.dumps(value)

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    invalid = await refresh(overview)
    assert invalid['error']['data']['reason'] == 'invalid_analysis', invalid
    assert db.state_meta.list_prefix('gateway.task_analysis.v1:') == before


@pytest.mark.asyncio
async def test_model_cannot_run_before_source_permission_or_owner_checks(overview, monkeypatch):
    from agent import oneshot
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: pytest.fail('model ran before permission checks'))
    for params in ({'stored_id': 'missing'}, {'session_id': 'missing'},
                   {'expected_owner': {**overview.params['expected_owner'], 'store_instance_id': 'other'}}):
        refused = await refresh(overview, **params)
        assert 'error' in refused
    assert overview.db.state_meta.list_prefix('gateway.task_analysis.v1:') == ()


@pytest.mark.asyncio
async def test_unrestored_legacy_analysis_uses_saved_main_route_without_adoption(overview, monkeypatch):
    from agent import oneshot
    from hermes_cli import runtime_provider
    db = overview.db
    overview.authority._native_legacy_transports = {overview.connection.actor.transport_id: overview.connection.actor}
    db.sessions.create(NewSession('old', source='cli', model='legacy-main', model_config={
        'provider': 'custom', 'base_url': 'https://legacy.invalid/v1'}))
    append_message(db, 'old', 'user', 'Build and test the feature')
    append_message(db, 'old', 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return {'provider': 'custom', 'base_url': 'https://legacy.invalid/v1', 'api_key': 'fixture'}

    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', resolve)
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: json.dumps(answer(json.loads(k['user_input'])['messages'])))
    before = db.sessions.get('old')
    result = await refresh(overview, session_id='old', stored_id='old')
    assert 'result' in result, result
    assert result['result']['analysis']['completed']
    assert calls[0]['target_model'] == 'legacy-main'
    assert db.sessions.get('old') == before
    assert 'old' not in overview.authority.sessions


@pytest.mark.asyncio
async def test_cold_readonly_session_uses_its_saved_main_route(overview, monkeypatch):
    from agent import oneshot
    from hermes_cli import runtime_provider
    db = overview.db
    overview.authority._native_legacy_transports = {overview.connection.actor.transport_id: overview.connection.actor}
    db.sessions.create(NewSession('cold', source='cron', model='cold-main', model_config={
        'provider': 'custom', 'base_url': 'https://cold.invalid/v1'}))
    append_message(db, 'cold', 'user', 'Build and test the feature')
    append_message(db, 'cold', 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', lambda **k: {
        'provider': 'custom', 'base_url': 'https://cold.invalid/v1', 'api_key': 'fixture'})
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: json.dumps(answer(json.loads(k['user_input'])['messages'])))
    result = await refresh(overview, session_id='cold', stored_id='cold')
    assert 'result' in result, result
    assert result['result']['analysis']['completed']


@pytest.mark.asyncio
async def test_bounded_input_keeps_original_and_latest_requests_and_marks_tool_truncation(overview, monkeypatch):
    from agent import oneshot
    from gateway import run_turn_prepare
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'Original: implement the feature')
    for index in range(12):
        append_message(db, sid, 'tool', f'Tool output {index}: ' + 'x' * 15000, tool_name='terminal', tool_call_id=str(index))
    append_message(db, sid, 'user', 'Latest: cancel publishing; only test locally')
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda *a, **k: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))

    def model(**kwargs):
        payload = json.loads(kwargs['user_input'])
        contents = [row['content'] for row in payload['messages']]
        assert contents[0] == 'Original: implement the feature'
        assert contents[-1] == 'Latest: cancel publishing; only test locally'
        assert any('tool' in item and 'truncated' in item for item in payload['limitations'])
        return json.dumps({'status': 'todo', 'goal': None, 'completed': [], 'remaining': [],
                           'needs_user': [], 'limitations': []})

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    result = await refresh(overview)
    assert 'result' in result, result
    analysis = result['result']['analysis']
    assert analysis['analyzed_messages'] < analysis['message_count'] == 14
    assert any('Bounded coverage' in item for item in analysis['limitations'])


@pytest.mark.asyncio
async def test_describe_advertises_analysis_and_main_resolution_failure_never_calls_model(overview, monkeypatch):
    from agent import oneshot
    from gateway import run_turn_prepare
    described = await rpc(overview, 'runtime.describe', {})
    assert 'task-analysis-v1' in described['result']['capabilities']
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: pytest.fail('fallback model called'))

    def unavailable(*args, **kwargs):
        assert kwargs['fallback'] is False
        raise RuntimeError('primary unavailable')

    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime', unavailable)
    response = await refresh(overview)
    assert response['error']['data']['reason'] == 'analysis_failed'
    assert overview.db.state_meta.list_prefix('gateway.task_analysis.v1:') == ()


@pytest.mark.asyncio
async def test_compacted_source_resolves_root_and_analyzes_original_and_tool_rows(overview, monkeypatch):
    from agent import oneshot
    from gateway import run_turn_prepare
    db, sid = overview.db, overview.ref.session_id
    append_message(db, sid, 'user', 'Original request')
    append_message(db, sid, 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    assert db.coordination.try_acquire_compression_lock(session_id=sid, holder='analysis')
    try:
        rotate_compression(db, sid, 'analysis-tip', 'gui',
                           [{'role': 'user', 'content': 'Current requirement'}], holder='analysis')
    finally:
        db.coordination.release_compression_lock(session_id=sid, holder='analysis')
    before = total_changes(db)
    source = await rpc(overview, 'session.task_source', {'session_id': 'analysis-tip'})
    assert 'result' in source, source
    assert source['result']['session_id'] == sid
    assert source['result']['stored_session_id'] == source['result']['resolved_id'] == 'analysis-tip'
    assert total_changes(db) == before
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda *a, **k: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))

    def model(**kwargs):
        rows = json.loads(kwargs['user_input'])['messages']
        assert rows[0]['content'] == 'Original request'
        assert rows[-1]['content'] == 'Current requirement'
        return json.dumps(answer(rows))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    result = await refresh(overview, stored_id='analysis-tip')
    assert 'result' in result, result
    assert result['result']['stored_session_id'] == 'analysis-tip'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['session.task_source', 'session.task_analysis'])
async def test_retired_database_epoch_refuses_reads_before_model(overview, monkeypatch, method):
    from agent import oneshot
    from hermes_state_runtime import begin_runtime_epoch
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **k: pytest.fail('stale epoch model call'))
    begin_runtime_epoch(overview.db, instance_id='replacement')
    params = ({'session_id': overview.ref.session_id} if method == 'session.task_source'
              else {**overview.params, 'task_id': 't', 'title': 'Build'})
    response = await rpc(overview, method, params)
    assert 'error' in response, response
    assert response['error']['data']['reason'] == 'stale_epoch'
