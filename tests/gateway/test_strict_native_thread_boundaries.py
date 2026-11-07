"""Real accepted/lost ACK, complete root and immutable transport boundary checks."""
import asyncio
import copy
import json

import pytest

from tests.gateway import strict_native_thread_support as native


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
@pytest.mark.parametrize('fault', ['lost', 'redirect', 'rate', 'server', 'shape', 'wrong_root', 'wrong_name', 'wrong_parent'])
async def test_create_uncertain_or_foreign_ack_never_retries(name, fault, monkeypatch, record_property):
    faults = [None, fault] if name == 'matrix' and fault in ('wrong_name', 'wrong_parent') else [fault]
    async with native.peer(name, faults) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        plan = helper.freeze(adapter, state, [('send', {'content': 'body'})], [], new_thread=True)
        with pytest.raises(Exception):
            await helper.create(plan)
        assert len([e for e in wire.events if e['method'] in ('POST', 'PUT')]) == 1
        count = len(wire.events)
        with pytest.raises((RuntimeError, ValueError)):
            await helper.create(plan)
        assert len(wire.events) == count
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_full_saved_receipt_rejects_each_identity_and_bare_root(name, monkeypatch):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        plan = helper.freeze(adapter, state, [('send', {'content': 'body'})], [], new_thread=True)
        receipt = await helper.create(plan)
        for field in ('parent', 'scope_id', 'platform', 'binding_sha256', 'request'):
            wrong = copy.deepcopy(receipt)
            wrong['ack'][field] = 'foreign'
            with pytest.raises((RuntimeError, ValueError)):
                helper.validate_receipt(plan['binding'], wrong)
        response = receipt['ack']['response']
        mutations = [('parent_id', '999'), ('guild_id', '999'), ('owner_id', '999'), ('name', 'foreign'), ('type', True)] if name == 'discord' else (
            [('result', {})] if name == 'telegram' else [('event_id', '$foreign')])
        for field, value in mutations:
            wrong = copy.deepcopy(receipt)
            wrong['ack']['response'][field] = value
            with pytest.raises((RuntimeError, ValueError)):
                helper.validate_receipt(plan['binding'], wrong)
        count = len(wire.events)
        with pytest.raises((RuntimeError, ValueError)):
            await helper.send(plan, receipt['root'])
        assert len(wire.events) == count
        assert (await helper.send(plan, receipt))['ok'] is True
        assert response == receipt['ack']['response']


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'matrix'])
async def test_foreign_scope_native_create_ack_rejected(name, monkeypatch):
    faults = [None, 'wrong_scope'] if name == 'matrix' else ['wrong_scope']
    async with native.peer(name, faults) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        plan = helper.freeze(adapter, state, [('send', {'content': 'body'})], [], new_thread=True)
        with pytest.raises(ValueError):
            await helper.create(plan)
        assert len([event for event in wire.events if event['method'] in ('POST', 'PUT')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_concurrent_same_plan_consumed_once(name, monkeypatch):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        plan = helper.freeze(adapter, state, [('send', {'content': 'once'})], [], new_thread=False)
        results = await asyncio.gather(helper.send(plan), helper.send(plan), return_exceptions=True)
        assert sum(isinstance(result, dict) and result.get('ok') is True for result in results) == 1
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        assert len([event for event in wire.events if event['method'] in ('POST', 'PUT')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
@pytest.mark.parametrize('fault', ['lost', 'redirect', 'rate', 'wrong_parent', 'wrong_root'])
async def test_first_body_unknown_stops_all_later_mutations(name, fault, monkeypatch, record_property):
    faults = [None, fault] if name == 'matrix' and fault == 'wrong_parent' else [fault]
    async with native.peer(name, faults) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        plan = helper.freeze(adapter, state, [('send', {'content': 'first'}), ('send', {'content': 'later'})], [], new_thread=False)
        with pytest.raises(Exception):
            await helper.send(plan)
        assert len([e for e in wire.events if e['method'] in ('POST', 'PUT')]) == 1
        count = len(wire.events)
        with pytest.raises((RuntimeError, ValueError)):
            await helper.send(plan)
        assert len(wire.events) == count
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_freeze_detaches_token_baseurl_options_and_multiple_plans(name, monkeypatch, record_property):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        metadata = {'notify': True}
        calls = [('send', {'content': 'frozen one', 'metadata': metadata})]
        first = helper.freeze(adapter, state, calls, [], new_thread=False)
        second = helper.freeze(adapter, state, [('send', {'content': 'frozen two'})], [], new_thread=False)
        metadata['notify'] = False
        calls[0][1]['content'] = 'mutated'
        state.chat_id = '999'
        state.job['name'] = 'mutated'
        adapter.format_message = lambda content: 'mutated formatter'
        if name == 'discord':
            import discord
            adapter._client.http.token = 'mutated'
            adapter._client.http.proxy = 'http://127.0.0.1:1'
            monkeypatch.setattr(discord.http.Route, 'BASE', 'http://127.0.0.1:1')
        elif name == 'telegram':
            adapter._bot._token = '999:mutated'
            adapter._bot._base_url = 'http://127.0.0.1:1'
            adapter.config.extra['proxy_url'] = 'http://127.0.0.1:1'
        else:
            adapter._client.api.token = 'mutated'
            adapter._client.api.base_url = 'http://127.0.0.1:1'
            adapter._proxy_url = 'http://127.0.0.1:1'
        results = await asyncio.gather(helper.send(first), helper.send(second))
        assert all(result['ok'] is True for result in results)
        serialized = json.dumps(wire.events)
        assert 'frozen one' in serialized and 'frozen two' in serialized and 'mutated' not in serialized
        assert 'inert-local-peer' not in json.dumps(first['binding'])
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_unknown_later_method_and_mutated_binding_refuse_before_network(name, monkeypatch):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        with pytest.raises((RuntimeError, ValueError)):
            helper.freeze(adapter, state, [('send', {'content': 'first'}), ('unverified', {})], [], new_thread=True)
        assert wire.events == []
        plan = helper.freeze(adapter, state, [('send', {'content': 'body'})], [], new_thread=False)
        plan['binding']['parent'] = 'foreign'
        with pytest.raises((RuntimeError, ValueError)):
            await helper.send(plan)
        assert wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_old_thread_and_inchannel_keep_exact_frozen_target(name, monkeypatch, record_property):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        state.thread_id = '$old' if name == 'matrix' else '888'
        plan = helper.freeze(adapter, state, [('send', {'content': 'old thread'})], [], new_thread=False)
        assert plan['binding']['root'] is None
        assert (await helper.send(plan))['ok'] is True
        first = wire.events[0]
        if name == 'discord':
            assert first['path'] == '/channels/888/messages'
        elif name == 'telegram':
            assert first['fields']['message_thread_id'] == '888'
        else:
            assert first['fields']['m.relates_to']['event_id'] == '$old'
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))
