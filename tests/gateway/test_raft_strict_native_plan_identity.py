"""Valid fixtures for the source's PreparedSend/FrozenSend representation."""
from dataclasses import replace
import json

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.raft.adapter import RaftAdapter
from plugins.platforms.raft.strict_delivery import PreparedSend
from tests.gateway import test_raft_strict_native_transport as transport


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['endpoint', 'token', 'body', 'replay_copy'])
async def test_whole_issued_plan_is_authenticated_and_claim_not_reusable(changed, tmp_path, monkeypatch):
    async with transport.peer() as original, transport.peer() as other:
        transport.credential(tmp_path, monkeypatch, original.url)
        adapter = RaftAdapter(PlatformConfig(enabled=True))
        plan = await transport.prepare(adapter)
        frozen = plan._frozen
        if changed == 'endpoint':
            forged = PreparedSend(replace(frozen, endpoint=other.url + '/internal/agent-api/v2/send'))
        elif changed == 'token':
            forged = PreparedSend(replace(frozen, token='sk_agent_other-inert-only'))
        elif changed == 'body':
            body = json.loads(frozen.body)
            body['content'] = 'changed wire but original caller still passes'
            forged = PreparedSend(replace(frozen, body=json.dumps(body).encode()))
        else:
            assert (await transport.consume(adapter, plan)).success
            assert plan._used is True and len(original.events) == 1
            forged = PreparedSend(frozen)
        assert forged is not plan and forged._used is False
        assert frozen.endpoint == original.url + '/internal/agent-api/v2/send'
        assert json.loads(frozen.body)['content'] == 'sealed-raft-loopback-result'
        original_count = len(original.events)
        try:
            result = await transport.consume(adapter, forged)
        except (RuntimeError, ValueError):
            result = None
        print(json.dumps({'changed': changed, 'original_posts':len(original.events), 'other_posts':len(other.events),
            'new_posts':len(original.events) - original_count + len(other.events),
            'success':getattr(result, 'success', None),
            'wire_content':[event['body']['content'] for event in original.events + other.events]}))
        assert len(original.events) == original_count and other.events == [], 'cloned or altered plan must not publish'
