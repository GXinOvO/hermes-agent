import json
from types import SimpleNamespace

import pytest

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize(('embedded', 'supplied', 'selected'), [
    ('other', 'default', 'default'), ('default', 'other', 'other'),
    ('other', 'Default', 'default'), ('default', 'Other', 'other'),
    ('Default', None, 'default'), ('Other', None, 'other'),
])
async def test_explicit_profile_selects_native_source_when_ids_collide(linked, monkeypatch, embedded, supplied, selected):
    from agent import oneshot
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import profiles
    from hermes_state_runtime import begin_runtime_epoch
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, open_persistence_reader, profile_scope, store_location
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_seed import append_message
    from tools import session_search_tool

    home = linked.home / 'profiles' / 'other'
    home.mkdir(parents=True)
    store = SessionStore(home / 'sessions', GatewayConfig())
    other = open_persistence(profile_scope(home=home), eager=True)
    store._db = other
    runner = SimpleNamespace(session_store=store, _session_db=other, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='other-owner', persistence=other,
                                 epoch=begin_runtime_epoch(other, instance_id='other-owner'))
    registry = SessionAuthorities(linked.home, multiplexed=True)
    registry.add(linked.home, linked.authority)
    registry.add(home, authority, name='other')
    linked.authority.runner.session_authorities = runner.session_authorities = registry
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home if name == 'other' else linked.home)
    monkeypatch.setattr(profiles, 'profile_exists', lambda name: name in ('default', 'other'))
    monkeypatch.setattr(session_search_tool, '_cross_profile_reads_confined', lambda: False)
    resolve_profile = session_search_tool._resolve_profile_db
    monkeypatch.setattr(session_search_tool, '_resolve_profile_db',
                        lambda name, handles: _default_reader(handles)
                        if profiles.normalize_profile_name(name) == 'default' else resolve_profile(name, handles))
    def _default_reader(handles):
        reader = handles.enter_context(open_persistence_reader(store_location(linked.db.scope)))
        reader.ready()
        return reader
    connection = AuthorityConnection(linked.authority, linked.connection.transport,
                                     {'user_id': linked.connection.actor.subject, 'provider': 'local', 'profile_scope': 'host',
                                      'profile_id': linked.authority.profile_id, 'instance_id': linked.authority.instance_id,
                                      'capabilities': list(linked.connection.actor.capabilities)})
    linked.authority._native_legacy_transports = {connection.actor.transport_id: connection.actor}
    linked.connection = connection
    for db, text in ((linked.db, 'default collision evidence'), (other, 'other collision evidence')):
        db.sessions.create(NewSession('collision', source='cli'))
        append_message(db, 'collision', 'user', text)

    args = {'session_id': f'{embedded}/collision'}
    if supplied is not None:
        args['profile'] = supplied
    native = session_search_tool.session_search(db=linked.db, current_session_id=linked.ref.session_id, **args)
    payload = json.loads(native)
    assert payload['success'], payload
    assert payload['link'] == f'@session:{supplied or embedded}/collision'
    assert payload['messages'][0]['content'] == f'{selected} collision evidence'
    append_message(linked.db, linked.ref.session_id, 'assistant', '', tool_calls=[{'id': 'collision-call', 'type': 'function',
                             'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
    append_message(linked.db, linked.ref.session_id, 'tool', native, tool_name='session_search', tool_call_id='collision-call')
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    try:
        response = await fixtures.run_linked(linked, refresh=True)
        assert 'result' in response, response
        sources = [source for source in response['result']['analysis']['sources'] if source['stored_session_id'] == 'collision']
        assert len(sources) == 1
        source = sources[0]
        assert source['profile'] == selected and source['status'] == 'read'
        assert source['owner']['profile_id'] == (str(home) if selected == 'other' else linked.authority.profile_id)
        contents = [row['content'] for row in captured[0]['messages'] if row['source_id'] == source['id']]
        assert f'{selected} collision evidence' in contents
        if embedded.lower() != selected:
            assert f'{embedded} collision evidence' not in contents
    finally:
        await connection.close()
        other.close()
