"""A transient provider error after a tool effect is not replay authorization."""
import asyncio

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_bot_retry import _admissions, _settle_quietly

pytest_plugins = ['tests.gateway.test_session_bot_retry']


@pytest.mark.asyncio
@pytest.mark.parametrize('observe_tool', [False, True])
async def test_bot_transient_after_real_local_effect_is_not_executed_twice(bot, monkeypatch, observe_tool):
    from gateway.session_bot import deliver, recover_bot_deliveries
    from tests.waiting import async_wait_until
    effect = bot.home / 'effect.txt'

    async def execute(authority, ref, row):
        from gateway.platforms.event import MessageEvent
        from gateway.session_delivery_policy import automation_delivery_scope, arm_tool_witness
        event = MessageEvent(text='effect fixture', source=authority.sessions[ref.session_id].source)
        async with automation_delivery_scope(authority, row, event):
            agent = SimpleNamespace(tool_start_callback=None)
            if observe_tool:
                arm_tool_witness(agent)
                agent.tool_start_callback('actual-call', 'terminal', {})
            return await effect_and_fail(authority, row)

    async def effect_and_fail(authority, row):
        await asyncio.to_thread(subprocess.run, [sys.executable, '-c',
            'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.open("a").write("effect\\n")',
            str(effect)], check=True)
        authority.pending_results[row['admission_id']] = {
            'result': {'failed': True, 'error': 'HTTP 503 server error', 'final_response': '',
                       'messages': []}, 'usage':{}}
        return ''

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    params = {'id':'f'*32, 'profile':'default', 'message':'write the local effect'}
    await deliver(bot.connection, params)
    await async_wait_until(lambda: _admissions(bot) and all(r['status']=='terminal' for r in _admissions(bot)),
                           timeout=10, what='settle effectful admission')
    await _settle_quietly(bot)
    await recover_bot_deliveries(bot.authority)
    await _settle_quietly(bot)
    again = await deliver(bot.connection, params)
    assert effect.read_text().splitlines() == ['effect'], again
    assert len(_admissions(bot)) == 1, _admissions(bot)


@pytest.mark.asyncio
async def test_sealed_consume_only_does_not_mint_retry_even_before_tool_start(bot, monkeypatch):
    from gateway.session_bot import deliver, recover_bot_deliveries
    # An admitted result consumer has no ordinary Bot request capability, even if its
    # provider fails before a tool starts. The fixture observes that empty execution.
    monkeypatch.setattr('gateway.session_delivery_policy.mark_bot_request', lambda event: None)
    bot.errors[:] = ['HTTP 503 server error']
    params = {'id':'e'*32, 'profile':'default', 'message':'consume the sealed result'}
    await deliver(bot.connection, params)
    await _settle_quietly(bot)
    await recover_bot_deliveries(bot.authority)
    await _settle_quietly(bot)
    receipt = await deliver(bot.connection, params)
    assert len(_admissions(bot)) == 1, receipt
    row = _admissions(bot)[0]
    assert row['payload']['local_automation_v1']['result_recovery_v1'] == 'consume_only'
