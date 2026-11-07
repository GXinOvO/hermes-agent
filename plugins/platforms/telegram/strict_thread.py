"""Single-attempt Telegram forum creation and immutable native body egress."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re

from plugins.platforms.discord.strict_thread import (
    _binding, _check_media, _digest, _encode, _media, _plain, _plan, _receipt,
    _receipt_ack, _route_metadata, _runtime, _take, _title,
)


def _positive(value):
    return type(value) is int and value > 0


def _messages(adapter, state, calls, retained, scope, new_thread):
    from plugins.platforms.telegram.adapter import normalize_telegram_chat_id, utf16_len
    from telegram.constants import ParseMode
    messages, identities = [], []
    paths = {'send_document': ('file_path', 'document'), 'send_image_file': ('image_path', 'photo'),
             'send_video': ('video_path', 'video'), 'send_voice': ('audio_path', 'voice')}
    for method, original in calls:
        kwargs = _plain(original)
        metadata = _route_metadata(kwargs, state, scope, new_thread)
        if new_thread and metadata.get('direct_messages_topic_id') is not None:
            raise ValueError('strict native Telegram new forum cannot retain direct topic target')
        if not new_thread and state.thread_id is not None and not metadata.get('direct_messages_topic_id'):
            metadata['thread_id'] = str(state.thread_id)
        if method == 'send':
            text = kwargs['content']
            if not isinstance(text, str) or not text.strip():
                raise ValueError('strict native empty Telegram body')
            chunks = adapter.truncate_message(adapter.format_message(text), adapter.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
            for index, chunk in enumerate(chunks):
                if utf16_len(chunk) > 4096:
                    raise ValueError('strict native Telegram body exceeds UTF16 limit')
                thread = adapter._metadata_thread_id(metadata)
                private, off, anchor = adapter._chunk_reply_routing(state.chat_id, kwargs.get('reply_to'), metadata, thread, index)
                if private and anchor is None and not off:
                    raise ValueError('strict native Telegram DM topic anchor unavailable')
                routing = adapter._thread_kwargs_for_send(state.chat_id, thread, metadata,
                    reply_to_message_id=anchor, reply_to_mode=adapter._reply_to_mode)
                options = {'chat_id': normalize_telegram_chat_id(str(state.chat_id)), 'text': chunk,
                    'parse_mode': ParseMode.MARKDOWN_V2, 'reply_to_message_id': anchor, **routing,
                    **adapter._notification_kwargs(metadata)}
                for key, value in adapter._link_preview_kwargs().items():
                    options[key] = value.to_dict() if hasattr(value, 'to_dict') else value
                messages.append({'method': 'send_message', 'options': options, 'file': None})
            continue
        if method not in paths:
            raise ValueError('strict native unsupported Telegram payload method: ' + method)
        key, kind = paths[method]
        path = kwargs[key]
        name = kwargs.get('file_name') or Path(path).name
        voice = kwargs.get('is_voice', False)
        data = _media(path, voice, name, retained, identities)
        if len(data) > (10 if kind == 'photo' else 50) * 1024 * 1024:
            raise ValueError('strict native Telegram media exceeds native limit')
        if kind == 'voice':
            ext = Path(path).suffix.lower()
            kind = 'voice' if ext in ('.ogg', '.opus') else 'audio' if ext in ('.mp3', '.m4a') else 'document'
        caption = kwargs.get('caption') or ''
        if utf16_len(caption) > 1024:
            raise ValueError('strict native Telegram caption exceeds UTF16 limit')
        from plugins.platforms.telegram.strict_delivery import media_routing
        options = _plain(media_routing(adapter, str(state.chat_id), kwargs.get('reply_to'), metadata))
        options.update(caption=caption)
        messages.append({'method': 'send_' + kind, 'options': options,
                         'file': {'name': name, 'hex': data.hex(), 'kind': kind}})
    _check_media(identities, retained)
    if not messages:
        raise ValueError('strict native empty Telegram plan')
    return messages, identities


def freeze(adapter, state, calls, retained, *, new_thread: bool):
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from gateway.platforms.base import resolve_proxy_url
    from agent.ssl_verify import platform_ssl_context
    source = adapter._bot
    if not isinstance(source, Bot) or not isinstance(source.request, HTTPXRequest):
        raise ValueError('strict native Telegram SDK/request unavailable')
    parent = str(state.chat_id)
    if not re.fullmatch(r'-?[1-9][0-9]*', parent):
        raise ValueError('strict native Telegram concrete numeric parent required')
    if new_thread and state.thread_id is not None:
        raise ValueError('strict native Telegram new forum parent already threaded')
    scope = source.token.split(':', 1)[0]
    if not re.fullmatch(r'[1-9][0-9]*', scope):
        raise ValueError('strict native Telegram bot identity unavailable')
    if state.origin.get('scope_id') not in (None, '', scope):
        raise ValueError('strict native Telegram bot scope mismatch')
    proxy = resolve_proxy_url('TELEGRAM_PROXY', target_hosts=['api.telegram.org'], configured=adapter.config.extra.get('proxy_url'))
    transport = {'token': source.token, 'base_url': source.base_url, 'base_file_url': source.base_file_url,
        'proxy': proxy, 'read_timeout': 60, 'write_timeout': 60, 'connect_timeout': 10,
        'media_write_timeout': 60, 'retry_count': 0, 'redirects': False, 'trust_env': False}
    root = {'method': 'create_forum_topic', 'chat_id': int(parent), 'name': _title(state, 128)} if new_thread else None
    messages, identities = _messages(adapter, state, calls, retained, scope, new_thread)
    if state.thread_id is not None and not re.fullmatch(r'[1-9][0-9]*', str(state.thread_id)):
        raise ValueError('strict native Telegram topic identity invalid')
    bindings = [{'method': message['method'], 'options': message['options'],
        'file': None if message['file'] is None else {'name': message['file']['name'], 'kind': message['file']['kind'],
            'sha256': __import__('hashlib').sha256(bytes.fromhex(message['file']['hex'])).hexdigest()}} for message in messages]
    return _plan(_binding('telegram', state, scope, root, bindings, identities, transport), transport, messages, platform_ssl_context())


@asynccontextmanager
async def _bot(runtime):
    import httpx
    from telegram import Bot
    from telegram.request import HTTPXRequest
    transport = json.loads(runtime.transport)
    responses = []

    class ObservedRequest(HTTPXRequest):
        async def do_request(self, *args, **kwargs):
            status, body = await super().do_request(*args, **kwargs)
            envelope = json.loads(body)
            if not isinstance(envelope, dict) or envelope.get('ok') is not True or 'result' not in envelope:
                raise ValueError('strict native Telegram full ACK invalid')
            responses.append(_plain(envelope))
            return status, body

    http = httpx.AsyncHTTPTransport(retries=0, proxy=transport['proxy'], verify=runtime.ssl)
    request = ObservedRequest(read_timeout=transport['read_timeout'], write_timeout=transport['write_timeout'],
        connect_timeout=transport['connect_timeout'], media_write_timeout=transport['media_write_timeout'],
        httpx_kwargs={'transport': http, 'follow_redirects': False, 'trust_env': False})
    bot = Bot(transport['token'], base_url=lambda token: transport['base_url'],
              base_file_url=lambda token: transport['base_file_url'], request=request, get_updates_request=request)
    try:
        yield bot, responses
    finally:
        await request.shutdown()


def supports_new_thread(adapter, chat_id):
    return True


def validate_plan(binding):
    for message in binding['messages']:
        if not isinstance(message, dict) or set(message) != {'method', 'options', 'file'}:
            raise ValueError('strict native Telegram message shape invalid')
        if not isinstance(message['method'], str) or not isinstance(message['options'], dict):
            raise ValueError('strict native Telegram message fields invalid')
        spec = message['file']
        if spec is not None and (not isinstance(spec, dict) or set(spec) != {'name', 'kind', 'sha256'}
                                 or not isinstance(spec['name'], str) or not spec['name']
                                 or not isinstance(spec['kind'], str)
                                 or not isinstance(spec['sha256'], str)
                                 or not re.fullmatch('[0-9a-f]{64}', spec['sha256'])):
            raise ValueError('strict native Telegram file shape invalid')


def validate_receipt(binding, receipt):
    """ForumTopic has no native parent field: bind the exact request and full response."""
    ack = _receipt_ack(binding, receipt, 'telegram')
    envelope, spec = ack.get('response'), binding['root']
    if (not isinstance(envelope, dict) or envelope.get('ok') is not True
            or not isinstance(envelope.get('result'), dict) or not isinstance(spec, dict)
            or set(spec) - {'method', 'chat_id', 'name'} != set()
            or not isinstance(spec.get('name'), str) or not spec['name']
            or type(spec.get('chat_id')) is not int):
        raise ValueError('strict native Telegram full root ACK invalid')
    response = envelope['result']
    root = response.get('message_thread_id')
    if (not _positive(root) or receipt['root'] != str(root) or response.get('name') != spec['name']
            or type(response.get('icon_color')) is not int or not 0 <= response['icon_color'] <= 0xFFFFFF
            or ('chat_id' in response and (type(response['chat_id']) is not int or response['chat_id'] != spec['chat_id']))
            or str(spec['chat_id']) != binding['parent']):
        raise ValueError('strict native Telegram topic/root/parent ACK invalid')


async def create(plan):
    runtime = _runtime(plan, 'telegram')
    binding = json.loads(runtime.binding)
    spec = json.loads(runtime.root)
    if spec is None:
        raise ValueError('strict native create requires new-thread plan')
    _take(runtime, 'create')
    async with _bot(runtime) as (bot, responses):
        topic = await bot.create_forum_topic(chat_id=spec['chat_id'], name=spec['name'])
    receipt = _receipt(binding, str(topic.message_thread_id), responses[-1])
    validate_receipt(binding, receipt)
    return receipt


async def send(plan, root=None):
    from telegram import InputFile, LinkPreviewOptions
    runtime = _runtime(plan, 'telegram')
    binding = json.loads(runtime.binding)
    if json.loads(runtime.root) is not None:
        validate_receipt(binding, root)
        topic = int(root['root'])
    else:
        if root is not None:
            raise ValueError('strict native old target cannot override root')
        topic = None
    _take(runtime, 'send')
    delivered = []
    async with _bot(runtime) as (bot, responses):
        for encoded in runtime.messages:
            message = json.loads(encoded)
            options = message['options']
            if topic is not None:
                options['message_thread_id'] = topic
            if isinstance(options.get('link_preview_options'), dict):
                options['link_preview_options'] = LinkPreviewOptions.de_json(options['link_preview_options'])
            file = message['file']
            if file:
                options[file['kind']] = InputFile(bytes.fromhex(file['hex']), filename=file['name'])
            await getattr(bot, message['method'])(**options)
            response = responses[-1]['result']
            if (not isinstance(response, dict) or not _positive(response.get('message_id'))
                    or not isinstance(response.get('chat'), dict)
                    or type(response['chat'].get('id')) is not int
                    or str(response['chat']['id']) != binding['parent']):
                raise ValueError('strict native Telegram body parent ACK invalid')
            expected = options.get('message_thread_id') or options.get('direct_messages_topic_id')
            actual = response.get('message_thread_id')
            if options.get('direct_messages_topic_id'):
                actual = (response.get('direct_messages_topic') or {}).get('topic_id')
            if (expected is not None and (type(actual) is not int or actual != int(expected))) or (expected is None and actual is not None):
                raise ValueError('strict native Telegram body topic ACK invalid')
            if file and not response.get(file['kind']):
                raise ValueError('strict native Telegram media ACK incomplete')
            delivered.append(str(response['message_id']))
    return {'ok': True, 'message_id': delivered[0]}
