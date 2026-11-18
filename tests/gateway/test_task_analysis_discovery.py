import json

import pytest

from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, open_persistence_reader, profile_scope, store_location
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import append_message, append_messages, seed_session, total_changes

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['direct', 'tool_call', 'execute_code'])
async def test_search_results_with_messages_are_recalled_and_versioned(linked, monkeypatch, wrapper):
    from agent import oneshot
    from tools.session_search_tool import session_search

    db, sid = linked.db, linked.ref.session_id
    for target, text in [('discovered-context', 'Real SSH acceptance is required'),
                         ('discovered-other', 'A separate task remains independent')]:
        db.sessions.create(NewSession(target, source='cli'))
        append_message(db, target, 'user', f'Recalluniqueneedle: {text}')
        append_message(db, target, 'assistant', f'Evidence for {text}')
    args = {'query': 'Recalluniqueneedle', 'limit': 2}
    native_result = session_search(db=db, current_session_id=sid, **args)
    discovery = json.loads(native_result)
    assert discovery['success'] is True and discovery['mode'] == 'discover'
    hits = discovery['results']
    assert {hit['session_id'] for hit in hits} == {'discovered-context', 'discovered-other'}
    assert {hit['detail'] for hit in hits} == {'full', 'compact'}
    assert all(hit['messages'] for hit in hits)
    if wrapper == 'direct':
        name, call_args, result = 'session_search', args, discovery
    elif wrapper == 'tool_call':
        name, call_args, result = 'tool_call', {'calls': [{'name': 'session_search', 'arguments': args}]}, discovery
    else:
        name, call_args = 'execute_code', {'code': 'opaque program'}
        result = {'status': 'success', 'exit_code': 0, 'output': native_result, 'tool_calls_made': 1}
    append_message(db, sid, 'assistant', '', tool_calls=[{'id': 'discover-call', 'type': 'function',
                      'function': {'name': name, 'arguments': json.dumps(call_args)}}])
    append_message(db, sid, 'tool', json.dumps(result), tool_name=name, tool_call_id='discover-call')
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    result = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in result, result
    analysis = result['result']['analysis']
    sources = {source['stored_session_id']: source for source in analysis['sources']}
    for hit in hits:
        assert sources[hit['session_id']]['status'] == 'read'
        assert sources[hit['session_id']]['relation'] == 'read'
    source = sources['discovered-context']
    assert any(row['source_id'] == source['id'] and row['content'] == 'Recalluniqueneedle: Real SSH acceptance is required'
               for row in captured[0]['messages'])
    append_message(db, 'discovered-context', 'user', 'The SSH acceptance was updated')
    stale = await fixtures.run_linked(linked)
    assert stale['result']['analysis']['stale'] is True


