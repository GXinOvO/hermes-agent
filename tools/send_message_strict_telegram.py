"""Telegram cold standalone: freeze the entire request plan before claiming."""
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    token: str = field(repr=False)
    proxy: object = field(repr=False)
    api: str
    common: tuple
    chunks: tuple
    parse_mode: object
    disable_link_previews: bool
    files: tuple = field(repr=False)


def prepare(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False, **kwargs):
    from gateway.platforms.base import BasePlatformAdapter, utf16_len, resolve_proxy_url
    from plugins.platforms.telegram.standalone import (_telegram_format, _telegram_thread_kwargs,
        _IMAGE_EXTS, _VIDEO_EXTS, _VOICE_EXTS, _TELEGRAM_SEND_AUDIO_EXTS)
    from tools.send_message_strict import media_bytes, TELEGRAM_API
    from plugins.platforms.telegram.telegram_network import proxy_target_hosts
    from plugins.platforms.telegram.telegram_outbound import notification_kwargs, resolve_notifications_mode

    token = pconfig.token
    if not isinstance(token, str) or not token.strip() or not chat_id:
        raise RuntimeError('strict Telegram destination unavailable')
    formatted, mode, _html = _telegram_format(message)
    chunks = tuple(BasePlatformAdapter.truncate_message(formatted, 4096, len_fn=utf16_len)) if formatted.strip() else ()
    frozen = media_bytes(media_files)
    files = []
    for file in frozen:
        ext = Path(file.path).suffix.lower()
        kind = 'document' if force_document else next((kind for extensions, kind in (
            (_IMAGE_EXTS, 'photo'), (_VIDEO_EXTS, 'video'),
            (_VOICE_EXTS if file.voice else (), 'voice'), (_TELEGRAM_SEND_AUDIO_EXTS, 'audio')) if ext in extensions), 'document')
        files.append(replace(file, kind=kind))
    if not chunks and not files:
        raise RuntimeError('strict Telegram empty payload')
    extra = pconfig.extra or {}
    base_url = extra.get('base_url')
    if base_url is not None and not isinstance(base_url, str):
        raise RuntimeError('strict Telegram base URL unavailable')
    notifications = notification_kwargs(resolve_notifications_mode(), kwargs.get('notify'))
    common = tuple({'chat_id': str(chat_id), **_telegram_thread_kwargs(thread_id),
                    **{key: 'true' if value is True else str(value) for key, value in notifications.items()}}.items())
    proxy = resolve_proxy_url('TELEGRAM_PROXY', target_hosts=proxy_target_hosts(base_url), configured=extra.get('proxy_url'))
    return Plan(token, proxy, base_url or TELEGRAM_API + '/bot', common, chunks, mode,
                bool(extra.get('disable_link_previews')), tuple(files))


async def send(plan):
    import aiohttp
    delivered = []

    async def post(session, operation, data):
        async with session.post(f'{plan.api}{plan.token}/{operation}',
                data=data, proxy=plan.proxy, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError('HTTP ' + str(response.status))
            body = await response.json()
            result = body.get('result') if body.get('ok') is True else None
            if not isinstance(result, dict) or type(result.get('message_id')) is not int:
                raise RuntimeError('invalid Telegram ACK')
            return str(result['message_id'])

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=False) as session:
            for text in plan.chunks:
                data = {**dict(plan.common), 'text': text}
                if plan.parse_mode:
                    data['parse_mode'] = plan.parse_mode
                if plan.disable_link_previews:
                    data['disable_web_page_preview'] = 'true'
                delivered.append(await post(session, 'sendMessage', data))
            for file in plan.files:
                data = aiohttp.FormData()
                for key, value in plan.common:
                    data.add_field(key, str(value))
                data.add_field(file.kind, file.data, filename=file.filename, content_type=file.content_type)
                delivered.append(await post(session, 'send' + file.kind.capitalize(), data))
        return {'success': True, 'platform': 'telegram', 'message_id': delivered[-1], 'message_ids': delivered}
    except (aiohttp.ClientError, TimeoutError, OSError, RuntimeError, ValueError, TypeError) as exc:
        return {'error': 'strict Telegram ACK missing: ' + type(exc).__name__,
                'ambiguous': True, 'message_ids': delivered}
