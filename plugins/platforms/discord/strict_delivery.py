"""Discord SDK serializers with a request implementation that never replays POST.

The stock HTTPClient.request retries 429, selected 5xx and connection resets.
Strict calls use a private SDK client/session, retaining token, proxy and native
payload encoding without mutating the client used by ordinary human sends.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
import io
from functools import wraps
from pathlib import Path
from urllib.parse import unquote

from gateway.platforms.base import SendResult, ssrf_redirect_guard


logger = logging.getLogger(__name__)


def strict_images_transport(method):
    from gateway.strict_transport import single_attempt_transport, strict_transport

    @wraps(method)
    @single_attempt_transport
    async def send(self, chat_id, images, metadata=None, human_delay=0.0):
        if strict_transport():
            return await send_images(self, chat_id, images, metadata)
        return await method(self, chat_id, images, metadata, human_delay)

    return send


def failed(exc, delivered=()):
    return SendResult(success=False, retryable=False, error='strict_delivery_unknown: ' + str(exc),
                      raw_response={'message_ids': list(delivered)})


@asynccontextmanager
async def single_http(adapter):
    import aiohttp
    from discord.http import HTTPClient
    from discord.errors import HTTPException
    from agent.ssl_verify import platform_ssl_context

    source = getattr(adapter._client, 'http', None)
    if not isinstance(source, HTTPClient) or not source.token:
        raise RuntimeError('strict_transport_unverified: Discord HTTP client unavailable')

    class SingleAttemptHTTPClient(HTTPClient):
        async def request(self, route, *, files=None, form=None, **kwargs):
            headers = {'User-Agent': source.user_agent, 'Authorization': 'Bot ' + source.token}
            if form:
                data = aiohttp.FormData(quote_fields=False)
                for part in form:
                    data.add_field(**part)
                kwargs['data'] = data
            if source.proxy:
                kwargs['proxy'] = source.proxy
            if source.proxy_auth:
                kwargs['proxy_auth'] = source.proxy_auth
            if not source._global_over.is_set():
                await source._global_over.wait()
            bucket = source.get_ratelimit(route.key + ':' + route.major_parameters)
            async with bucket:
                async with session.request(route.method, route.url, headers=headers,
                                           allow_redirects=False, **kwargs) as response:
                    data = await response.json(content_type=None)
                    if 'X-Ratelimit-Remaining' in response.headers and response.status != 429:
                        bucket.update(response, use_clock=source.use_clock)
                    if not 200 <= response.status < 300:
                        raise HTTPException(response, data)
                    return data

    connector = aiohttp.TCPConnector(ssl=platform_ssl_context())
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=60),
                                     trust_env=False) as session:
        yield SingleAttemptHTTPClient(asyncio.get_running_loop())


async def preflight(adapter, method, *, chat_id, metadata=None, **kwargs):
    from discord.http import HTTPClient
    source = getattr(adapter._client, 'http', None)
    if not isinstance(source, HTTPClient) or not source.token:
        raise RuntimeError('strict_transport_unverified: Discord HTTP client unavailable')
    channel = await target(adapter, chat_id, metadata)
    if kwargs.get('caption') and len(kwargs['caption']) > adapter.MAX_MESSAGE_LENGTH:
        raise ValueError('Discord caption exceeds message limit')
    images = kwargs.get('images', ())
    if method == 'send_multiple_images' and not images:
        raise ValueError('no images to send')
    for start in range(0, len(images), 10):
        caption = '\n'.join(caption for _url, caption in images[start:start + 10] if caption)
        if len(caption) > adapter.MAX_MESSAGE_LENGTH:
            raise ValueError('Discord combined captions exceed message limit')
    for key in ('image_path', 'audio_path', 'video_path', 'file_path'):
        if kwargs.get(key):
            size = (await asyncio.to_thread(Path(kwargs[key]).stat)).st_size
            if size > adapter._discord_upload_limit_bytes(channel):
                raise ValueError('Discord file exceeds upload limit')
    if method == 'send_voice':
        from plugins.platforms.discord.adapter_media import _ogg_opus_duration
        data = await asyncio.to_thread(Path(kwargs['audio_path']).read_bytes)
        if _ogg_opus_duration(data) is None:
            raise ValueError('strict_transport_unsupported: native voice requires Ogg/Opus')


async def target(adapter, chat_id, metadata):
    from plugins.platforms.discord.adapter import _prompt_target_id

    channel = await adapter._resolve_channel(_prompt_target_id(chat_id, metadata))
    if channel is None:
        raise ValueError('Discord channel unavailable')
    if adapter._is_forum_parent(channel):
        raise ValueError('strict_transport_unsupported: Discord forum parent')
    return channel


async def post(adapter, http, channel, *, content=None, files=None, reply_to=None):
    from discord.http import handle_message_parameters
    from discord.utils import MISSING

    reference = adapter._reply_reference_for_send(reply_to, channel)
    with handle_message_parameters(content=content, files=files if files else MISSING,
            message_reference=reference.to_dict() if reference else MISSING) as params:
        data = await http.send_message(channel.id, params=params)
        if files and len(data.get('attachments', [])) != len(files):
            raise RuntimeError('Discord attachment ACK incomplete')
        return None, str(data['id'])


async def send_text(adapter, chat_id, content, reply_to, metadata):
    delivered = []
    try:
        if not (content or '').strip():
            raise ValueError('Refusing to send empty message')
        chunks = adapter.truncate_message(adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH)
        if len(chunks) > adapter.MAX_SPLIT_MESSAGES:
            raise ValueError('strict_transport_unsupported: Discord split limit exceeded')
        channel = await target(adapter, chat_id, metadata)
        async with single_http(adapter) as http:
            for index, chunk in enumerate(chunks):
                _, message_id = await post(adapter, http, channel, content=chunk,
                    reply_to=reply_to if index == 0 or adapter._reply_to_mode == 'all' else None)
                delivered.append(message_id)
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc, delivered)


async def file_bytes(source):
    from tools.url_safety import create_ssrf_safe_async_client, is_safe_url

    if source.startswith('file://'):
        path = Path(unquote(source[7:]))
        return await asyncio.to_thread(path.read_bytes), path.name
    if not is_safe_url(source):
        raise ValueError('Unsafe media URL')
    async with create_ssrf_safe_async_client(timeout=30, follow_redirects=True,
            event_hooks={'response': [ssrf_redirect_guard]}) as client:
        response = await client.get(source)
        response.raise_for_status()
        return response.content, 'image.png'


async def send_images(adapter, chat_id, images, metadata, reply_to=None):
    import discord

    delivered = []
    try:
        if not images:
            raise ValueError('no images to send')
        channel = await target(adapter, chat_id, metadata)
        sources = []
        for url, caption in images:
            data, name = await file_bytes(url)
            if len(data) > adapter._discord_upload_limit_bytes(channel):
                raise ValueError('Discord file exceeds upload limit')
            if len(caption) > adapter.MAX_MESSAGE_LENGTH:
                raise ValueError('Discord caption exceeds message limit')
            sources.append((data, name, caption))
        batches = [sources[start:start + 10] for start in range(0, len(sources), 10)]
        captions = ['\n'.join(caption for data, name, caption in batch if caption) for batch in batches]
        if any(len(caption) > adapter.MAX_MESSAGE_LENGTH for caption in captions):
            raise ValueError('Discord combined captions exceed message limit')
        async with single_http(adapter) as http:
            for batch, caption in zip(batches, captions):
                files = [discord.File(io.BytesIO(data), filename=name) for data, name, caption in batch]
                _, message_id = await post(adapter, http, channel, content=caption or None, files=files, reply_to=reply_to)
                delivered.append(message_id)
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc, delivered)


async def send_file(adapter, chat_id, path, caption, reply_to, metadata, file_name=None):
    import discord

    try:
        channel = await target(adapter, chat_id, metadata)
        data = await asyncio.to_thread(Path(path).read_bytes)
        if len(data) > adapter._discord_upload_limit_bytes(channel):
            raise ValueError('Discord file exceeds upload limit')
        if caption and len(caption) > adapter.MAX_MESSAGE_LENGTH:
            raise ValueError('Discord caption exceeds message limit')
        file = discord.File(io.BytesIO(data), filename=file_name or Path(path).name)
        async with single_http(adapter) as http:
            _, message_id = await post(adapter, http, channel, content=caption, files=[file], reply_to=reply_to)
        return SendResult(success=True, message_id=message_id)
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc)


async def send_url(adapter, chat_id, url, caption, reply_to, metadata):
    return await send_images(adapter, chat_id, [(url, caption or '')], metadata, reply_to=reply_to)


async def send_voice(adapter, chat_id, path, reply_to, metadata):
    import base64
    import json
    from discord.http import Route
    from plugins.platforms.discord.adapter_media import _ogg_opus_duration

    try:
        channel = await target(adapter, chat_id, metadata)
        data = await asyncio.to_thread(Path(path).read_bytes)
        duration = _ogg_opus_duration(data)
        if duration is None:
            raise ValueError('strict_transport_unsupported: native voice requires Ogg/Opus')
        if len(data) > adapter._discord_upload_limit_bytes(channel):
            raise ValueError('Discord file exceeds upload limit')
        reference = adapter._reply_reference_for_send(reply_to, channel)
        payload = {'flags': 8192, 'attachments': [{'id': '0', 'filename': 'voice-message.ogg',
            'duration_secs': round(duration, 2), 'waveform': base64.b64encode(bytes([128] * 256)).decode()}]}
        if reference:
            payload['message_reference'] = reference.to_dict()
        form = [{'name': 'payload_json', 'value': json.dumps(payload)},
                {'name': 'files[0]', 'value': data, 'filename': 'voice-message.ogg', 'content_type': 'audio/ogg'}]
        async with single_http(adapter) as http:
            result = await http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id), form=form)
        if not result.get('attachments'):
            raise RuntimeError('Discord voice ACK incomplete')
        return SendResult(success=True, message_id=str(result['id']))
    except Exception as exc:
        logger.exception('Strict native voice delivery stopped; ACK unknown')
        return failed(exc)