@pytest.mark.asyncio
async def test_scroll_rebound_to_branch_reads_actual_source(linked, monkeypatch):
    from agent import oneshot
    from tools.session_search_tool import session_search

    db, sid = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('scroll-parent', source='cli'))
    append_message(db, 'scroll-parent', 'user', 'Parent context')
    db.sessions.create(NewSession('scroll-child', source='cli', parent_session_id='scroll-parent',
                                  model_config={'_branched_from': 'scroll-parent'}))
    anchor = append_message(db, 'scroll-child', 'user', 'Actual branched session contains the requirement')
    args = {'session_id': 'scroll-parent', 'around_message_id': anchor}
    native_result = session_search(db=db, current_session_id=sid, **args)
    scroll = json.loads(native_result)
    assert scroll['success'] is True and scroll['mode'] == 'scroll', scroll
    assert scroll['session_id'] == 'scroll-child' and 'rebound' in scroll.get('warning', '')
    append_message(db, sid, 'assistant', '', tool_calls=[{'id': 'scroll-rebound', 'type': 'function',
                      'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
    append_message(db, sid, 'tool', native_result, tool_name='session_search', tool_call_id='scroll-rebound')
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    source = next(source for source in reply['result']['analysis']['sources']
                  if source['stored_session_id'] == 'scroll-child')
    assert source['status'] == 'read'
    assert any(row['source_id'] == source['id'] and row['content'] == 'Actual branched session contains the requirement'
               for row in captured[0]['messages'])


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['joined', 'mixed'])
async def test_joined_delegate_results_recall_owned_children(linked, monkeypatch, shape):
    from agent import oneshot

    db, parent = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('joined-child', source='subagent', parent_session_id=parent,
                                  model_config={'_delegate_from': parent, '_subagent_id': 'sa-joined'}))
    append_message(db, 'joined-child', 'user', 'Joined child requirement')
    entries = [{'task_index': 0, 'status': 'completed', 'subagent_id': 'sa-joined', 'summary': 'done'}]
    value = {'results': entries} if shape == 'joined' else {'status': 'dispatched', 'subagent_ids': [], 'inline_results': entries}
    append_message(db, parent, 'assistant', '', tool_calls=[{'id': 'joined-delegate', 'type': 'function',
                      'function': {'name': 'delegate_task', 'arguments': '{}'}}])
    append_message(db, parent, 'tool', json.dumps(value), tool_name='delegate_task', tool_call_id='joined-delegate')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    result = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in result, result
    assert any(source['stored_session_id'] == 'joined-child' and source['relation'] == 'delegate'
               for source in result['result']['analysis']['sources']), result['result']['analysis']['sources']


@pytest.mark.asyncio
async def test_unrelated_wrapped_tool_cannot_be_mistaken_for_delegation(linked, monkeypatch):
    from agent import oneshot

    db, parent = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('unrelated-child', source='subagent', parent_session_id=parent,
                                  model_config={'_delegate_from': parent, '_subagent_id': 'sa-unrelated'}))
    append_message(db, 'unrelated-child', 'user', 'Not consulted by this call')
    args = {'calls': [{'name': 'some_other_tool', 'arguments': {}}]}
    result = {'results': [{'subagent_id': 'sa-unrelated', 'status': 'completed'}]}
    append_message(db, parent, 'assistant', '', tool_calls=[{'id': 'other-call', 'type': 'function',
                      'function': {'name': 'tool_call', 'arguments': json.dumps(args)}}])
    append_message(db, parent, 'tool', json.dumps(result), tool_name='tool_call', tool_call_id='other-call')
    monkeypatch.setattr(oneshot, 'run_oneshot',
                        lambda **kwargs: json.dumps(fixtures.linked_answer(json.loads(kwargs['user_input']))))
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    assert not any(source['stored_session_id'] == 'unrelated-child' for source in reply['result']['analysis']['sources'])


@pytest.mark.asyncio
async def test_search_result_profile_still_requires_source_authorization(linked, monkeypatch):
    from agent import oneshot

    db, sid = linked.db, linked.ref.session_id
    args = {'query': 'needle', 'profile': 'other'}
    result = {'success': True, 'mode': 'discover', 'results': [{
        'session_id': 'private', 'link': '@session:other/private', 'detail': 'full',
        'messages': [{'id': 100, 'role': 'user', 'content': 'private source'}],
    }]}
    append_message(db, sid, 'assistant', '', tool_calls=[{'id': 'discover-private', 'type': 'function',
                      'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
    append_message(db, sid, 'tool', json.dumps(result), tool_name='session_search', tool_call_id='discover-private')
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    reply = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in reply, reply
    analysis = reply['result']['analysis']
    source = next(source for source in analysis['sources'] if source['stored_session_id'] == 'private')
    assert source['profile'] == 'other' and source['status'] == 'unavailable'
    assert source['reason'] == 'profile_mismatch' and analysis['complete'] is False
    assert not any(row['source_id'] == source['id'] for row in captured[0]['messages'])
    assert all('private source' not in row['content'] for row in captured[0]['messages'])
