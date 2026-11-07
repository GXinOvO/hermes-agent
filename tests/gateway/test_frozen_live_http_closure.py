"""Preclaim input and route closure tests for frozen HTTP plans."""
import json

import pytest

from tests.gateway import frozen_live_http_peer as native


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat', 'weixin'])
async def test_reserved_response_marker_in_user_content_refused_before_mutation(platform):
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        calls = [('send', {'content': 'user __HERMES_FROZEN_UPLOAD_REFERENCE__ text'})]
        with pytest.raises(ValueError, match='reserved response marker'):
            await native.prepare(platform, adapter, native.state_for(platform, chat), calls, [])
        assert native.writes(wire) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat'])
async def test_selected_parent_and_reply_semantics_are_frozen(platform):
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        parent = 'spaces/space-A/threads/parent-A' if platform == 'google_chat' else 'parent-A'
        metadata = {'thread_id': parent, 'reply_to_message_id': 'message-A', 'job_id': 'job-A'}
        calls = [('send', {'content': 'thread-reply', 'reply_to': 'message-A' if platform == 'feishu' else None, 'metadata': metadata})]
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat), calls, [])
        metadata['thread_id'] = 'parent-B'
        metadata['reply_to_message_id'] = 'message-B'
        result = await native.deliver(platform, plan)
        assert result['ok'] is True
        event = native.writes(wire)[0]
        if platform == 'mattermost':
            assert event.payload['root_id'] == 'parent-A'
        elif platform == 'feishu':
            assert event.path.endswith('/message-A/reply')
            assert event.payload['reply_in_thread'] is True
        else:
            assert event.payload['thread'] == {'name': parent}
            assert 'messageReplyOption=REPLY_MESSAGE_OR_FAIL' in event.query


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat', 'weixin'])
async def test_same_plan_concurrent_send_is_consumed_once(platform):
    import asyncio
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat), [('send', {'content': 'once'})], [])
        results = await asyncio.gather(native.deliver(platform, plan), native.deliver(platform, plan), return_exceptions=True)
        assert sum(isinstance(result, dict) and result.get('ok') is True for result in results) == 1
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        assert len(native.writes(wire)) == 1
        assert json.loads(json.dumps(plan['binding'])) == plan['binding']
