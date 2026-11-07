"""Single-attempt Telegram egress for a caller already holding a durable claim.

A plain Bot bypasses ExtBot rate-limit retries; a private HTTPX transport has
zero retries and no redirects or IP fallback. Human sends keep their live Bot.
"""
from contextlib import asynccontextmanager

from telegram.error import TelegramError

from gateway.platforms.base import SendResult, resolve_proxy_url


@asynccontextmanager
async def single_bot(adapter):
    import httpx
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from agent.ssl_verify import platform_ssl_context

    source = adapter._bot
    if not isinstance(source, Bot):
        raise RuntimeError('strict_transport_unverified: Telegram Bot unavailable')
    proxy = resolve_proxy_url('TELEGRAM_PROXY', target_hosts=['api.telegram.org'],
                              configured=adapter.config.extra.get('proxy_url'))
    transport = httpx.AsyncHTTPTransport(retries=0, proxy=proxy, verify=platform_ssl_context())
    request = HTTPXRequest(read_timeout=60, write_timeout=60, connect_timeout=10,
                           media_write_timeout=60, httpx_kwargs={
                               'transport': transport, 'follow_redirects': False, 'trust_env': False})
    bot = Bot(source.token, base_url=lambda token: source.base_url,
              base_file_url=lambda token: source.base_file_url,
              request=request, get_updates_request=request)
    try:
        yield bot
    finally:
        await request.shutdown()


