import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from aiohttp import web

from gateway.strict_transport import prepare_call, strict_call
from gateway.final_delivery import _attachments_content
from gateway.platforms.base import _ExtractedResponse
from tests.gateway import test_strict_sdk_delivery as sdk


@asynccontextmanager
async def images():
    state = {'body': b'image-A', 'status': 200, 'requests': 0, 'redirect': None}

    async def image(_request):
        state['requests'] += 1
        if state['redirect']:
            raise web.HTTPFound(state['redirect'])
        return web.Response(body=state['body'], status=state['status'])

    app = web.Application()
    app.router.add_get('/image', image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield state, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/image'
    finally:
        await runner.cleanup()


def allow_loopback(monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: True)
    monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                        lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))


async def refused(adapter, method, kwargs):
    try:
        result = await strict_call(adapter, method, **kwargs)
    except (ValueError, RuntimeError, TypeError):
        return
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['send_image', 'send_multiple_images'])
async def test_remote_image_is_frozen_at_prepare_and_consumed_once(monkeypatch, method):
    allow_loopback(monkeypatch)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        args = {'image_url': url, 'caption': 'caption'} if method == 'send_image' else {'images': [(url, 'caption')]}
        name, kwargs = await prepare_call(adapter, method, chat_id='C555', execution_id='exec', claim_id='claim', **args)
        assert image['requests'] == 1 and wire.events == []
        image['body'] = b'image-B'
        assert (await strict_call(adapter, name, **kwargs)).success
        assert image['requests'] == 1
        assert any('image-A' in event['body'] for event in wire.events)
        assert not any('image-B' in event['body'] for event in wire.events)
        before = list(wire.events)
        await refused(adapter, name, kwargs)
        assert wire.events == before


@pytest.mark.asyncio
@pytest.mark.parametrize('method,key', [
    ('send_image_file', 'image_path'), ('send_video', 'video_path'),
    ('send_document', 'file_path'), ('send_voice', 'audio_path'),
])
async def test_local_attachment_is_frozen_at_prepare(tmp_path, method, key):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        name, kwargs = await prepare_call(adapter, method, chat_id='C555', execution_id='exec', claim_id='claim',
                                          **{key: str(path)})
        path.write_bytes(b'image-B')
        assert (await strict_call(adapter, name, **kwargs)).success
        assert any('image-A' in event['body'] for event in wire.events)
        assert not any('image-B' in event['body'] for event in wire.events)


@pytest.mark.asyncio
async def test_plan_rejects_changed_identity_arguments_and_adapter(tmp_path):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter, sdk.slack_adapter(wire) as other:
        name, kwargs = await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                                          execution_id='exec', claim_id='claim')
        await refused(adapter, name, {**kwargs, 'claim_id': 'other'})
        await refused(adapter, name, {**kwargs, 'chat_id': 'C999'})
        await refused(other, name, kwargs)
        assert wire.events == []
        assert (await strict_call(adapter, name, **kwargs)).success


@pytest.mark.asyncio
async def test_same_claim_different_image_cannot_replace_live_plan(monkeypatch):
    allow_loopback(monkeypatch)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        args = dict(chat_id='C555', image_url=url, execution_id='exec', claim_id='claim')
        name, kwargs = await prepare_call(adapter, 'send_image', **args)
        image['body'] = b'image-B'
        with pytest.raises((RuntimeError, ValueError)):
            await prepare_call(adapter, 'send_image', **args)
        assert wire.events == []
        assert (await strict_call(adapter, name, **kwargs)).success
        assert any('image-A' in event['body'] for event in wire.events)


@pytest.mark.asyncio
async def test_same_claim_equivalent_live_plans_share_one_consumption(tmp_path):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        args = dict(chat_id='C555', image_path=str(path), execution_id='exec', claim_id='claim')
        first, second = await asyncio.gather(*(prepare_call(adapter, 'send_image_file', **args) for _ in range(2)))
        assert (await strict_call(adapter, first[0], **first[1])).success
        before = list(wire.events)
        await refused(adapter, second[0], second[1])
        assert wire.events == before


@pytest.mark.asyncio
async def test_download_error_is_preclaim_and_does_not_leak_url(monkeypatch):
    allow_loopback(monkeypatch)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        image['status'] = 503
        with pytest.raises(RuntimeError) as caught:
            await prepare_call(adapter, 'send_image', chat_id='C555', image_url=url + '?secret=private',
                               execution_id='exec', claim_id='claim')
        assert 'private' not in str(caught.value)
        assert image['requests'] == 1 and wire.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['remote', 'local'])
async def test_image_capacity_refused_before_upload(tmp_path, monkeypatch, source):
    allow_loopback(monkeypatch)
    monkeypatch.setattr('gateway.platforms.base.get_inbound_media_max_bytes', lambda: 4)
    async with images() as (_image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        path = tmp_path / 'payload.png'
        path.write_bytes(b'image-A')
        args = {'image_url': url} if source == 'remote' else {'image_path': str(path)}
        method = 'send_image' if source == 'remote' else 'send_image_file'
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, method, chat_id='C555', execution_id='exec', claim_id='claim', **args)
        assert wire.events == []


@pytest.mark.asyncio
async def test_ssrf_refused_before_download_and_upload(monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: False)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_image', chat_id='C555', image_url=url,
                               execution_id='exec', claim_id='claim')
        assert image['requests'] == 0 and wire.events == []


