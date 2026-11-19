import json

import pytest

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession

from tests.gateway import test_task_analysis_linked as fixtures

linked = fixtures.linked
overview = fixtures.overview


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['direct', 'execute_code'])
async def test_scroll_without_provenance_never_claims_complete_coverage(linked, monkeypatch, wrapper):
    from agent import oneshot
    from tools.session_search_tool import session_search

    db, sid = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('unresolved-scroll', source='cli'))
    anchor = db.messages.append('unresolved-scroll', NewMessage(role='user', content='A private historical requirement'))
    args = {'session_id': 'unresolved-scroll', 'around_message_id': anchor}
    native = session_search(db=db, current_session_id=sid, **args)
    scroll = json.loads(native)
    assert scroll['success'] is True and scroll['mode'] == 'scroll' and 'link' not in scroll
    assert any(row['content'] == 'A private historical requirement' for row in scroll['messages'])
    name = 'session_search' if wrapper == 'direct' else 'execute_code'
    call_args = args if wrapper == 'direct' else {'code': 'historical wrapped output'}
    output = native if wrapper == 'direct' else json.dumps({'status': 'success', 'exit_code': 0,
                                                             'output': native, 'tool_calls_made': 1})
    db.messages.append(sid, NewMessage(role='assistant', content='', tool_calls=[
        {'id': 'scroll-output', 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(call_args)}}]))
    output_row = db.messages.append(sid, NewMessage(role='tool', content=output, tool_name=name,
                                                    tool_call_id='scroll-output'))
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    response = await fixtures.run_linked(linked, refresh=True)
    assert 'result' in response, response
    analysis = response['result']['analysis']
    sources = [source for source in analysis['sources'] if source['stored_session_id'] == 'unresolved-scroll']
    if wrapper == 'direct':
        assert len(sources) == 1 and sources[0]['status'] == 'read'
        assert analysis['complete'] is True
        assert any(row['source_id'] == sources[0]['id'] and row['content'] == 'A private historical requirement'
                   for row in captured[0]['messages'])
    else:
        assert sources == []
        assert all('A private historical requirement' not in row['content'] for row in captured[0]['messages'])
        assert analysis['complete'] is False
        assert any('s0' in item and str(output_row) in item for item in analysis['limitations'])
        assert any('s0' in item and str(output_row) in item for item in captured[0]['limitations'])
    cached = await fixtures.run_linked(linked)
    assert cached['result']['analysis'] == analysis


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['session_search', 'tool_call', 'execute_code'])
@pytest.mark.parametrize('mode', ['read', 'scroll'])
@pytest.mark.parametrize('success', ['missing', None, 1, 'true', True])
async def test_read_success_requires_native_boolean(linked, monkeypatch, wrapper, mode, success):
    from agent import oneshot
    from tools.session_search_tool import session_search, _session_link

    db, sid = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('malformed-read', source='cli'))
    anchor = db.messages.append('malformed-read', NewMessage(role='user', content='A private malformed result'))
    args = {'session_id': 'malformed-read', **({'around_message_id': anchor} if mode == 'scroll' else {})}
    read = json.loads(session_search(db=db, current_session_id=sid, **args))
    assert read['success'] is True and read['mode'] == mode
    if wrapper == 'execute_code':
        read['link'] = _session_link('malformed-read', 'default')
    if success == 'missing':
        read.pop('success')
    else:
        read['success'] = success
    call_args = args
    if wrapper == 'tool_call':
        call_args = {'name': 'session_search', 'arguments': args}
    if wrapper == 'execute_code':
        call_args = {'code': 'historical wrapped output'}
    output = read if wrapper != 'execute_code' else {'status': 'success', 'exit_code': 0, 'output': json.dumps(read), 'tool_calls_made': 1}
    db.messages.append(sid, NewMessage(role='assistant', content='', tool_calls=[
        {'id': 'malformed-read', 'type': 'function',
         'function': {'name': wrapper, 'arguments': json.dumps(call_args)}}]))
    output_row = db.messages.append(sid, NewMessage(role='tool', content=json.dumps(output),
                                                    tool_name=wrapper, tool_call_id='malformed-read'))
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    analysis = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    assert analysis['complete'] is (success is True)
    sources = [source for source in analysis['sources'] if source['stored_session_id'] == 'malformed-read']
    assert bool(sources) is (success is True)
    assert any('A private malformed result' in row['content'] for row in captured[0]['messages']) is (success is True)
    assert all(row['source_id'] != 's0' or row['row_id'] != output_row for row in captured[0]['messages'])
    if success is not True:
        assert any('s0' in item and str(output_row) in item for item in analysis['limitations'])


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['read', 'scroll'])
@pytest.mark.parametrize('status', ['error', 'timeout', 'interrupted', None, 'success'])
async def test_execute_outer_status_controls_copy_removal(linked, monkeypatch, mode, status):
    from agent import oneshot
    from gateway.session_task_analysis_input import stored_rows
    from gateway.session_task_relations import copied_session_results
    from tools.session_search_tool import session_search, _session_link

    db, sid = linked.db, linked.ref.session_id
    db.sessions.create(NewSession('outer-result', source='cli'))
    anchor = db.messages.append('outer-result', NewMessage(role='user', content='Private outer-result evidence'))
    args = {'session_id': 'outer-result', **({'around_message_id': anchor} if mode == 'scroll' else {})}
    payload = json.loads(session_search(db=db, current_session_id=sid, **args))
    assert payload['success'] is True and payload['mode'] == mode
    payload['link'] = _session_link('outer-result', 'default')
    result = {'status': status, 'output': json.dumps(payload), 'tool_calls_made': 1}
    db.messages.append(sid, NewMessage(role='assistant', content='', tool_calls=[
        {'id': 'outer', 'type': 'function',
         'function': {'name': 'execute_code', 'arguments': '{}'}}]))
    row_id = db.messages.append(sid, NewMessage(role='tool', content=json.dumps(result),
                                                tool_name='execute_code', tool_call_id='outer'))
    copied = copied_session_results(stored_rows(db, sid))
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        return json.dumps(fixtures.linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    analysis = (await fixtures.run_linked(linked, refresh=True))['result']['analysis']
    assert analysis['complete'] is (status == 'success')
    assert any(source['stored_session_id'] == 'outer-result' for source in analysis['sources']) is (status == 'success')
    assert all(row['source_id'] != 's0' or row['row_id'] != row_id for row in captured[0]['messages'])
    assert any('Private outer-result evidence' in row['content'] for row in captured[0]['messages']) is (status == 'success')
    if status != 'success':
        assert any('s0' in item and str(row_id) in item for item in analysis['limitations'])
    assert (row_id in copied) is (status == 'success')
