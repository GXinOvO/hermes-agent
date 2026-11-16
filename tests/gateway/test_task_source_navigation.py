import asyncio
import json

import pytest

from tests.gateway import test_session_task_overview as overview_fixture

overview = overview_fixture.overview


async def rpc(env, method, params):
    return await env.connection.dispatch({'id': 41, 'method': method, 'params': params})


@pytest.mark.asyncio
async def test_legacy_compressed_source_opens_and_keeps_analysis_after_adoption(overview, monkeypatch):
    from agent import oneshot
    from gateway import session_task_analysis_runtime
    from tests.gateway.test_session_task_analysis import Transport
    db = overview.db
    overview.authority._native_legacy_transports = {overview.connection.actor.transport_id: overview.connection.actor}
    db.create_session('legacy-root', source='desktop', model='frozen')
    db.append_message('legacy-root', 'user', 'Keep the original requirement')
    assert db.try_acquire_compression_lock('legacy-root', 'task-source')
    try:
        db.publish_compression_child(parent_session_id='legacy-root', child_session_id='legacy-tip',
                                     source='desktop', model='frozen',
                                     messages=[{'role': 'user', 'content': 'Continue the original requirement'}],
                                     compression_lock_holder='task-source')
    finally:
        db.release_compression_lock('legacy-root', 'task-source')
    params = {'session_id': 'legacy-tip', 'expected_owner': overview.params['expected_owner']}
    before = db._conn.total_changes
    source = (await rpc(overview, 'session.task_source', params))['result']
    assert source['session_id'] == source['stored_session_id'] == source['resolved_id'] == 'legacy-tip'
    assert db._conn.total_changes == before
    assert not overview.connection.subscriptions
    monkeypatch.setattr(session_task_analysis_runtime, 'runtime_for', lambda *args: lambda: {'model': 'frozen', 'provider': 'custom'})
    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        user = json.loads(kwargs['user_input'])['messages'][0]
        return json.dumps({'status': 'waiting', 'goal': {'text': 'Keep the requirement',
                           'evidence': [{'row_id': user['row_id'], 'quote': user['content']}]},
                           'completed': [], 'remaining': [], 'needs_user': [], 'limitations': []})

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    overview.connection.transport = transport = Transport()
    analysis_params = {**params, 'session_id': source['session_id'], 'stored_id': 'legacy-tip',
                       'task_id': 'matter', 'title': 'Original requirement', 'refresh': True}
    assert await rpc(overview, 'session.task_analysis', analysis_params) is None
    analyzed = await asyncio.wait_for(transport.replies.get(), 5)
    assert 'result' in analyzed, analyzed
    saved = db.list_meta_prefix('gateway.task_analysis.v1:')
    resumed = await rpc(overview, 'session.resume', {**params, 'session_id': source['session_id']})
    assert 'result' in resumed, resumed
    assert resumed['result']['session_id'] == resumed['result']['stored_session_id'] == source['session_id']
    before = db._conn.total_changes
    reread_source = await rpc(overview, 'session.task_source', params)
    assert reread_source['result'] == source
    cached = await rpc(overview, 'session.task_analysis', {**analysis_params, 'refresh': False})
    assert cached['result']['analysis'] == analyzed['result']['analysis']
    assert db.list_meta_prefix('gateway.task_analysis.v1:') == saved
    assert db._conn.total_changes == before
    assert len(calls) == 1
