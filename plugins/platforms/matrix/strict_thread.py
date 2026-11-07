"""Frozen plaintext Matrix seed/relation requests using native mautrix HTTPAPI.

E2EE and media are refused before any mutation. Native PUT ACKs contain only an
id, so a read-only getEvent binds each successful ACK to its room/sender/content.
"""
from contextlib import asynccontextmanager
import json
import re

from plugins.platforms.discord.strict_thread import (
    _binding, _digest, _encode, _event_id, _plain, _plan, _receipt, _receipt_ack,
    _route_metadata, _runtime, _take, _title,
)
from plugins.platforms.matrix.strict_delivery import _NoRedirectSession


def freeze(adapter, state, calls, retained, *, new_thread: bool):
    from mautrix.api import HTTPAPI
    from agent.ssl_verify import platform_ssl_context
    client = adapter._client
    if client is None or not isinstance(client.api, HTTPAPI):
        raise ValueError('strict native Matrix HTTPAPI unavailable')
    if adapter._encryption or getattr(client, 'crypto', None):
        raise ValueError('strict native unsupported Matrix E2EE branch')
    if retained:
        raise ValueError('strict native unsupported Matrix media branch')
    parent, sender = str(state.chat_id), str(client.mxid)
    if not re.fullmatch(r'![^\s:]+:[^\s]+', parent) or not re.fullmatch(r'@[^\s:]+:[^\s]+', sender):
        raise ValueError('strict native Matrix room/sender identity invalid')
    scope = sender.split(':', 1)[1]
    if state.origin.get('scope_id') not in (None, '', scope):
        raise ValueError('strict native Matrix homeserver scope mismatch')
    if state.thread_id is not None and not _event_id(str(state.thread_id)):
        raise ValueError('strict native Matrix old root invalid')
    if new_thread and state.thread_id is not None:
        raise ValueError('strict native Matrix new root cannot retain old thread')
    proxy = adapter._proxy_url
    if proxy and not proxy.startswith(('http://', 'https://')):
        raise ValueError('strict native unsupported Matrix proxy branch')
    api = client.api
    if not isinstance(api.token, str) or not api.token:
        raise ValueError('strict native Matrix token unavailable')
    transport = {'base_url': str(api.base_url), 'token': api.token, 'proxy': proxy, 'sender': sender,
        'as_user_id': str(api.as_user_id) if api.as_user_id else None,
        'as_device_id': str(api.as_device_id) if api.as_device_id else None,
        'timeout': 60, 'retry_count': 0, 'redirects': False, 'trust_env': False}
    root_content = _plain(adapter._build_text_message_content(_title(state, 128)))
    root = {'method': 'send_message_event', 'room_id': parent, 'event_type': 'm.room.message',
        'content': root_content, 'sender': sender,
        'txn_id': 'hermes-root-' + _digest({'job': state.job, 'parent': parent, 'content': root_content})} if new_thread else None
    messages = []
    for method, original in calls:
        kwargs = _plain(original)
        metadata = _route_metadata(kwargs, state, scope, new_thread)
        if method != 'send':
            raise ValueError('strict native unsupported Matrix payload method: ' + method)
        text = kwargs['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('strict native empty Matrix body')
        if state.thread_id is not None:
            metadata['thread_id'] = str(state.thread_id)
        chunks = adapter.truncate_message(adapter.format_message(text), adapter.max_message_length, len_fn=adapter.message_len_fn)
        for chunk in chunks:
            content = adapter._build_text_message_content(chunk)
            adapter._apply_relation_metadata(content, reply_to=kwargs.get('reply_to'), metadata=metadata)
            if len(_encode(content)) > 60000:
                raise ValueError('strict native Matrix event exceeds body byte limit')
            messages.append(_plain(content))
    if not messages:
        raise ValueError('strict native empty Matrix plan')
    return _plan(_binding('matrix', state, scope, root, messages, [], transport), transport, messages, platform_ssl_context())


@asynccontextmanager
async def _api(runtime):
    import aiohttp
    from mautrix.api import HTTPAPI
    transport = json.loads(runtime.transport)
    connector = aiohttp.TCPConnector(ssl=runtime.ssl)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=transport['timeout']),
                                     proxy=transport['proxy'], trust_env=False) as session:
        if not hasattr(session, '_retry_connection'):
            raise RuntimeError('strict native aiohttp retry control unavailable')
        session._retry_connection = False
        yield HTTPAPI(transport['base_url'], token=transport['token'], client_session=_NoRedirectSession(session),
            default_retry_count=0, as_user_id=transport['as_user_id'], as_device_id=transport['as_device_id'])


