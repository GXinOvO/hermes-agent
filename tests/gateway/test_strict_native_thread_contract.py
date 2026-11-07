"""Bounded helper protocol, exercised through actual SDKs and loopback HTTP."""
import copy
import json

import pytest

from tests.gateway import strict_native_thread_support as native


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['discord', 'telegram', 'matrix'])
async def test_native_create_body_and_saved_receipt(name, monkeypatch, record_property):
    async with native.peer(name) as wire, native.connected(name, wire, monkeypatch) as (adapter, state):
        helper = native.module(name)
        calls = [('send', {'content': 'sealed body', 'metadata': {'notify': True}})]
        plan = helper.freeze(adapter, state, calls, [], new_thread=True)
        assert wire.events == []
        json.dumps(plan['binding'], allow_nan=False)
        receipt = await helper.create(plan)
        helper.validate_receipt(plan['binding'], json.loads(json.dumps(receipt)))
        assert receipt['root'] in ('777', '$root')
        result = await helper.send(plan, receipt)
        assert result['ok'] is True
        mutations = [event for event in wire.events if event['method'] in ('POST', 'PUT')]
        assert len(mutations) == 2
        if name == 'telegram':
            assert mutations[0]['path'].endswith('/createForumTopic')
            assert mutations[1]['fields']['message_thread_id'] == '777'
        elif name == 'discord':
            assert mutations[0]['path'] == '/channels/555/threads'
            assert mutations[1]['path'] == '/channels/777/messages'
        else:
            relation = mutations[1]['fields']['m.relates_to']
            assert relation['rel_type'] == 'm.thread' and relation['event_id'] == '$root'
        count = len(wire.events)
        with pytest.raises((RuntimeError, ValueError)):
            await helper.create(plan)
        with pytest.raises((RuntimeError, ValueError)):
            await helper.send(plan, receipt)
        assert len(wire.events) == count
        damaged = copy.deepcopy(receipt)
        damaged['root'] = '999' if name != 'matrix' else '$foreign'
        with pytest.raises((RuntimeError, ValueError)):
            helper.validate_receipt(plan['binding'], damaged)
        record_property('native_wire', json.dumps(wire.events, sort_keys=True))
