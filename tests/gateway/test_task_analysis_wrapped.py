import json

import pytest

from tests.gateway import test_task_analysis_linked as fixtures
from tests.gateway.test_task_analysis_linked import linked_answer, run_linked

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['tool_call', 'execute_code'])
async def test_native_wrapped_explicit_session_read_is_followed_without_parsing_code(linked, monkeypatch, wrapper):
    from agent import oneshot
    db, sid = linked.db, linked.ref.session_id
    db.create_session('wrapped', source='subagent')
    db.append_message('wrapped', 'tool', 'Wrapped source test passed', tool_name='terminal', tool_call_id='test')
    read = {'success': True, 'mode': 'read', 'session_id': 'wrapped', 'link': '@session:default/wrapped',
            'messages': [], 'message_count': 1, 'truncated': False}
    args = {'calls': [{'name': 'session_search', 'arguments': {'session_id': 'wrapped'}}]} if wrapper == 'tool_call' else {'code': 'opaque program'}
    result = read if wrapper == 'tool_call' else {'status': 'success', 'exit_code': 0,
                'output': json.dumps(read), 'tool_calls_made': 1}
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'wrapped-call', 'type': 'function',
                      'function': {'name': wrapper, 'arguments': json.dumps(args)}}])
    db.append_message(sid, 'tool', json.dumps(result), tool_name=wrapper, tool_call_id='wrapped-call')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert 'result' in result, result
    source = next(source for source in result['result']['analysis']['sources'] if source['stored_session_id'] == 'wrapped')
    assert source['relation'] == 'read' and source['status'] == 'read'


@pytest.mark.asyncio
async def test_search_discovery_and_failed_read_do_not_import_search_hit_tasks(linked, monkeypatch):
    from agent import oneshot
    db, sid = linked.db, linked.ref.session_id
    for target in ('search-hit', 'failed-read'):
        db.create_session(target, source='subagent')
        db.append_message(target, 'user', 'Unrelated task should not be added')
    for key, args, value in [
        ('discover', {'query': 'unrelated'}, {'success': True, 'mode': 'discover', 'results': [{'session_id': 'search-hit'}]}),
        ('failed', {'session_id': 'failed-read'}, {'success': False, 'mode': 'read', 'session_id': 'failed-read', 'error': 'permission_denied'})
    ]:
        db.append_message(sid, 'assistant', '', tool_calls=[{'id': key, 'type': 'function',
                          'function': {'name': 'session_search', 'arguments': json.dumps(args)}}])
        db.append_message(sid, 'tool', json.dumps(value), tool_name='session_search', tool_call_id=key)
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    result = await run_linked(linked, refresh=True)
    assert 'result' in result, result
    assert not any(source['stored_session_id'] in ('search-hit', 'failed-read') for source in result['result']['analysis']['sources'])


@pytest.mark.asyncio
async def test_legacy_v1_result_survives_until_v2_is_generated(linked, monkeypatch):
    from agent import oneshot
    from gateway.session_task_analysis import _key
    from tests.gateway.test_session_task_analysis import rpc
    sid = linked.ref.session_id
    linked.db.append_message(sid, 'tool', 'primary check passed', tool_name='terminal', tool_call_id='check')
    def v1_model(**kwargs):
        row = next(row for row in json.loads(kwargs['user_input'])['messages'] if row['role'] == 'user')
        return json.dumps({'status': 'waiting', 'goal': {'text': 'Release',
                           'evidence': [{'row_id': row['row_id'], 'quote': row['content']}]},
                           'completed': [], 'remaining': [], 'needs_user': [], 'limitations': []})

    monkeypatch.setattr(oneshot, 'run_oneshot', v1_model)
    value = await rpc(linked, 'session.task_analysis', {**linked.params, 'task_id': 'linked', 'title': 'Release', 'refresh': True})
    assert value is None
    old = (await linked.connection.transport.replies.get())['result']['analysis']
    before = linked.db.get_meta(_key(sid, 'linked'))
    read = await run_linked(linked)
    cached = read['result']['analysis']
    assert cached['version'] == 1 and cached['scope'] == 'single_conversation'
    assert cached['stale'] is True
    assert {**cached, 'stale': old['stale']} == old
    assert linked.db.get_meta(_key(sid, 'linked')) == before
    linked.db.append_message('related', 'user', 'A related source changed')
    again = await run_linked(linked)
    assert again['result']['analysis']['stale'] is True
    assert linked.db.get_meta(_key(sid, 'linked')) == before
