"""Discord standalone plans using the canonical single-attempt SDK client."""
from dataclasses import dataclass, field
from types import SimpleNamespace


@dataclass(frozen=True)
class Plan:
    token: str = field(repr=False)
    proxy: object = field(repr=False)
    chat_id: str
    target_id: str
    chunks: tuple
    files: tuple = field(repr=False)
    caption: object


def prepare(pconfig, chat_id, message, *, thread_id=None, media_files=None, caption=None, **kwargs):
    from agent.secret_scope import get_secret
    from gateway.platforms.base import BasePlatformAdapter, resolve_proxy_url
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tools.send_message_strict import media_bytes

    token = (pconfig.token or get_secret('DISCORD_BOT_TOKEN', '') or '').strip()
    target_id = str(thread_id or chat_id)
    if not token or not target_id.isascii() or not target_id.isdecimal():
        raise RuntimeError('strict Discord destination unavailable')
    files = media_bytes(media_files)
    chunks = tuple(BasePlatformAdapter.truncate_message(message, DiscordAdapter.MAX_MESSAGE_LENGTH)) if message.strip() else ()
    if (not chunks and not files) or len(chunks) > DiscordAdapter.MAX_SPLIT_MESSAGES:
        raise RuntimeError('strict Discord payload unavailable or split limit exceeded')
    return Plan(token, resolve_proxy_url(platform_env_var='DISCORD_PROXY'), str(chat_id), target_id, chunks, files, caption)


async def send(plan):
    import asyncio
    import io
    from discord import File
    from discord.http import HTTPClient, handle_message_parameters
    from plugins.platforms.discord.strict_delivery import single_http

    delivered = []
    try:
        source = HTTPClient(asyncio.get_running_loop(), proxy=plan.proxy)
        source.token = plan.token
        source._global_over = asyncio.Event()
        source._global_over.set()
        adapter = SimpleNamespace(_client=SimpleNamespace(http=source))
        async with single_http(adapter) as http:
            for chunk in plan.chunks:
                with handle_message_parameters(content=chunk) as params:
                    result = await http.send_message(int(plan.target_id), params=params)
                if not isinstance(result.get('id'), str) or not result['id']:
                    raise RuntimeError('Discord ACK missing id')
                delivered.append(result['id'])
            for index, media in enumerate(plan.files):
                file = File(io.BytesIO(media.data), filename=media.filename)
                with handle_message_parameters(content=plan.caption if index == 0 else None, files=[file]) as params:
                    result = await http.send_message(int(plan.target_id), params=params)
                if not isinstance(result.get('id'), str) or not result['id'] or len(result.get('attachments', [])) != 1:
                    raise RuntimeError('Discord attachment ACK incomplete')
                delivered.append(result['id'])
        return {'success': True, 'platform': 'discord', 'chat_id': plan.chat_id,
                'message_id': delivered[-1], 'message_ids': delivered}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Strict Discord standalone stopped; ACK unknown')
        return {'error': 'strict Discord ACK missing: ' + type(exc).__name__,
                'ambiguous': True, 'message_ids': delivered}
