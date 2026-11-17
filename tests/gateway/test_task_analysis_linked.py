import asyncio
import json
import threading

import pytest

from tests.gateway import test_session_task_overview as overview_fixture
from tests.gateway.test_session_task_analysis import Transport, rpc
from tests.waiting import async_wait_until

overview = overview_fixture.overview


def seed_read(env, target='related', profile=None):
    db, sid = env.db, env.ref.session_id
    db.append_message(sid, 'assistant', '', tool_calls=[{'id': 'read-old', 'type': 'function',
        'function': {'name': 'session_search', 'arguments': json.dumps({'session_id': target, **({'profile': profile} if profile else {})})}}])
    db.append_message(sid, 'tool', json.dumps({'success': True, 'mode': 'read', 'session_id': target,
                      'link': f'@session:{profile or "default"}/{target}', 'truncated': False,
                      'message_count': 1, 'messages': []}), tool_name='session_search', tool_call_id='read-old')


def linked_answer(prompt):
    rows = prompt['messages']
    primary = next(row for row in rows if row['source_id'] == 's0' and row['role'] == 'user')
    related = next(row for row in rows if row['source_id'] != 's0' and row['role'] == 'tool')
    return {'status': 'awaiting_acceptance', 'goal': {'text': 'Complete the release',
            'evidence': [{'source_id': primary['source_id'], 'row_id': primary['row_id'], 'quote': primary['content']}]},
            'completed': [{'text': 'The related check passed', 'evidence': [{'source_id': related['source_id'],
                           'row_id': related['row_id'], 'quote': related['content']}]}],
            'remaining': [], 'needs_user': [], 'limitations': []}


@pytest.fixture
def linked(overview, monkeypatch):
    from gateway import run_turn_prepare
    overview.authority._native_legacy_transports = {overview.connection.actor.transport_id: overview.connection.actor}
    db, sid = overview.db, overview.ref.session_id
    db.append_message(sid, 'user', 'Complete the release; acceptance remains manual')
    db.create_session('related', source='subagent')
    db.append_message('related', 'user', 'Check the release')
    db.append_message('related', 'tool', '3 passed', tool_name='terminal', tool_call_id='check')
    seed_read(overview)
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda *args, **kwargs: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))
    overview.connection.transport = Transport()
    return overview


async def run_linked(env, **extra):
    params = {**env.params, 'task_id': 'linked', 'title': 'Release', 'follow_sources': True, **extra}
    response = await rpc(env, 'session.task_analysis', params)
    return response if response is not None else await asyncio.wait_for(env.connection.transport.replies.get(), 8)


@pytest.mark.asyncio
async def test_rpc_automatically_reads_related_native_rows_and_caches_without_manual_source_list(linked, monkeypatch):
    from agent import oneshot
    captured = []

    def model(**kwargs):
        prompt = json.loads(kwargs['user_input'])
        captured.append(prompt)
        assert 'latest' in kwargs['instructions'].lower()
        assert 'primary' in kwargs['instructions'].lower()
        return json.dumps(linked_answer(prompt))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    response = await run_linked(linked, refresh=True)
    assert 'result' in response, response
    analysis = response['result']['analysis']
    assert analysis['version'] == 2 and analysis['scope'] == 'linked_conversations' and analysis['complete'] is True
    assert len(analysis['sources']) == 2
    source = analysis['sources'][1]
    assert source['stored_session_id'] == 'related' and source['relation'] == 'read'
    assert source['via_source_id'] == 's0' and source['via_row_id'] > 0
    assert analysis['completed'][0]['evidence'][0]['source_id'] == source['id']
    before = linked.db._conn.total_changes
    cached = await run_linked(linked, refresh=False)
    assert cached['result']['analysis'] == analysis
    assert len(captured) == 1 and linked.db._conn.total_changes == before
    assert not linked.connection.subscriptions
    linked.db.append_message('related', 'user', 'One more check is needed')
    stale = await run_linked(linked)
    assert stale['result']['analysis']['stale'] is True


@pytest.mark.asyncio
async def test_missing_related_source_is_visible_incomplete_not_fabricated(linked, monkeypatch):
    from agent import oneshot
    linked.db.append_message(linked.ref.session_id, 'user', 'Also consult @session:default/missing-source')
    monkeypatch.setattr(oneshot, 'run_oneshot', lambda **kwargs: json.dumps(linked_answer(json.loads(kwargs['user_input']))))
    response = await run_linked(linked, refresh=True)
    assert 'result' in response, response
    analysis = response['result']['analysis']
    assert analysis['complete'] is False
    missing = next(source for source in analysis['sources'] if source['stored_session_id'] == 'missing-source')
    assert missing['status'] == 'unavailable' and missing['owner'] is None and missing['reason']
    assert analysis['limitations']


@pytest.mark.asyncio
async def test_related_change_while_model_runs_cannot_commit_late_result(linked, monkeypatch):
    from agent import oneshot
    gate, entered = threading.Event(), threading.Event()

    def model(**kwargs):
        entered.set()
        gate.wait(8)
        return json.dumps(linked_answer(json.loads(kwargs['user_input'])))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    try:
        response = await rpc(linked, 'session.task_analysis', {**linked.params, 'task_id': 'late', 'title': 'Release',
                                                             'refresh': True, 'follow_sources': True})
        assert response is None, response
        await async_wait_until(entered.is_set)
        linked.db.append_message('related', 'user', 'Updated requirement during analysis')
        before = linked.db.list_meta_prefix('gateway.task_analysis.v2:')
        gate.set()
        failed = await asyncio.wait_for(linked.connection.transport.replies.get(), 8)
        assert failed['error']['data']['reason'] in ('revision_conflict', 'session_source_changed')
        assert linked.db.list_meta_prefix('gateway.task_analysis.v2:') == before
    finally:
        gate.set()
        await linked.connection.close()
