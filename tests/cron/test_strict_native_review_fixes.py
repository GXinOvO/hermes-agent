"""Regression oracles for the five native19 review findings (deleg_9882d710).

Each test reconstructs the reviewer's counterexample against the integration tree.
Product bytes are unchanged until this file is red for the intended reason.
"""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from cron import delivery_queue as queue, scheduler, strict_thread_delivery as stages
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore
from tests.gateway import strict_native_thread_support as support


def setup_row(tmp_path, monkeypatch, platform, *, dm=False):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(queue, 'DELIVERY_DB', tmp_path / 'cron/deliveries.db')
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    parent = '!room:local' if platform == 'matrix' else '555'
    job = {'id': 'reg-job', 'execution_id': 'reg-exec', 'name': 'Regression native', 'deliver': 'origin',
           'attach_to_session': True, 'origin': {'platform': platform, 'chat_id': parent,
            'chat_type': 'dm' if dm else 'group', 'user_id': '999',
            'scope_id': {'discord': '444', 'telegram': '', 'matrix': 'local'}[platform]}}
    queue.enqueue('reg-exec', job, 'sealed body')
    return job


@pytest.mark.asyncio
async def test_discord_native_retains_client_allowed_mentions(monkeypatch):
    import discord
    async with support.peer('discord') as wire, support.connected('discord', wire, monkeypatch) as (adapter, state):
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        adapter._client._connection.allowed_mentions = allowed
        plan = support.module('discord').freeze(
            adapter, state, [('send', {'content': '@everyone <@&123> hello'})], [], new_thread=False)
        await support.module('discord').send(plan)
        assert wire.events[0]['fields'].get('allowed_mentions') == allowed.to_dict()


@pytest.mark.asyncio
async def test_discord_dm_continuable_uses_ordinary_delivery(tmp_path, monkeypatch):
    import discord
    job = setup_row(tmp_path, monkeypatch, 'discord', dm=True)
    async with support.peer('discord') as wire, support.connected('discord', wire, monkeypatch) as (adapter, _state):
        channel = discord.DMChannel(me=adapter._client.user, state=adapter._client._connection,
            data={'id': '555', 'type': 1,
                  'recipients': [{'id': '999', 'username': 'human', 'discriminator': '0', 'avatar': None}]})
        adapter._client.get_channel = lambda cid: channel if cid == 555 else None
        adapter._session_store = SessionStore(tmp_path / 'sessions', GatewayConfig())
        job['origin']['scope_id'] = ''
        with queue._transaction() as conn:
            conn.execute('UPDATE deliveries SET job_json=? WHERE execution_id=?',
                         (json.dumps(job, ensure_ascii=False, sort_keys=True), 'reg-exec'))
        monkeypatch.setattr('gateway.config.load_gateway_config',
                            lambda: GatewayConfig(platforms={Platform.DISCORD: adapter.config}))
        assert await asyncio.to_thread(scheduler.drain_delivery_queue,
                                       {Platform.DISCORD: adapter}, asyncio.get_running_loop()) == 1
        assert queue.get_status('reg-exec')['status'] == 'delivered'
        assert [event['path'] for event in wire.events] == ['/channels/555/messages']


