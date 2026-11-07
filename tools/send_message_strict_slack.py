"""Slack standalone plans; SDK encoding and strict sessions stay canonical."""
from dataclasses import dataclass, field
from types import SimpleNamespace


@dataclass(frozen=True)
class Plan:
    client_options: tuple = field(repr=False)
    headers: tuple = field(repr=False)
    default_params: tuple
    chat_id: str
    thread_id: object
    chunks: tuple
    unfurl: tuple
    files: tuple = field(repr=False)
    caption: str


def selected_token(pconfig):
    import json
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    raw = pconfig.token or get_secret('SLACK_BOT_TOKEN', '') or ''
    team = pconfig.extra.get('team_id') or pconfig.extra.get('slack_team_id')
    saved = get_hermes_home() / 'slack_tokens.json'
    try:
        entries = json.loads(saved.read_text(encoding='utf-8-sig')) if saved.is_file() else {}
    except (OSError, ValueError) as exc:
        raise RuntimeError('strict Slack workspace tokens unavailable') from exc
    if team:
        token = entries.get(str(team), {}).get('token')
        if not token:
            raise RuntimeError('strict Slack selected workspace token unavailable')
        return token
    tokens = list(dict.fromkeys([part.strip() for part in raw.split(',') if part.strip()] +
        [entry.get('token') for entry in entries.values() if isinstance(entry, dict) and entry.get('token')]))
    if len(tokens) != 1:
        raise RuntimeError('strict Slack requires one selected workspace token')
    return tokens[0]


def prepare(pconfig, chat_id, message, *, thread_id=None, media_files=None, caption=None, **kwargs):
    from gateway.platforms.base import BasePlatformAdapter, resolve_proxy_url
    from tools.send_message_strict import media_bytes
    from plugins.platforms.slack.adapter import (
        _slack_unfurl_kwargs, _standalone_format_mrkdwn,
    )
    from slack_sdk.web.async_client import AsyncWebClient

    token = selected_token(pconfig)
    if not str(chat_id).startswith(('C', 'G', 'D')):
        raise RuntimeError('strict Slack requires a resolved conversation ID')
    files = media_bytes(media_files)
    unfurl = _slack_unfurl_kwargs(pconfig.extra)
    formatted_caption = _standalone_format_mrkdwn(caption or '')
    text = formatted_caption if formatted_caption and unfurl else _standalone_format_mrkdwn(message)
    chunks = tuple(BasePlatformAdapter.truncate_message(text, 39000)) if text.strip() else ()
    if not chunks and not files:
        raise RuntimeError('strict Slack empty payload')
    source = AsyncWebClient(token=token, proxy=resolve_proxy_url(), retry_handlers=[])
    options = tuple((key, getattr(source, key)) for key in ('token', 'proxy', 'base_url', 'timeout', 'ssl'))
    return Plan(options, tuple(source.headers.items()), tuple(source.default_params.items()),
                str(chat_id), str(thread_id) if thread_id else None, chunks, tuple(unfurl.items()), files,
                formatted_caption if not unfurl else '')


async def send(plan):
    from plugins.platforms.slack.adapter import _standalone_post_kwargs
    from plugins.platforms.slack.strict_delivery import single_client

    delivered = []
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        source = AsyncWebClient(**dict(plan.client_options), headers=dict(plan.headers), retry_handlers=[])
        source.proxy = dict(plan.client_options)['proxy']
        source.default_params = dict(plan.default_params)
        adapter = SimpleNamespace(_client_for=lambda *_: source)
        async with single_client(adapter, plan.chat_id, None) as client:
            for chunk in plan.chunks:
                result = await client.chat_postMessage(**_standalone_post_kwargs(
                    plan.chat_id, chunk, dict(plan.unfurl), plan.thread_id))
                if not isinstance(result.get('ts'), str) or not result['ts']:
                    raise RuntimeError('Slack ACK missing ts')
                delivered.append(result['ts'])
            for index, file in enumerate(plan.files):
                result = await client.files_upload_v2(channel=plan.chat_id, thread_ts=plan.thread_id,
                    file_uploads=[{'content': file.data, 'filename': file.filename}],
                    initial_comment=plan.caption if index == 0 else '')
                files = result.get('files')
                if not isinstance(files, list) or len(files) != 1 or not files[0].get('id'):
                    raise RuntimeError('Slack file ACK incomplete')
                delivered.append(files[0]['id'])
        return {'success': True, 'platform': 'slack', 'chat_id': plan.chat_id,
                'message_id': delivered[-1], 'message_ids': delivered}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Strict Slack standalone stopped; ACK unknown')
        return {'error': 'strict Slack ACK missing: ' + type(exc).__name__,
                'ambiguous': True, 'message_ids': delivered}