def preflight(adapter, method, *, chat_id, reply_to=None, metadata=None, **kwargs):
    from telegram import Bot
    from plugins.platforms.telegram.adapter import utf16_len
    if not isinstance(adapter._bot, Bot):
        raise RuntimeError('strict_transport_unverified: Telegram Bot unavailable')
    if method == 'send':
        thread_id = adapter._metadata_thread_id(metadata)
        chunks = adapter.truncate_message(adapter.format_message(kwargs['content']), adapter.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
        for index, _ in enumerate(chunks):
            private, off, anchor = adapter._chunk_reply_routing(chat_id, reply_to, metadata, thread_id, index)
            if private and anchor is None and not off:
                raise ValueError(adapter._dm_topic_missing_anchor_error())
            adapter._thread_kwargs_for_send(chat_id, thread_id, metadata,
                reply_to_message_id=anchor, reply_to_mode=adapter._reply_to_mode)
    else:
        media_routing(adapter, chat_id, None if method == 'send_multiple_images' else reply_to, metadata)
        images = kwargs.get('images', ())
        if method == 'send_multiple_images' and not images:
            raise ValueError('no images to send')
        if method == 'send_multiple_images' and any(adapter._is_animation_url(url) for url, _ in images):
            raise ValueError('strict_transport_unsupported: animation in photo album')
        captions = [kwargs.get('caption')] + [caption for _url, caption in images]
        if any(caption and utf16_len(caption) > 1024 for caption in captions):
            raise ValueError('Telegram caption exceeds 1024 UTF-16 units')


def failed(exc, delivered=()):
    from plugins.platforms.telegram.adapter import _redact_telegram_error_text

    return SendResult(success=False, retryable=False,
                      error='strict_delivery_unknown: ' + _redact_telegram_error_text(exc),
                      raw_response={'message_ids': list(delivered)})


async def send_text(adapter, chat_id, content, reply_to, metadata):
    from telegram.constants import ParseMode
    from plugins.platforms.telegram.adapter import normalize_telegram_chat_id, utf16_len

    delivered = []
    try:
        chunks = adapter.truncate_message(adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
        if not chunks:
            return SendResult(success=True)
        thread_id = adapter._metadata_thread_id(metadata)
        calls = []
        for index, chunk in enumerate(chunks):
            private, off, anchor = adapter._chunk_reply_routing(chat_id, reply_to, metadata, thread_id, index)
            if private and anchor is None and not off:
                raise ValueError(adapter._dm_topic_missing_anchor_error())
            routing = adapter._thread_kwargs_for_send(chat_id, thread_id, metadata,
                reply_to_message_id=anchor, reply_to_mode=adapter._reply_to_mode)
            calls.append(dict(chat_id=normalize_telegram_chat_id(chat_id), text=chunk,
                parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=anchor, **routing,
                **adapter._link_preview_kwargs(), **adapter._notification_kwargs(metadata)))
        async with adapter._chat_send_lock(chat_id), single_bot(adapter) as bot:
            for kwargs in calls:
                msg = await bot.send_message(**kwargs)
                delivered.append(str(msg.message_id))
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except (TelegramError, OSError, ValueError, RuntimeError, TypeError) as exc:
        return failed(exc, delivered)


async def media_source(source):
    from pathlib import Path
    from urllib.parse import unquote
    import asyncio
    from telegram import InputFile
    from tools.url_safety import is_safe_url

    if source.startswith('file://'):
        path = Path(unquote(source[7:]))
        return InputFile(await asyncio.to_thread(path.read_bytes), filename=path.name)
    if not is_safe_url(source):
        raise ValueError('Unsafe media URL')
    return source


def media_routing(adapter, chat_id, reply_to, metadata):
    anchor, routing = adapter._media_send_kwargs(chat_id, reply_to, metadata)
    private, off, _ = adapter._chunk_reply_routing(chat_id, reply_to, metadata,
                                                 adapter._metadata_thread_id(metadata), 0)
    if private and anchor is None and not off:
        raise ValueError(adapter._dm_topic_missing_anchor_error())
    return routing


async def send_media(adapter, kind, chat_id, source, caption, reply_to, metadata, file_name=None):
    from pathlib import Path
    from telegram import InputFile
    import asyncio

    try:
        if caption and len(caption.encode('utf-16-le')) // 2 > 1024:
            raise ValueError('Telegram caption exceeds 1024 UTF-16 units')
        routing = media_routing(adapter, chat_id, reply_to, metadata)
        if kind in {'photo', 'animation_url'}:
            payload = await media_source(source)
            kind = 'photo' if kind == 'photo' else 'animation'
        else:
            path = Path(source)
            payload = InputFile(await asyncio.to_thread(path.read_bytes), filename=file_name or path.name)
            if kind == 'image_file':
                kind = 'photo'
            elif kind == 'voice':
                ext = path.suffix.lower()
                kind = 'voice' if ext in {'.ogg', '.opus'} else 'audio' if ext in {'.mp3', '.m4a'} else 'document'
        async with single_bot(adapter) as bot:
            message = await getattr(bot, 'send_' + kind)(**routing, **{kind: payload}, caption=caption)
        return SendResult(success=True, message_id=str(message.message_id))
    except (TelegramError, OSError, ValueError, RuntimeError, TypeError) as exc:
        return failed(exc)


async def send_images(adapter, chat_id, images, metadata):
    from telegram import InputMediaPhoto

    delivered = []
    try:
        if not images:
            raise ValueError('no images to send')
        routing = media_routing(adapter, chat_id, None, metadata)
        # Snapshot and validate the entire bundle before even its first POST.
        media = []
        for url, caption in images:
            if caption and len(caption.encode('utf-16-le')) // 2 > 1024:
                raise ValueError('Telegram caption exceeds 1024 UTF-16 units')
            if adapter._is_animation_url(url):
                raise ValueError('strict_transport_unsupported: animation in photo album')
            media.append(InputMediaPhoto(await media_source(url), caption=caption or None))
        async with single_bot(adapter) as bot:
            for start in range(0, len(media), 10):
                batch = media[start:start + 10]
                if len(batch) == 1:
                    message = await bot.send_photo(**routing, photo=batch[0].media, caption=batch[0].caption)
                    messages = [message]
                else:
                    messages = await bot.send_media_group(**routing, media=batch)
                if len(messages) != len(batch) or any(not message.message_id for message in messages):
                    raise RuntimeError('Telegram album ACK incomplete')
                delivered.extend(str(message.message_id) for message in messages)
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except (TelegramError, OSError, ValueError, RuntimeError, TypeError) as exc:
        return failed(exc, delivered)