@pytest.mark.asyncio
async def test_corrupt_native_sibling_does_not_abort_recovery(tmp_path, monkeypatch):
    job = setup_row(tmp_path, monkeypatch, 'telegram')
    async with support.peer('telegram') as wire, support.connected('telegram', wire, monkeypatch) as (adapter, state):
        state.job = job
        plan = support.module('telegram').freeze(
            adapter, state, [('send', {'content': 'sealed body'})], [], new_thread=True)
        receipt = await support.module('telegram').create(plan)
        value = stages.prepare_bundle([({'platform': 'telegram', 'chat_id': '555'}, state, 'sealed body',
            {'_native_platform': 'telegram', 'new_thread': True, 'plan': plan})], job, 'sealed body', False)
        clean = copy.deepcopy(value)
        target = value['binding']['targets'][0]
        del target['plan']['root']['name']
        receipt['ack']['request'] = copy.deepcopy(target['plan']['root'])
        from plugins.platforms.discord.strict_thread import _digest
        receipt['ack']['binding_sha256'] = _digest(target['plan'])
        value['binding_sha256'] = stages.digest(value['binding'])
        value['items'][0] = {'stage': 'root_acked', 'receipt': receipt,
            'receipt_sha256': stages.digest({'binding': value['binding_sha256'], 'target': target, 'ack': receipt})}
        clean['binding']['job'] = dict(job, execution_id='reg-good')
        clean['binding_sha256'] = stages.digest(clean['binding'])
        queue.enqueue('reg-good', clean['binding']['job'], 'sealed body')
        with queue._transaction() as conn:
            for execution, encoded in [('reg-exec', value), ('reg-good', clean)]:
                conn.execute("UPDATE deliveries SET status='delivering', owner_process_id='dead-owner', "
                            "owner_pid=99999999, owner_started_at=1, thread_json=? WHERE execution_id=?",
                            (stages.encode(encoded), execution))
        raw = queue.get_status('reg-exec')['thread_json']
        assert stages.recoverable(queue.get_status('reg-good')) is True
        queue.recover_abandoned()
        assert queue.get_status('reg-exec')['thread_json'] == raw
        assert queue.get_status('reg-good')['status'] == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['discord', 'telegram', 'matrix'])
async def test_native_ordinary_binding_rejects_bad_message_and_route(tmp_path, monkeypatch, platform):
    job = setup_row(tmp_path, monkeypatch, platform)
    async with support.peer(platform) as wire, support.connected(platform, wire, monkeypatch) as (adapter, state):
        state.job = job
        plan = support.module(platform).freeze(
            adapter, state, [('send', {'content': 'sealed body'})], [], new_thread=False)
        value = stages.prepare_bundle([({'platform': platform, 'chat_id': state.chat_id}, state, 'sealed body',
            {'_native_platform': platform, 'new_thread': False, 'plan': plan})], job, 'sealed body', False)
        value['binding']['targets'][0]['plan']['messages'] = [17]
        value['binding']['targets'][0]['route'] = {}
        value['binding_sha256'] = stages.digest(value['binding'])
        with pytest.raises((ValueError, TypeError)):
            stages.validate(value)


@pytest.mark.asyncio
async def test_outer_body_timeout_stops_later_native_mutations(tmp_path, monkeypatch):
    setup_row(tmp_path, monkeypatch, 'discord')
    monkeypatch.setattr(scheduler, 'load_config',
                        lambda: {'cron': {'wrap_response': False, 'standalone_send_timeout_seconds': 1}})
    body_started, release = asyncio.Event(), asyncio.Event()

    class HeldFirstBody(support.Peer):
        async def handle(self, request):
            response = await super().handle(request)
            if request.path.endswith('/messages') and not body_started.is_set():
                body_started.set()
                await release.wait()
            return response

    monkeypatch.setattr(support, 'Peer', HeldFirstBody)
    helper = support.module('discord')
    original_send = helper.send
    finished = asyncio.Event()

    async def observed_send(*args, **kwargs):
        try:
            return await original_send(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(helper, 'send', observed_send)
    with queue._transaction() as conn:
        conn.execute('UPDATE deliveries SET content=? WHERE execution_id=?', ('x' * 2100, 'reg-exec'))
    async with support.peer('discord') as wire, support.connected('discord', wire, monkeypatch) as (adapter, _state):
        adapter._session_store = SessionStore(tmp_path / 'sessions', GatewayConfig())
        monkeypatch.setattr('gateway.config.load_gateway_config',
                            lambda: GatewayConfig(platforms={Platform.DISCORD: adapter.config}))
        drain = asyncio.create_task(asyncio.to_thread(
            scheduler.drain_delivery_queue, {Platform.DISCORD: adapter}, asyncio.get_running_loop()))
        await asyncio.wait_for(body_started.wait(), 10)
        try:
            await asyncio.wait_for(drain, 10)
            status = queue.get_status('reg-exec')['status']
            before = len(wire.events)
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 10)
        assert status == 'unknown'
        assert len(wire.events) == before
