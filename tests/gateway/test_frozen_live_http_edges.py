"""Real native-HTTP qualification of independently prepared delivery plans."""
import asyncio
import base64
import hashlib
import json

import pytest

from tests.gateway import frozen_live_http_peer as native

PLATFORMS = ['mattermost', 'feishu', 'google_chat', 'weixin']


@pytest.mark.asyncio
async def test_weixin_escaped_context_token_is_not_persisted():
    secret = 'synthetic-context-"-\\-\n-私有'
    async with native.peer() as wire, native.adapter_for('weixin', wire) as (adapter, chat):
        adapter._token_store._cache[adapter._token_store._key(adapter._account_id, chat)] = secret
        plan = await native.prepare('weixin', adapter, native.state_for('weixin', chat), [('send', {'content': 'sealed'})], [])
        request = plan['binding']['prepared'][0]
        decoded = json.loads(base64.b64decode(request['body_b64']))
        assert decoded['msg']['context_token'] != secret
        assert decoded['msg']['context_token'].startswith('sha256:')
        assert (await native.deliver('weixin', plan))['ok'] is True
        assert native.writes(wire)[0].payload['msg']['context_token'] == secret


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', PLATFORMS)
async def test_text_survives_token_host_route_chunk_and_body_drift(platform):
    async with native.peer() as wire, native.peer() as decoy, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        metadata = {'job_id': 'original-job'}
        calls = [('send', {'content': 'original-text-A', 'metadata': metadata})]
        plan = await native.prepare(platform, adapter, state, calls, [])
        adapter._token, adapter._base_url = 'synthetic-token-B', decoy.base
        adapter._account_id = 'changed-account'
        if platform == 'feishu':
            adapter._client.im.v1.message.config.domain = decoy.base
        if platform == 'google_chat':
            adapter._chat_api._baseUrl = decoy.base + '/'
            adapter._credentials.token = 'synthetic-google-B'
            adapter._last_inbound_thread[chat] = 'changed-thread'
            adapter._chunk_text = lambda text: ['changed-text']
        if platform == 'weixin':
            adapter._token_store._cache.clear()
            adapter._split_text = lambda text: ['changed-text']
        calls[0][1]['content'] = 'changed-text'
        metadata['thread_id'] = 'changed-thread'
        state.chat_id = 'changed-target'
        result = await native.deliver(platform, plan)
        assert result['ok'] is True and result['message_id']
        assert not decoy.events
        events = native.writes(wire)
        assert len(events) == 1
        assert b'original-text-A' in events[0].body
        assert b'changed-' not in events[0].body
        assert 'synthetic-token-B' not in str(events[0].headers)
        assert 'synthetic-google-B' not in str(events[0].headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', PLATFORMS)
@pytest.mark.parametrize('fault', ['lost', 'redirect', 401, 429, 'missing', 'boolean'])
async def test_first_unknown_ack_stops_all_later_requests(platform, fault):
    def fail(event, index):
        if 'tenant_access_token' in event.path:
            return None
        if fault == 'missing':
            return {'success': True}
        if fault == 'boolean':
            return {'code': False, 'ret': False, 'errcode': False, 'id': True, 'name': True, 'data': {'message_id': True}}
        return fault
    async with native.peer(fail) as wire, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        calls = [('send', {'content': 'first'}), ('send', {'content': 'must-not-follow'})]
        plan = await native.prepare(platform, adapter, state, calls, [])
        with pytest.raises(Exception):
            await native.deliver(platform, plan)
        assert len(native.writes(wire)) == 1
        with pytest.raises(RuntimeError, match='already attempted'):
            await native.deliver(platform, plan)
        assert len(native.writes(wire)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', PLATFORMS)
async def test_long_text_preserves_all_preselected_chunks(platform):
    text = ('line proof-A\n' * 6000).strip()
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        formatted = adapter.format_message(text)
        if platform == 'mattermost':
            from plugins.platforms.mattermost.adapter import MAX_POST_LENGTH
            expected = adapter.truncate_message(formatted, MAX_POST_LENGTH)
        elif platform == 'feishu':
            expected = adapter.truncate_message(formatted, adapter.MAX_MESSAGE_LENGTH)
        elif platform == 'google_chat':
            expected = adapter._chunk_text(formatted)
        else:
            expected = adapter._split_text(formatted)
        assert len(expected) > 1
        plan = await native.prepare(platform, adapter, native.state_for(platform, chat), [('send', {'content': text})], [])
        result = await native.deliver(platform, plan)
        assert result['ok'] is True
        events = native.writes(wire)
        assert len(events) == len(expected)
        if platform == 'mattermost':
            actual = [e.payload['message'] for e in events]
        elif platform == 'feishu':
            actual = [json.loads(e.payload['content'])['text'] for e in events]
        elif platform == 'google_chat':
            actual = [e.payload['text'] for e in events]
        else:
            actual = [e.payload['msg']['item_list'][0]['text_item']['text'] for e in events]
        assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', PLATFORMS)
async def test_independent_plans_and_fresh_owner_binding(platform):
    async with native.peer() as wire, native.adapter_for(platform, wire) as (adapter, chat):
        state = native.state_for(platform, chat)
        calls_a = [('send', {'content': 'plan-A', 'metadata': {'job_id': 'job'}})]
        calls_b = [('send', {'content': 'plan-B', 'metadata': {'job_id': 'job'}})]
        first, second = await asyncio.gather(native.prepare(platform, adapter, state, calls_a, []),
                                             native.prepare(platform, adapter, state, calls_b, []))
        async with native.adapter_for(platform, wire) as (fresh, _):
            recovered = await native.prepare(platform, fresh, state, calls_a, [])
            assert recovered['binding'] == first['binding']
        changed = first['binding']
        changed['actualdestination'] = 'tampered'
        changed['prepared'].clear()
        assert first['binding'] != changed
        result = await asyncio.gather(native.deliver(platform, first), native.deliver(platform, second))
        assert all(r['ok'] is True for r in result)
        assert len(native.writes(wire)) == 2
        assert b'plan-A' in native.writes(wire)[0].body or b'plan-A' in native.writes(wire)[1].body
        encoded = json.dumps(first['binding'])
        for secret in ['synthetic-google-A', 'synthetic-feishu-A', 'synthetic-context-A', 'synthetic-weixin-A', 'synthetic-mattermost-A']:
            assert secret not in encoded
            for request in first['binding']['prepared']:
                assert secret.encode() not in base64.b64decode(request['body_b64'])
        binding = first['binding']
        expected = binding.pop('transport_sha256')
        from plugins.platforms.frozen_delivery import encode
        assert hashlib.sha256(encode(binding)).hexdigest() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['mattermost', 'feishu', 'google_chat'])
async def test_media_binding_rebuild_and_upload_ack_stop(platform, tmp_path):
    path = tmp_path / 'voice.ogg'
    path.write_bytes(b'original-media-A')
    def missing(event, index):
        if event.path.endswith(('/files', '/images', 'attachments:upload')):
            return {'success': True}
    async with native.peer(missing) as wire, native.adapter_for(platform, wire) as (adapter, chat):
        calls = [('send_document', {'file_path': str(path), 'file_name': 'visible.bin', 'caption': 'kept-caption'})]
        state = native.state_for(platform, chat)
        first = await native.prepare(platform, adapter, state, calls, [(str(path), False)])
        fresh = await native.prepare(platform, adapter, state, calls, [(str(path), False)])
        assert first['binding'] == fresh['binding']
        with pytest.raises(RuntimeError):
            await native.deliver(platform, first)
        assert len(native.writes(wire)) == 1
        assert b'original-media-A' in native.writes(wire)[0].body
        assert b'visible.bin' in native.writes(wire)[0].body


@pytest.mark.asyncio
async def test_aba_during_media_read_is_rejected_before_mutation(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / 'proof.bin'
    path.write_bytes(b'A')
    original = Path.open
    class ChangingRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self):
            path.write_bytes(b'B')
            data = self.stream.read()
            path.write_bytes(b'A')
            return data
    async with native.peer() as wire, native.adapter_for('mattermost', wire) as (adapter, chat):
        monkeypatch.setattr(Path, 'open', lambda self, mode='r', *a, **kw:
            ChangingRead(original(self, mode, *a, **kw)) if self == path and mode == 'rb' else original(self, mode, *a, **kw))
        with pytest.raises(ValueError, match='changed during freeze'):
            await native.prepare('mattermost', adapter, native.state_for('mattermost', chat),
                [('send', {'content': 'must-not-send'}), ('send_document', {'file_path': str(path)})], [(str(path), False)])
        assert native.writes(wire) == []
