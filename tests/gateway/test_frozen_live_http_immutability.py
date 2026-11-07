"""Freeze the real native request before the Cron delivery claim."""
import pytest

from tests.gateway import frozen_live_http_peer as native


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat', 'weixin'])
async def test_postfreeze_body_metadata_and_media_are_frozen(platform, tmp_path):
    media = tmp_path / 'proof.bin'
    media.write_bytes(b'original-A')
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        metadata = {'job_id': 'job-A'}
        calls = [('send', {'content': 'sealed-original-A', 'metadata': metadata}),
                 ('send_document', {'file_path': str(media), 'caption': 'caption-A', 'metadata': metadata})]
        plan = await native.prepare(platform, adapter, state, calls, [(str(media), False)])
        calls[0][1]['content'] = 'MUTATED-B'
        metadata['thread_id'] = 'MUTATED-THREAD'
        media.write_bytes(b'MUTATED-B')
        result = await native.deliver(platform, plan)
        assert result['ok'] is True
        sent = b'\n'.join(e.body for e in native.writes(wire))
        assert b'MUTATED-B' not in sent
        assert b'MUTATED-THREAD' not in sent
        assert b'sealed-original-A' in sent
        if platform != 'weixin':
            assert b'original-A' in sent


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat', 'weixin'])
async def test_later_missing_attachment_prevents_all_mutations(platform, tmp_path):
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        missing = str(tmp_path / 'missing.bin')
        calls = [('send', {'content': 'must-never-submit'}), ('send_document', {'file_path': missing})]
        try:
            plan = await native.prepare(platform, adapter, state, calls, [(missing, False)])
            await native.deliver(platform, plan)
        except (ValueError, RuntimeError, OSError):
            pass
        assert native.writes(wire) == []
