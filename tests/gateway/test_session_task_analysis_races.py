import asyncio
import json
import threading

import pytest

from tests.gateway import test_session_task_overview as overview_fixture
from tests.gateway.test_session_task_analysis import Transport, answer, rpc
from tests.waiting import async_wait_until

overview = overview_fixture.overview


@pytest.fixture
def seeded(overview, monkeypatch):
    from gateway import run_turn_prepare
    overview.db.append_message(overview.ref.session_id, 'user', 'Build and test the feature')
    overview.db.append_message(overview.ref.session_id, 'tool', '3 passed', tool_name='terminal', tool_call_id='run')
    monkeypatch.setattr(run_turn_prepare, '_resolve_policy_agent_runtime',
                        lambda *a, **k: ('frozen', {'provider': 'custom', 'base_url': 'https://model.invalid/v1', 'api_key': 'fixture'}))
    overview.connection.transport = Transport()
    return overview


@pytest.mark.asyncio
async def test_concurrent_same_task_cannot_overwrite_new_analysis(seeded, monkeypatch):
    from agent import oneshot
    gate, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []

    def model(**kwargs):
        with lock:
            ordinal = len(calls)
            calls.append(ordinal)
        if ordinal == 0:
            entered.set()
            gate.wait(5)
        value = answer(json.loads(kwargs['user_input'])['messages'])
        value['goal']['text'] = 'old result' if ordinal == 0 else 'new result'
        return json.dumps(value)

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    params = {**seeded.params, 'task_id': 'same', 'title': 'Build', 'refresh': True}
    try:
        assert await rpc(seeded, 'session.task_analysis', params) is None
        await async_wait_until(entered.is_set)
        assert await rpc(seeded, 'session.task_analysis', params) is None
        newer = await asyncio.wait_for(seeded.connection.transport.replies.get(), 5)
        assert newer['result']['analysis']['goal']['text'] == 'new result'
        gate.set()
        older = await asyncio.wait_for(seeded.connection.transport.replies.get(), 5)
        assert older['error']['data']['reason'] == 'analysis_conflict'
        saved = await rpc(seeded, 'session.task_analysis', {**params, 'refresh': False})
        assert saved['result']['analysis']['goal']['text'] == 'new result'
    finally:
        gate.set()
        await seeded.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revision', 'source', 'retiring', 'epoch', 'close', 'external_generation'])
async def test_late_analysis_cannot_write_after_boundary_changes(seeded, monkeypatch, change):
    from agent import oneshot
    from hermes_state_runtime import begin_runtime_epoch
    gate, entered, finished = threading.Event(), threading.Event(), threading.Event()

    def model(**kwargs):
        entered.set()
        gate.wait(5)
        finished.set()
        return json.dumps(answer(json.loads(kwargs['user_input'])['messages']))

    monkeypatch.setattr(oneshot, 'run_oneshot', model)
    params = {**seeded.params, 'task_id': 'same', 'title': 'Build', 'refresh': True}
    try:
        assert await rpc(seeded, 'session.task_analysis', params) is None
        await async_wait_until(entered.is_set)
        assert (await rpc(seeded, 'ping', {}))['result']
        assert await asyncio.to_thread(lambda: 'default executor is free') == 'default executor is free'
        from gateway.session_task_analysis import _key
        actions = {
            'revision': lambda: seeded.db.append_message(seeded.ref.session_id, 'user', 'Changed requirement'),
            'source': lambda: setattr(seeded.authority.sessions[seeded.ref.session_id], 'route', 'other-route'),
            'retiring': lambda: setattr(seeded.authority, 'retiring', True),
            'epoch': lambda: begin_runtime_epoch(seeded.db, instance_id='new-owner'),
            'external_generation': lambda: seeded.db.set_meta(_key(seeded.ref.session_id, 'same'), '{"generation":"external"}'),
        }
        if change == 'close':
            await seeded.connection.close()
        else:
            actions[change]()
        before = seeded.db.list_meta_prefix('gateway.task_analysis.v1:')
        gate.set()
        await async_wait_until(finished.is_set)
        if change != 'close':
            response = await asyncio.wait_for(seeded.connection.transport.replies.get(), 5)
            assert 'error' in response, response
        else:
            await async_wait_until(lambda: not seeded.authority._oneshot_pool.answers)
            assert seeded.connection.transport.replies.empty()
        assert seeded.db.list_meta_prefix('gateway.task_analysis.v1:') == before
    finally:
        gate.set()
        seeded.authority.retiring = False
        await seeded.connection.close()
