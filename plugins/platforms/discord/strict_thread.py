"""Frozen native Discord requests for an already-owned durable delivery stage.

The small value/consumption utilities are shared by the sibling native helpers;
no runtime plan retains an adapter, live SDK client or mutable caller payload.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import threading


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _plain(value):
    return json.loads(_encode(value))


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value) is not None


def _event_id(value):
    return isinstance(value, str) and re.fullmatch(r'\$[^\s\x00-\x1f]+', value) is not None


@dataclass
class _Consumption:
    lock: object = field(default_factory=threading.Lock)
    attempted: set = field(default_factory=set)


@dataclass(frozen=True)
class _Runtime:
    binding: bytes
    transport: bytes = field(repr=False)
    root: bytes
    messages: tuple
    ssl: object
    consumption: _Consumption = field(default_factory=_Consumption)


def _plan(binding, transport, messages, ssl):
    return {'binding': _plain(binding), '_runtime': _Runtime(_encode(binding), _encode(transport),
        _encode(binding['root']), tuple(_encode(message) for message in messages), ssl)}


def _runtime(plan, platform):
    runtime = plan.get('_runtime') if isinstance(plan, dict) else None
    if (not isinstance(runtime, _Runtime) or _encode(plan.get('binding')) != runtime.binding
            or plan['binding'].get('platform') != platform):
        raise ValueError('strict native plan identity mismatch')
    return runtime


def _take(runtime, stage):
    with runtime.consumption.lock:
        if stage in runtime.consumption.attempted:
            raise RuntimeError('strict native stage already attempted; ACK unknown must not retry')
        runtime.consumption.attempted.add(stage)


def _binding(platform, state, scope, root, messages, media, transport):
    parent = str(state.chat_id)
    origin = _plain(state.origin)
    chat_type = getattr(state, 'target_chat_type', None) or ('dm' if state.is_dm_target else 'group')
    if chat_type not in ('dm', 'group', 'thread', 'channel'):
        raise ValueError('strict native target chat type unavailable')
    if state.platform_name != platform or not isinstance(state.job, dict):
        raise ValueError('strict native platform/job mismatch')
    return {'version': 1, 'platform': platform, 'parent': parent, 'scope_id': scope,
        'chat_type': chat_type, 'thread_id': str(state.thread_id) if state.thread_id is not None else None,
        'root': root, 'messages': _plain(messages), 'media': media, 'transport_sha256': _digest(transport),
        'route_sha256': _digest({'job': state.job, 'origin': origin, 'parent': parent,
                                'scope_id': scope, 'chat_type': chat_type})}


def _validate_binding(binding, platform):
    required = {'version', 'platform', 'parent', 'scope_id', 'chat_type', 'thread_id', 'root',
                'messages', 'media', 'transport_sha256', 'route_sha256'}
    if (not isinstance(binding, dict) or set(binding) != required or type(binding['version']) is not int
            or binding['version'] != 1 or binding['platform'] != platform
            or not isinstance(binding['parent'], str) or not binding['parent']
            or not isinstance(binding['scope_id'], str) or binding['chat_type'] not in ('dm', 'group', 'thread', 'channel')
            or not isinstance(binding['root'], dict) or not isinstance(binding['messages'], list)
            or not isinstance(binding['media'], list)):
        raise ValueError('strict native saved binding invalid')
    for name in ('transport_sha256', 'route_sha256'):
        if not isinstance(binding[name], str) or not re.fullmatch('[0-9a-f]{64}', binding[name]):
            raise ValueError('strict native binding fingerprint invalid')


def _receipt(binding, root, response, **extra):
    return {'root': root, 'ack': {'binding_sha256': _digest(binding), 'platform': binding['platform'],
        'parent': binding['parent'], 'scope_id': binding['scope_id'], 'request': binding['root'],
        'response': _plain(response), **extra}}


def _receipt_ack(binding, receipt, platform):
    _validate_binding(binding, platform)
    if (not isinstance(receipt, dict) or set(receipt) != {'root', 'ack'}
            or not isinstance(receipt['ack'], dict)):
        raise ValueError('strict native full root receipt required')
    ack = receipt['ack']
    expected = {'binding_sha256': _digest(binding), 'platform': platform, 'parent': binding['parent'],
                'scope_id': binding['scope_id'], 'request': binding['root']}
    if any(_encode(ack.get(key)) != _encode(value) for key, value in expected.items()):
        raise ValueError('strict native root receipt binding mismatch')
    return ack


def _media(path, voice, name, retained, identities):
    """Read once, compare stat identity around the read, then retain the bytes."""
    path = str(path)
    source = Path(path)
    before = source.stat()
    with source.open('rb') as stream:
        import os
        opened = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    current = source.stat()
    def identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if not (identity(before) == identity(opened) == identity(after) == identity(current)):
        raise ValueError('strict native media changed during freeze')
    index = len(identities)
    if index >= len(retained) or [path, voice] != [str(retained[index][0]), retained[index][1]] or type(voice) is not bool:
        raise ValueError('strict native retained media order/voice mismatch')
    identities.append([path, voice, hashlib.sha256(data).hexdigest(), name])
    return data


def _check_media(identities, retained):
    if len(identities) != len(retained):
        raise ValueError('strict native retained media coverage mismatch')


def _title(state, limit):
    name = state.job.get('name') or state.job.get('id')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('strict native root name unavailable')
    return ('Hermes — ' + name.strip())[:limit]


def _route_metadata(kwargs, state, scope, new_thread):
    if type(new_thread) is not bool:
        raise ValueError('strict native new_thread must be boolean')
    metadata = _plain(kwargs.get('metadata') or {})
    if not isinstance(metadata, dict):
        raise ValueError('strict native metadata shape invalid')
    if metadata.get('scope_id') not in (None, '', scope):
        raise ValueError('strict native metadata scope mismatch')
    thread = metadata.get('thread_id')
    expected = str(state.thread_id) if state.thread_id is not None else None
    if thread is not None and str(thread) != expected:
        raise ValueError('strict native metadata thread mismatch')
    if new_thread and (expected or kwargs.get('reply_to')):
        raise ValueError('strict native new root cannot retain old reply target')
    return metadata


def _discord_payloads(adapter, state, calls, retained, scope, new_thread, channel):
    import base64
    messages, identities = [], []
    paths = {'send_document': 'file_path', 'send_image_file': 'image_path', 'send_video': 'video_path', 'send_voice': 'audio_path'}
    for method, original in calls:
        kwargs = _plain(original)
        _route_metadata(kwargs, state, scope, new_thread)
        reference = adapter._reply_reference_for_send(kwargs.get('reply_to'), channel)
        ref = reference.to_dict() if reference else None
        if method == 'send':
            text = kwargs['content']
            if not isinstance(text, str) or not text.strip():
                raise ValueError('strict native empty Discord body')
            chunks = adapter.truncate_message(adapter.format_message(text), adapter.MAX_MESSAGE_LENGTH)
            if len(chunks) > adapter.MAX_SPLIT_MESSAGES or any(len(chunk) > 2000 for chunk in chunks):
                raise ValueError('strict native Discord body exceeds split limit')
            for index, chunk in enumerate(chunks):
                payload = {'content': chunk, 'tts': False}
                if ref and (index == 0 or adapter._reply_to_mode == 'all'):
                    payload['message_reference'] = ref
                messages.append({'payload': payload, 'files': []})
            continue
        if method not in paths:
            raise ValueError('strict native unsupported Discord payload method: ' + method)
        path = kwargs[paths[method]]
        name = kwargs.get('file_name') or Path(path).name
        voice = kwargs.get('is_voice', False)
        data = _media(path, voice, name, retained, identities)
        if len(data) > adapter._discord_upload_limit_bytes(channel):
            raise ValueError('strict native Discord media exceeds upload limit')
        caption = kwargs.get('caption') or ''
        if len(caption) > 2000:
            raise ValueError('strict native Discord caption exceeds body limit')
        attachment = {'id': 0, 'filename': name}
        payload = {'content': caption, 'tts': False, 'attachments': [attachment]}
        if method == 'send_voice':
            from plugins.platforms.discord.adapter_media import _ogg_opus_duration
            duration = _ogg_opus_duration(data)
            if duration is None:
                raise ValueError('strict native unsupported Discord voice: Ogg/Opus required')
            attachment.update(duration_secs=round(duration, 2), waveform=base64.b64encode(bytes([128] * 256)).decode())
            payload.pop('content')
            payload['flags'] = 8192
        if ref:
            payload['message_reference'] = ref
        messages.append({'payload': payload, 'files': [{'name': name, 'hex': data.hex()}]})
    _check_media(identities, retained)
    if not messages:
        raise ValueError('strict native empty Discord plan')
    return messages, identities


def _mention_targets(value):
    """AllowedMentions stores False, True (all) or an id list; keep all three distinct."""
    if isinstance(value, bool):
        return value
    return [str(item.id if hasattr(item, 'id') else item) for item in value]


def _allowed_mentions_spec(adapter):
    """Client-wide mention policy is a delivery safety input, not adapter lookaside."""
    source = getattr(getattr(adapter, '_client', None), '_connection', None)
    allowed = getattr(source, 'allowed_mentions', None)
    if allowed is None:
        return None
    return {'everyone': bool(getattr(allowed, 'everyone', False)),
            'users': _mention_targets(getattr(allowed, 'users', False)),
            'roles': _mention_targets(getattr(allowed, 'roles', False)),
            'replied_user': bool(getattr(allowed, 'replied_user', False))}


def _mention_arg(value):
    import discord
    if isinstance(value, bool):
        return value
    return [discord.Object(int(item)) for item in value]


def supports_new_thread(adapter, chat_id):
    import discord
    channel = adapter._client.get_channel(int(chat_id))
    return type(channel) is discord.TextChannel


def validate_plan(binding):
    """Bounded ordinary/new-thread plan schema; reject shapes the sender cannot consume."""
    for message in binding['messages']:
        if not isinstance(message, dict) or set(message) != {'payload', 'files'}:
            raise ValueError('strict native Discord message shape invalid')
        payload, files = message['payload'], message['files']
        if not isinstance(payload, dict) or set(payload) - {'content', 'tts', 'attachments', 'message_reference', 'flags'}:
            raise ValueError('strict native Discord payload shape invalid')
        if not isinstance(files, list) or any(not isinstance(item, dict) or set(item) != {'name', 'sha256'}
                                              or not isinstance(item['sha256'], str)
                                              or not re.fullmatch('[0-9a-f]{64}', item['sha256']) for item in files):
            raise ValueError('strict native Discord files shape invalid')
        if 'content' in payload and not isinstance(payload['content'], str):
            raise ValueError('strict native Discord content shape invalid')
        if type(payload.get('tts')) is not bool:
            raise ValueError('strict native Discord tts shape invalid')
        if len(files) != len([item for item in payload.get('attachments', [])]) and files:
            raise ValueError('strict native Discord attachment count mismatch')


def freeze(adapter, state, calls, retained, *, new_thread: bool):
    """Snapshot native direct-create and all body/media inputs without network I/O."""
    import discord
    from discord.http import HTTPClient, Route
    from agent.ssl_verify import platform_ssl_context
    source = getattr(getattr(adapter, '_client', None), 'http', None)
    if not isinstance(source, HTTPClient) or not source.token or not _id(str(state.chat_id)):
        raise ValueError('strict native Discord SDK/parent unavailable')
    channel = adapter._client.get_channel(int(state.chat_id))
    if new_thread and (type(channel) is not discord.TextChannel or state.thread_id):
        raise ValueError('strict native unsupported Discord parent: cached text channel required')
    if channel is None:
        raise ValueError('strict native Discord cached target unavailable')
    scope = str(channel.guild.id) if getattr(channel, 'guild', None) else ''
    if state.origin.get('scope_id') not in (None, '', scope):
        raise ValueError('strict native Discord workspace mismatch')
    if new_thread and (not scope or adapter._client.user is None):
        raise ValueError('strict native Discord owner identity unavailable')
    root = {'method': 'start_thread_without_message', 'parent': str(state.chat_id), 'name': _title(state, 80),
        'auto_archive_duration': 1440, 'type': 11, 'invitable': True, 'rate_limit_per_user': None,
        'owner_id': str(adapter._client.user.id)} if new_thread else None
    if root and not _id(root['owner_id']):
        raise ValueError('strict native Discord owner identity invalid')
    messages, media = _discord_payloads(adapter, state, calls, retained, scope, new_thread, channel)
    target = str(state.thread_id) if state.thread_id is not None else str(state.chat_id)
    if not _id(target):
        raise ValueError('strict native Discord body target invalid')
    transport = {'base_url': Route.BASE, 'token': str(source.token), 'user_agent': str(source.user_agent),
        'proxy': str(source.proxy) if source.proxy else None,
        'proxy_auth': list(source.proxy_auth) if source.proxy_auth else None,
        'timeout': 60, 'retry_count': 0, 'redirects': False, 'target': target,
        'allowed_mentions': _allowed_mentions_spec(adapter)}
    binding_messages = [{'payload': message['payload'], 'files': [{'name': item['name'],
        'sha256': hashlib.sha256(bytes.fromhex(item['hex'])).hexdigest()} for item in message['files']]} for message in messages]
    binding = _binding('discord', state, scope, root, binding_messages, media, transport)
    return _plan(binding, transport, messages, platform_ssl_context())


@asynccontextmanager
async def _http(runtime):
    import asyncio
    import aiohttp
    from discord.http import HTTPClient
    transport = json.loads(runtime.transport)
    connector = aiohttp.TCPConnector(ssl=runtime.ssl)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=transport['timeout']),
                                     trust_env=False) as session:
        if not hasattr(session, '_retry_connection'):
            raise RuntimeError('strict native aiohttp retry control unavailable')
        session._retry_connection = False

        class SingleAttemptHTTP(HTTPClient):
            async def request(self, route, *, files=None, form=None, **kwargs):
                from discord.errors import HTTPException
                if route.method != 'POST' or '/channels/' not in route.url:
                    raise ValueError('strict native Discord unexpected SDK request')
                url = transport['base_url'] + '/channels/' + route.url.split('/channels/', 1)[1]
                headers = {'Authorization': 'Bot ' + transport['token'], 'User-Agent': transport['user_agent']}
                reason = kwargs.pop('reason', None)
                if reason:
                    from urllib.parse import quote
                    headers['X-Audit-Log-Reason'] = quote(reason)
                if form:
                    data = aiohttp.FormData(quote_fields=False)
                    for part in form:
                        data.add_field(**part)
                    kwargs['data'] = data
                if transport['proxy']:
                    kwargs['proxy'] = transport['proxy']
                if transport['proxy_auth']:
                    kwargs['proxy_auth'] = aiohttp.BasicAuth(*transport['proxy_auth'])
                async with session.request(route.method, url, headers=headers, allow_redirects=False, **kwargs) as response:
                    data = await response.json(content_type=None)
                    if not 200 <= response.status < 300:
                        raise HTTPException(response, data)
                    return data

        yield SingleAttemptHTTP(asyncio.get_running_loop())


def validate_receipt(binding, receipt):
    """Check native thread ACK and exact saved route/root specification."""
    ack = _receipt_ack(binding, receipt, 'discord')
    response, spec = ack.get('response'), binding['root']
    if (not isinstance(spec, dict) or spec.get('method') != 'start_thread_without_message'
            or spec.get('parent') != binding['parent'] or not _id(binding['scope_id'])):
        raise ValueError('strict native Discord root specification invalid')
    if (not isinstance(response, dict) or not _id(receipt['root'])
            or response.get('id') != receipt['root'] or response.get('parent_id') != binding['parent']
            or response.get('guild_id') != binding['scope_id'] or response.get('owner_id') != spec.get('owner_id')
            or response.get('name') != spec.get('name') or type(response.get('type')) is not int
            or response['type'] != spec.get('type')):
        raise ValueError('strict native Discord full root ACK invalid')
    metadata = response.get('thread_metadata')
    if (not isinstance(metadata, dict) or metadata.get('archived') is not False
            or metadata.get('locked') is not False or type(metadata.get('auto_archive_duration')) is not int
            or metadata['auto_archive_duration'] != spec['auto_archive_duration']
            or not isinstance(metadata.get('archive_timestamp'), str) or not metadata['archive_timestamp']):
        raise ValueError('strict native Discord root metadata ACK invalid')


async def create(plan):
    runtime = _runtime(plan, 'discord')
    binding = json.loads(runtime.binding)
    spec = json.loads(runtime.root)
    if spec is None:
        raise ValueError('strict native create requires new-thread plan')
    _take(runtime, 'create')
    async with _http(runtime) as http:
        response = await http.start_thread_without_message(int(spec['parent']), name=spec['name'],
            auto_archive_duration=spec['auto_archive_duration'], type=spec['type'], invitable=spec['invitable'],
            rate_limit_per_user=spec['rate_limit_per_user'], reason='Hermes strict handoff')
    receipt = _receipt(binding, response.get('id') if isinstance(response, dict) else None, response)
    validate_receipt(binding, receipt)
    return receipt


async def send(plan, root=None):
    """For a new thread, root is the complete validated create receipt, never a bare id."""
    import io
    import discord
    from discord.http import handle_message_parameters
    from discord.utils import MISSING
    runtime = _runtime(plan, 'discord')
    transport = json.loads(runtime.transport)
    binding = json.loads(runtime.binding)
    if json.loads(runtime.root) is not None:
        validate_receipt(binding, root)
        channel = root['root']
    else:
        if root is not None:
            raise ValueError('strict native old target cannot override root')
        channel = transport['target']
    _take(runtime, 'send')
    delivered = []
    async with _http(runtime) as http:
        for encoded in runtime.messages:
            message = json.loads(encoded)
            payload = message['payload']
            files = [discord.File(io.BytesIO(bytes.fromhex(item['hex'])), filename=item['name']) for item in message['files']]
            try:
                if payload.get('flags') == 8192:
                    from discord.http import Route
                    form = [{'name': 'payload_json', 'value': json.dumps(payload)}, {'name': 'files[0]',
                        'value': bytes.fromhex(message['files'][0]['hex']), 'filename': message['files'][0]['name'],
                        'content_type': 'audio/ogg'}]
                    data = await http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=int(channel)), form=form)
                else:
                    allowed = transport.get('allowed_mentions')
                    mention = MISSING
                    if allowed is not None:
                        mention = discord.AllowedMentions(
                            everyone=allowed['everyone'],
                            users=_mention_arg(allowed['users']),
                            roles=_mention_arg(allowed['roles']),
                            replied_user=allowed['replied_user'])
                    options = {'content': payload.get('content'), 'files': files or MISSING,
                               'message_reference': payload.get('message_reference', MISSING)}
                    if mention is not MISSING:
                        options['previous_allowed_mentions'] = mention
                    with handle_message_parameters(**options) as params:
                        data = await http.send_message(int(channel), params=params)
                if (not isinstance(data, dict) or not _id(data.get('id')) or data.get('channel_id') != channel
                        or data.get('content', '') != payload.get('content', '')
                        or not isinstance(data.get('attachments'), list) or len(data['attachments']) != len(files)):
                    raise ValueError('strict native Discord body ACK invalid')
                if files and [item.get('filename') for item in data['attachments']] != [item['name'] for item in message['files']]:
                    raise ValueError('strict native Discord media ACK incomplete')
                delivered.append(data['id'])
            finally:
                for file in files:
                    file.close()
    return {'ok': True, 'message_id': delivered[0]}
