import pytest

from tests.gateway import frozen_live_http_peer as native


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'google_chat'])
async def test_frozen_http_rejects_other_target_ack_before_next_send(platform):
    def fault(event, index):
        if platform == 'mattermost':
            return {'id': 'post-other', 'channel_id': 'other-channel'}
        return {'name': 'spaces/other-space/messages/msg-other'}

    async with native.peer(fault) as wire, native.adapter_for(platform, wire) as (adapter, chat):
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat),
            [('send', {'content': 'first'}), ('send', {'content': 'must-not-send'})], [])
        with pytest.raises((RuntimeError, ValueError), match='ACK'):
            await native.deliver(platform, plan)
        assert len(native.writes(wire)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'google_chat'])
async def test_frozen_http_exact_target_ack_allows_two_submissions(platform):
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat),
            [('send', {'content': 'first'}), ('send', {'content': 'second'})], [])
        assert (await native.deliver(platform, plan))['ok'] is True
        assert len(native.writes(wire)) == 2
