import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.gateway import test_task_analysis_linked as fixtures
from tests.gateway.test_task_analysis_linked import linked_answer, run_linked, seed_read

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
async def test_cycles_and_compression_aliases_read_each_conversation_once(linked, monkeypatch):
    from agent import oneshot
    db = linked.db
    db.append_message('related', 'user', f'Return to @session:default/{linked.ref.session_id}')
    assert db.try_acquire_compression_lock('related', 'linked')
    try:
        db.publish_compression_child(parent_session_id='related', child_session_id='related-tip', source='subagent',
                                     messages=[{'role': 'user', 'content': 'Continue checks'}], compression_lock_holder='linked')
    finally:
        db.release_compression_lock('related', 'linked')
    linked.db.append_message(linked.ref.session_id, 'user', 'Also read @session:default/related-tip')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert 'result' in result, result
    analysis = result['result']['analysis']
    assert len(analysis['sources']) == 2 and analysis['complete'] is True
    assert analysis['sources'][1]['resolved_id'] == 'related-tip'


@pytest.mark.asyncio
async def test_delegated_child_uses_durable_parent_marker_not_random_id(linked, monkeypatch):
    from agent import oneshot
    db, parent = linked.db, linked.ref.session_id
    db.create_session('child', source='subagent', parent_session_id=parent,
                      model_config={'_delegate_from': parent, '_subagent_id': 'sa-real'})
    db.append_message('child', 'tool', '2 integration tests passed', tool_name='terminal', tool_call_id='integration')
    db.create_session('unrelated', source='desktop')
    db.create_session('foreign', source='subagent', parent_session_id='unrelated',
                      model_config={'_delegate_from': 'unrelated', '_subagent_id': 'sa-fake'})
    db.append_message('foreign', 'user', 'This belongs to another task')
    db.append_message(parent, 'assistant', '', tool_calls=[{'id': 'dispatch', 'type': 'function',
                      'function': {'name': 'delegate_task', 'arguments': '{}'}}])
    db.append_message(parent, 'tool', json.dumps({'status': 'dispatched', 'subagent_ids': ['sa-real', 'sa-fake']}),
                      tool_name='delegate_task', tool_call_id='dispatch')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert 'result' in result, result
    sources = result['result']['analysis']['sources']
    assert any(source['stored_session_id'] == 'child' and source['relation'] == 'delegate' for source in sources)
    assert not any(source['stored_session_id'] == 'foreign' for source in sources)


@pytest.mark.asyncio
async def test_profile_bound_connection_does_not_read_other_profile_or_scan_fallback(linked, monkeypatch):
    from agent import oneshot
    seed_read(linked, 'secret-session', profile='other')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert 'result' in result, result
    source = next(source for source in result['result']['analysis']['sources'] if source['profile'] == 'other')
    assert source['status'] == 'unavailable' and source['reason'] == 'profile_mismatch'
    assert result['result']['analysis']['complete'] is False


@pytest.mark.asyncio
async def test_host_scoped_connection_uses_served_sibling_and_keeps_same_row_numbers_distinct(linked, monkeypatch):
    from agent import oneshot
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from gateway.session_authority import SessionAuthority
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import begin_runtime_epoch
    from hermes_cli import profiles
    home = linked.home / 'profiles' / 'other'
    home.mkdir(parents=True)
    from hermes_state import SessionDB
    store = SessionStore(home / 'sessions', GatewayConfig())
    db = SessionDB(db_path=home / 'state.db')
    store._db = db
    assert db.db_path != linked.db.db_path
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='other-owner', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='other-owner'))
    registry = SessionAuthorities(linked.home, multiplexed=True)
    registry.add(linked.home, linked.authority)
    registry.add(home, authority, name='other')
    linked.authority.runner.session_authorities = registry
    runner.session_authorities = registry
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home if name == 'other' else linked.home)
    connection = AuthorityConnection(linked.authority, linked.connection.transport,
                                     {'user_id': linked.connection.actor.subject, 'provider': 'local', 'profile_scope': 'host',
                                      'profile_id': linked.authority.profile_id, 'instance_id': linked.authority.instance_id,
                                      'capabilities': list(linked.connection.actor.capabilities)})
    db.create_session('shared', source='subagent')
    db.append_message('shared', 'user', 'Reference-only task. Do not add its todo to the release')
    db.append_message('shared', 'tool', '4 remote tests passed', tool_name='terminal', tool_call_id='remote')
    seed_read(linked, 'shared', profile='other')
    linked.connection = connection
    captured = []

    def model(**kwargs):
        value = json.loads(kwargs['user_input'])
        captured.append(value)
        assert any(source['profile'] == 'other' and source['status'] == 'read' for source in value['sources']), value['sources']
        return json.dumps(linked_answer(value))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    try:
        result = await run_linked(linked, refresh=True)
        assert 'result' in result, result
        sources = result['result']['analysis']['sources']
        source = next(source for source in sources if source['profile'] == 'other')
        assert source['owner']['profile_id'] == str(home) and source['status'] == 'read'
        assert any(row['source_id'] == source['id'] and row['content'] == '4 remote tests passed'
                   for row in captured[0]['messages'])
        before = db.conversation_revision('shared', include_ancestors=True)
        db.append_message('shared', 'user', 'New remote requirement')
        changed = db.conversation_revision('shared', include_ancestors=True)
        assert changed != before
        cached = await run_linked(linked)
        assert cached['result']['analysis']['stale'] is True
        assert db.conversation_revision('shared', include_ancestors=True) == changed
    finally:
        await connection.close()
        db.close()


@pytest.mark.asyncio
async def test_forged_source_or_boolean_row_evidence_is_rejected_and_keeps_success(linked, monkeypatch):
    from agent import oneshot
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    first = await run_linked(linked, refresh=True)
    assert 'result' in first, first
    before = linked.db.list_meta_prefix('gateway.task_analysis.v2:')

    def model(**kwargs):
        value = linked_answer(json.loads(kwargs['user_input']))
        value['completed'][0]['evidence'][0]['source_id'] = 'invented'
        return json.dumps(value)

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    failed = await run_linked(linked, refresh=True)
    assert failed['error']['data']['reason'] == 'invalid_analysis'
    assert linked.db.list_meta_prefix('gateway.task_analysis.v2:') == before


@pytest.mark.asyncio
async def test_bounds_are_visible_and_v1_cache_stays_available(linked, monkeypatch):
    from agent import oneshot
    from gateway import session_task_sources
    monkeypatch.setattr(session_task_sources, 'MAX_SOURCES', 2)
    linked.db.append_message(linked.ref.session_id, 'user', 'Also check @session:default/third')
    linked.db.create_session('third', source='subagent')
    linked.db.append_message('third', 'user', 'More work')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert result['result']['analysis']['complete'] is False
    assert any('limit' in item.lower() for item in result['result']['analysis']['limitations'])
    assert linked.db.list_meta_prefix('gateway.task_analysis.v1:') == []