def _validate_event(event, parent, event_id, sender, content):
    if (not isinstance(event, dict) or not _event_id(event_id) or event.get('event_id') != event_id
            or event.get('room_id') != parent or event.get('sender') != sender
            or event.get('type') != 'm.room.message' or _encode(event.get('content')) != _encode(content)):
        raise ValueError('strict native Matrix complete event ACK invalid')


def supports_new_thread(adapter, chat_id):
    return True


def validate_plan(binding):
    for message in binding['messages']:
        if not isinstance(message, dict) or not message or set(message) - {'msgtype', 'body', 'format',
                                                                           'formatted_body', 'm.relates_to'}:
            raise ValueError('strict native Matrix message shape invalid')
        if message.get('msgtype') != 'm.text' or not isinstance(message.get('body'), str):
            raise ValueError('strict native Matrix body shape invalid')


def validate_receipt(binding, receipt):
    ack = _receipt_ack(binding, receipt, 'matrix')
    response, spec = ack.get('response'), binding['root']
    if (not isinstance(response, dict) or not isinstance(spec, dict)
            or not _event_id(receipt['root']) or response.get('event_id') != receipt['root']
            or spec.get('room_id') != binding['parent']):
        raise ValueError('strict native Matrix full root ACK invalid')
    _validate_event(ack.get('event'), binding['parent'], receipt['root'], spec['sender'], spec['content'])


async def _put_and_read(api, parent, txn, content, sender):
    from mautrix.api import Method, Path
    response = await api.request(Method.PUT, Path.v3.rooms[parent].send['m.room.message'][txn],
                                 content, retry_count=0, sensitive=True)
    if not isinstance(response, dict) or not _event_id(response.get('event_id')):
        raise ValueError('strict native Matrix PUT ACK invalid')
    root = response['event_id']
    event = await api.request(Method.GET, Path.v3.rooms[parent].event[root], retry_count=0)
    _validate_event(event, parent, root, sender, content)
    return response, event


async def create(plan):
    runtime = _runtime(plan, 'matrix')
    binding = json.loads(runtime.binding)
    spec = json.loads(runtime.root)
    if spec is None:
        raise ValueError('strict native create requires new-thread plan')
    _take(runtime, 'create')
    async with _api(runtime) as api:
        response, event = await _put_and_read(api, spec['room_id'], spec['txn_id'], spec['content'], spec['sender'])
    receipt = _receipt(binding, response['event_id'], response, event=event)
    validate_receipt(binding, receipt)
    return receipt


async def send(plan, root=None):
    runtime = _runtime(plan, 'matrix')
    transport = json.loads(runtime.transport)
    binding = json.loads(runtime.binding)
    if json.loads(runtime.root) is not None:
        validate_receipt(binding, root)
        thread = root['root']
    else:
        if root is not None:
            raise ValueError('strict native old target cannot override root')
        thread = None
    _take(runtime, 'send')
    delivered = []
    async with _api(runtime) as api:
        for index, encoded in enumerate(runtime.messages):
            content = json.loads(encoded)
            if thread is not None:
                content['m.relates_to'] = {'rel_type': 'm.thread', 'event_id': thread,
                    'is_falling_back': True, 'm.in_reply_to': {'event_id': thread}}
            txn = 'hermes-body-' + _digest({'binding': binding, 'index': index, 'content': content})
            response, _ = await _put_and_read(api, binding['parent'], txn, content, transport['sender'])
            delivered.append(response['event_id'])
    return {'ok': True, 'message_id': delivered[0]}
