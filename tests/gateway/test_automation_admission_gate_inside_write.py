"""A drain that begins while an ordered admission waits on the write lock still refuses the write.

The default automation admission (no route fence) must hand the backend a gate the backend reads
inside the write, not a verdict taken before the ordered lock is acquired.
"""

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_ingress_context import native_callback, register_transport_home
from persistence.core.errors import AuthorityRefusal
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_a_drain_during_the_ordered_write_wait_refuses_the_default_admission(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setattr('gateway.session_bot._watch_reply', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='inert-probe', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42', user_name='human')
    human = MessageEvent(text='human', message_id='human', source=adapter.build_source(
        chat_id='42', chat_type='dm', user_id='42', user_name='human', message_id='human'))
    with native_callback(runner, human, tmp_path):
        authority = await initialize_session_authority(runner, profile_id='default', instance_id='probe')
        await authority.admit_native(human)
    route = runner.session_store._generate_session_key(source)
    session_id = runner.session_store.peek_session_id(route)
    event = MessageEvent(text='wake', source=source, internal=True, message_id='wake',
                         metadata={'gateway_session_key': route, 'gateway_session_id': session_id})
    before = authority.persistence.admissions.list(session_id=session_id, pending_only=False)
    order = getattr(authority, '_ordered_writes', None) or asyncio.Lock()
    authority._ordered_writes = order
    await order.acquire()
    pending = asyncio.create_task(authority.admit_automation(adapter, event, 'probe'))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not pending.done()
    runner._draining = True
    order.release()
    with pytest.raises(AuthorityRefusal, match='^runtime_draining$'):
        await asyncio.wait_for(pending, 30)
    assert authority.persistence.admissions.list(session_id=session_id, pending_only=False) == before
    runner.session_store.close_all_db_handles()