@pytest.mark.asyncio
async def test_selected_client_is_frozen_without_exposing_credentials(tmp_path):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as first, sdk.peer() as second, sdk.slack_adapter(first) as adapter:
        name, kwargs = await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                                          execution_id='exec', claim_id='claim')
        assert 'inert-local-peer' not in repr(kwargs['prepared_plan'])
        adapter._app.client.base_url = second.base + '/'
        adapter._app.client.token = 'different-secret'
        assert (await strict_call(adapter, name, **kwargs)).success
        assert first.events and second.events == []


@pytest.mark.asyncio
async def test_plan_cannot_expose_or_change_its_frozen_state(tmp_path):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        name, kwargs = await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                                          execution_id='exec', claim_id='claim')
        plan = kwargs['prepared_plan']
        assert not hasattr(plan, 'state')
        assert (await strict_call(adapter, name, **kwargs)).success
        assert any('image-A' in event['body'] for event in wire.events)


@pytest.mark.asyncio
async def test_nonserializable_text_is_refused_during_prepare(monkeypatch):
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        monkeypatch.setattr(adapter, '_maybe_blocks', lambda _: {'not-json': object()})
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send', chat_id='C555', content='answer',
                               execution_id='exec', claim_id='claim')
        assert wire.events == []


@pytest.mark.asyncio
async def test_batch_capacity_stops_before_downloading_later_images(monkeypatch):
    allow_loopback(monkeypatch)
    monkeypatch.setattr('gateway.platforms.base.get_inbound_media_max_bytes', lambda: 10)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_multiple_images', chat_id='C555',
                images=[(url, '')] * 3, execution_id='exec', claim_id='claim')
        assert image['requests'] == 2 and wire.events == []


@pytest.mark.asyncio
async def test_ssrf_redirect_is_refused_before_upload(monkeypatch):
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        image['redirect'] = 'http://127.0.0.1:1/blocked'
        monkeypatch.setattr('tools.url_safety.is_safe_url', lambda candidate: candidate == url)
        monkeypatch.setattr('tools.url_safety.create_ssrf_safe_async_client',
                            lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs))
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_image', chat_id='C555', image_url=url,
                               execution_id='exec', claim_id='claim')
        assert image['requests'] == 1 and wire.events == []


@pytest.mark.asyncio
async def test_prepared_text_preserves_confirmed_ids_after_lost_ack():
    async with sdk.peer([None, 'lost']) as wire, sdk.slack_adapter(wire) as adapter:
        adapter.MAX_MESSAGE_LENGTH = 80
        method, kwargs = await prepare_call(adapter, 'send', chat_id='C555', content='x' * 200,
                                            execution_id='exec', claim_id='claim')
        result = await strict_call(adapter, method, **kwargs)
        assert not result.success and len(wire.events) == 2
        assert len(result.raw_response['message_ids']) == 1
        await refused(adapter, method, kwargs)
        assert len(wire.events) == 2


@pytest.mark.asyncio
async def test_live_prepared_payloads_have_shared_memory_bound(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.platforms.base.get_inbound_media_max_bytes', lambda: 10)
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        first = await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                                   execution_id='exec', claim_id='one')
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                               execution_id='exec', claim_id='two')
        assert wire.events == []
        assert (await strict_call(adapter, first[0], **first[1])).success


@pytest.mark.asyncio
async def test_claim_capacity_fails_closed_before_post(tmp_path):
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        adapter._strict_consumed_claims.update(('old', str(i)) for i in range(4096))
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                               execution_id='exec', claim_id='claim')
        assert wire.events == []


@pytest.mark.asyncio
async def test_issued_plan_with_target_style_claim_still_preflights(monkeypatch):
    monkeypatch.setattr('tools.url_safety.is_safe_url', lambda url: False)
    async with images() as (image, url), sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        with pytest.raises(RuntimeError):
            await prepare_call(adapter, 'send_image', chat_id='C555', image_url=url,
                               execution_id='exec', claim_id='target:0:not-a-bypass')
        assert image['requests'] == 0 and wire.events == []


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason='preflight does not receive the sealed local file digest')
async def test_local_frozen_bytes_match_prior_snapshot_during_aba(tmp_path, monkeypatch):
    import plugins.platforms.slack.strict_delivery as slack
    path = tmp_path / 'payload.png'
    path.write_bytes(b'image-A')
    extracted = _ExtractedResponse(text_content='', images=[], media_files=[(str(path), False)], local_files=[],
                                   force_document_attachments=False, pre_extract='')
    sealed = _attachments_content(extracted)
    original = slack._local_bytes

    def read_transient(value):
        path.write_bytes(b'image-B')
        try:
            return original(value)
        finally:
            path.write_bytes(b'image-A')

    monkeypatch.setattr(slack, '_local_bytes', read_transient)
    async with sdk.peer() as wire, sdk.slack_adapter(wire) as adapter:
        method, kwargs = await prepare_call(adapter, 'send_image_file', chat_id='C555', image_path=str(path),
                                            execution_id='exec', claim_id='claim')
        assert _attachments_content(extracted) == sealed
        assert (await strict_call(adapter, method, **kwargs)).success
        assert not any('image-B' in event['body'] for event in wire.events)