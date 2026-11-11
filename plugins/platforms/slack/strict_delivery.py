"""Strict Slack egress: private SDK client, zero retry handlers, no redirects.

files_upload_v2 is a multi-phase protocol. Each phase submits once; any missing
ACK aborts the remainder (including completion/batches) without text fallback.
"""
import asyncio
import copy
import hashlib
import inspect
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from gateway.platforms.base import SendResult, read_httpx_body_with_limit, ssrf_redirect_guard


logger = logging.getLogger(__name__)


def failed(exc, delivered=()):
    return SendResult(success=False, retryable=False, error='strict_delivery_unknown: ' + str(exc),
                      raw_response={'message_ids': list(delivered)})


def preflight(adapter, chat_id, metadata):
    blocked = adapter._outbound_blocked(chat_id, 'strict delivery to')
    if blocked:
        raise ValueError(blocked.error)
    if str(chat_id).startswith(('U', 'W')):
        key = f'{adapter._metadata_team_id(metadata)}:{chat_id}'
        chat_id = adapter._dm_conversation_cache.get(key)
        if not chat_id:
            raise ValueError('strict_transport_unsupported: unresolved Slack DM')
    # A response_url or live append-only stream is a different transport contract.
    if any(chat_id in key for key in adapter._slash_command_contexts):
        raise ValueError('strict_transport_unsupported: Slack slash response')
    if any(chat_id in key for key in adapter._active_streams):
        raise ValueError('strict_transport_unsupported: Slack active stream')
    return chat_id


def prepare(adapter, method, *, chat_id, metadata=None, **kwargs):
    from slack_sdk.web.async_client import AsyncWebClient
    chat_id = preflight(adapter, chat_id, metadata)
    if not isinstance(adapter._client_for(chat_id, metadata), AsyncWebClient):
        raise RuntimeError('strict_transport_unverified: Slack SDK client unavailable')
    if method == 'send_multiple_images' and not kwargs.get('images'):
        raise ValueError('no images to send')


@dataclass(eq=False, repr=False)
class _Plan:
    pass


@dataclass(eq=False, repr=False)
class _State:
    binding: str
    signature: str
    method: str
    chat_id: str
    metadata: object
    thread_ts: object
    source: object
    payload: object
    identity: object
    used: bool = False


def _binding(adapter, method, kwargs):
    args = inspect.signature(getattr(adapter, method)).bind(**kwargs)
    args.apply_defaults()
    values = dict(args.arguments)
    for name in ('prepared_plan', 'execution_id', 'claim_id'):
        values.pop(name, None)
    def path_value(value):
        if isinstance(value, Path):
            return str(value)
        raise TypeError('unsupported Slack argument')
    return json.dumps([method, values], sort_keys=True, separators=(',', ':'), default=path_value)


def _source_copy(source):
    from slack_sdk.web.async_client import AsyncWebClient
    if not isinstance(source, AsyncWebClient) or source.default_params:
        raise ValueError('unsupported Slack client')
    source = copy.copy(source)
    source.headers = copy.deepcopy(source.headers)
    source.default_params = copy.deepcopy(source.default_params)
    return source


def _source_signature(source):
    values = [source.token, source.base_url, source.timeout, source.proxy, id(source.ssl),
              source.headers, source.default_params]
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _state_signature(state):
    payload = state.payload if state.method == 'send' else [
        [hashlib.sha256(data).hexdigest(), name, caption] for data, name, caption in state.payload]
    return hashlib.sha256(json.dumps([state.binding, state.chat_id, state.thread_ts, state.metadata, payload,
                                     _source_signature(state.source)], sort_keys=True).encode()).hexdigest()


def _issue(adapter, state):
    from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size
    identity = state.identity
    if identity is not None:
        if identity in adapter._strict_consumed_claims:
            raise RuntimeError('strict_transport_unavailable: Slack claim already consumed')
        current = adapter._strict_live_claims.get(identity)
        if current is not None:
            if current.signature != state.signature or current.used:
                raise RuntimeError('strict_transport_unavailable: Slack claim binding changed')
            state = current
        else:
            if len(adapter._strict_consumed_claims) + len(adapter._strict_live_claims) >= 4096:
                raise RuntimeError('strict_transport_unavailable: Slack claim capacity reached')
    else:
        current = None
    if current is None:
        unique = {id(item): item for item in adapter._strict_issued_plans.values() if item.payload is not None}
        unique[id(state)] = state
        total = sum(len(data) for item in unique.values() if item.method != 'send'
                    for data, _, _ in item.payload)
        validate_inbound_media_size(total, media_type='media', max_bytes=get_inbound_media_max_bytes())
        if identity is not None:
            adapter._strict_live_claims[identity] = state
    plan = _Plan()
    adapter._strict_issued_plans[plan] = state
    return plan


def _consume(adapter, method, plan, execution_id, claim_id, kwargs):
    state = adapter._strict_issued_plans.get(plan) if type(plan) is _Plan else None
    identity = None if execution_id is None and claim_id is None else (execution_id, claim_id)
    if (state is None or state.used or state.method != method or state.signature != _state_signature(state)
            or state.identity != identity or state.binding != _binding(adapter, method, kwargs)):
        raise ValueError('Slack strict plan missing, changed, or consumed')
    if identity is not None:
        if adapter._strict_live_claims.get(identity) is not state or identity in adapter._strict_consumed_claims:
            raise ValueError('Slack strict claim already consumed')
        adapter._strict_consumed_claims.add(identity)
        del adapter._strict_live_claims[identity]
    state.used = True
    return state


def _text_calls(adapter, chat_id, content, reply_to, metadata):
    from plugins.platforms.slack.adapter import _slack_unfurl_kwargs
    chunks = adapter.truncate_message(adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH)
    blocks = adapter._maybe_blocks(content) if len(chunks) == 1 else None
    thread_ts = adapter._resolve_thread_ts(reply_to, metadata)
    calls = []
    for index, chunk in enumerate(chunks):
        kwargs = dict(channel=chat_id, text=chunk, mrkdwn=True, **_slack_unfurl_kwargs(adapter.config.extra))
        if blocks and index == 0:
            kwargs['blocks'] = blocks
        if thread_ts:
            kwargs['thread_ts'] = thread_ts
            if adapter.config.extra.get('reply_broadcast', False) and index == 0:
                kwargs['reply_broadcast'] = True
        calls.append(kwargs)
    return calls


def _local_bytes(path):
    from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size
    limit = max(0, get_inbound_media_max_bytes())
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1 if limit > 0 else -1)
    validate_inbound_media_size(len(data), media_type='media', max_bytes=limit)
    return data


async def prepare_plan(adapter, method, *, execution_id=None, claim_id=None, **kwargs):
    try:
        if (execution_id is None) != (claim_id is None):
            raise ValueError('incomplete claim identity')
        if execution_id is not None and (not isinstance(execution_id, str) or not execution_id
                or not isinstance(claim_id, str) or not claim_id):
            raise ValueError('invalid claim identity')
        binding = _binding(adapter, method, kwargs)
        metadata = copy.deepcopy(kwargs.get('metadata'))
        chat_id = preflight(adapter, kwargs['chat_id'], metadata)
        source = _source_copy(adapter._client_for(chat_id, metadata))
        thread_ts = adapter._resolve_thread_ts(kwargs.get('reply_to'), metadata)
        if method == 'send':
            payload = _text_calls(adapter, chat_id, kwargs['content'], kwargs.get('reply_to'), metadata)
            payload = json.loads(json.dumps(payload))
        else:
            from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size
            limit = get_inbound_media_max_bytes()
            paths = {'send_image_file': 'image_path', 'send_voice': 'audio_path',
                     'send_video': 'video_path', 'send_document': 'file_path'}
            if method in paths:
                path = kwargs[paths[method]]
                data = await asyncio.to_thread(_local_bytes, path)
                payload = [(data, kwargs.get('file_name') or Path(path).name, kwargs.get('caption') or '')]
            elif method in ('send_image', 'send_multiple_images'):
                images = ([(kwargs['image_url'], kwargs.get('caption'))] if method == 'send_image'
                          else kwargs['images'])
                if not images:
                    raise ValueError('no images to send')
                payload = []
                total = 0
                for url, caption in images:
                    data, name = await source_bytes(url)
                    total += len(data)
                    validate_inbound_media_size(total, media_type='media', max_bytes=limit)
                    payload.append((data, name, caption or ''))
            else:
                raise ValueError('unsupported Slack strict operation')
            validate_inbound_media_size(sum(len(data) for data, _, _ in payload), media_type='media',
                                        max_bytes=limit)
            for _, name, caption in payload:
                if not isinstance(name, str) or not isinstance(caption, str):
                    raise TypeError('invalid Slack upload metadata')
                name.encode('utf-8')
                caption.encode('utf-8')
            '\n'.join(caption for _, _, caption in payload if caption)
        identity = None if execution_id is None else (execution_id, claim_id)
        state = _State(binding, '', method, chat_id, metadata, thread_ts, source, payload, identity)
        state.signature = _state_signature(state)
        with adapter._strict_plan_lock:
            return _issue(adapter, state)
    except Exception:  # health: allow BLE001 -- strict preflight fails closed on any SDK/parse/IO error; the cause is dropped so request details never reach logs
        raise RuntimeError('strict_transport_payload_unavailable: Slack preflight failed') from None


async def send_prepared(adapter, method, plan, execution_id, claim_id, **kwargs):
    delivered = []
    state = None
    try:
        with adapter._strict_plan_lock:
            state = _consume(adapter, method, plan, execution_id, claim_id, kwargs)
        if method != 'send':
            return await upload(adapter, state.chat_id, state.payload, None, state.metadata,
                                source=state.source, thread_ts=state.thread_ts)
        if not state.payload:
            return SendResult(success=True)
        async with single_client(adapter, state.chat_id, state.metadata, source=state.source) as client:
            for call in state.payload:
                response = await client.chat_postMessage(**call)
                if not response.get('ts'):
                    raise RuntimeError('Slack message ACK missing ts')
                delivered.append(response['ts'])
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except Exception:  # health: allow BLE001 -- one-attempt boundary: any post-claim failure becomes delivery-unknown in the result, a traceback would escape the contract
        return failed(RuntimeError('Slack ACK or prepared plan unavailable'), delivered)
    finally:
        if state is not None and state.used:
            with adapter._strict_plan_lock:
                state.payload = None
                state.source = None
                state.metadata = None


@asynccontextmanager
async def single_client(adapter, chat_id, metadata, *, source=None):
    import aiohttp
    from slack_sdk.web.async_client import AsyncWebClient

    source = source if source is not None else adapter._client_for(chat_id, metadata)
    if not isinstance(source, AsyncWebClient):
        raise RuntimeError('strict_transport_unverified: Slack SDK client unavailable')

    class NoRedirectSession:
        @property
        def closed(self):
            return session.closed

        def request(self, method, url, **kwargs):
            # Covers both Web API and the SDK's separate upload helper.
            return session.request(method, url, **{**kwargs, 'allow_redirects': False})

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=source.timeout),
                                     trust_env=False) as session:
        client = AsyncWebClient(token=source.token, base_url=source.base_url, timeout=source.timeout,
            ssl=source.ssl, proxy=source.proxy, headers=dict(source.headers),
            session=NoRedirectSession(), retry_handlers=[], trust_env_in_session=False)
        client.default_params = dict(source.default_params)
        client.proxy = source.proxy  # Do not re-resolve proxy environment after selecting the workspace.
        yield client


async def send_text(adapter, chat_id, content, reply_to, metadata):
    from plugins.platforms.slack.adapter import _slack_unfurl_kwargs

    delivered = []
    try:
        chat_id = preflight(adapter, chat_id, metadata)
        chunks = adapter.truncate_message(adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH)
        if not chunks:
            return SendResult(success=True)
        blocks = adapter._maybe_blocks(content) if len(chunks) == 1 else None
        thread_ts = adapter._resolve_thread_ts(reply_to, metadata)
        calls = []
        for index, chunk in enumerate(chunks):
            kwargs = dict(channel=chat_id, text=chunk, mrkdwn=True, **_slack_unfurl_kwargs(adapter.config.extra))
            if blocks and index == 0:
                kwargs['blocks'] = blocks
            if thread_ts:
                kwargs['thread_ts'] = thread_ts
                if adapter.config.extra.get('reply_broadcast', False) and index == 0:
                    kwargs['reply_broadcast'] = True
            calls.append(kwargs)
        async with single_client(adapter, chat_id, metadata) as client:
            for kwargs in calls:
                response = await client.chat_postMessage(**kwargs)
                if not response.get('ts'):
                    raise RuntimeError('Slack message ACK missing ts')
                delivered.append(response['ts'])
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc, delivered)


async def source_bytes(source):
    from tools.url_safety import create_ssrf_safe_async_client, is_safe_url

    if source.startswith('file://'):
        path = Path(unquote(source[7:]))
        return await asyncio.to_thread(_local_bytes, path), path.name
    if not is_safe_url(source):
        raise ValueError('Unsafe media URL')
    async with create_ssrf_safe_async_client(timeout=30, follow_redirects=True,
            event_hooks={'response': [ssrf_redirect_guard]}) as client:
        async with client.stream('GET', source) as response:
            response.raise_for_status()
            return await read_httpx_body_with_limit(response, media_type='image'), 'image.png'


async def upload(adapter, chat_id, entries, reply_to, metadata, *, source=None, thread_ts=None):
    delivered = []
    try:
        if source is None:
            chat_id = preflight(adapter, chat_id, metadata)
            thread_ts = adapter._resolve_thread_ts(reply_to, metadata)
        if not entries:
            raise ValueError('no files to send')
        # All local bytes/downloads and comments are prepared before any SDK POST.
        async with single_client(adapter, chat_id, metadata, source=source) as client:
            for start in range(0, len(entries), 10):
                batch = entries[start:start + 10]
                response = await client.files_upload_v2(channel=chat_id, thread_ts=thread_ts,
                    file_uploads=[{'content': data, 'filename': name} for data, name, caption in batch],
                    initial_comment='\n'.join(caption for data, name, caption in batch if caption))
                files = response.get('files') or []
                if len(files) != len(batch) or any(not file.get('id') for file in files):
                    raise RuntimeError('Slack file ACK incomplete')
                delivered.extend(file['id'] for file in files)
        adapter._record_uploaded_file_thread(chat_id, thread_ts, metadata)
        return SendResult(success=True, message_id=delivered[0], raw_response={'message_ids': delivered})
    except Exception as exc:
        if source is not None:
            return failed(RuntimeError('Slack upload ACK unavailable'), delivered)
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc, delivered)


async def send_file(adapter, chat_id, path, caption, reply_to, metadata, file_name=None):
    try:
        preflight(adapter, chat_id, metadata)
        data = await asyncio.to_thread(Path(path).read_bytes)
        return await upload(adapter, chat_id, [(data, file_name or Path(path).name, caption or '')], reply_to, metadata)
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc)


async def send_images(adapter, chat_id, images, metadata):
    try:
        preflight(adapter, chat_id, metadata)
        entries = []
        for url, caption in images:
            data, name = await source_bytes(url)
            entries.append((data, name, caption or ''))
        return await upload(adapter, chat_id, entries, None, metadata)
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc)


async def send_url(adapter, chat_id, url, caption, reply_to, metadata):
    try:
        preflight(adapter, chat_id, metadata)
        data, name = await source_bytes(url)
        return await upload(adapter, chat_id, [(data, name, caption or '')], reply_to, metadata)
    except Exception as exc:
        logger.exception("Strict delivery stopped; ACK unknown")
        return failed(exc)


def freeze_thread(adapter, state, calls, retained):
    """Freeze every mutation input; the body destination depends only on this root's ACK."""
    import copy
    import hashlib
    import re
    from plugins.platforms.slack.adapter import SlackAdapter, _slack_unfurl_kwargs
    from gateway.strict_transport import require_strict_transport
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    if type(adapter) is not SlackAdapter or 'create_handoff_thread' in adapter.__dict__:
        raise RuntimeError('strict Slack thread creator unsupported')
    if not isinstance(getattr(adapter, '_session_store', None), SessionStore) or not isinstance(adapter._session_store._db, SessionDB):
        raise RuntimeError('strict Slack continuation store unavailable')
    team = adapter._metadata_team_id(calls[0][1].get('metadata')) or adapter.scope_id_for_chat(str(state.chat_id))
    if not team or team not in adapter._team_clients:
        raise RuntimeError('strict Slack workspace unavailable or ambiguous')
    metadata = {'scope_id': team}
    parent = preflight(adapter, str(state.chat_id), metadata)
    if not re.fullmatch(r'[CDG][A-Z0-9]+', parent):
        raise RuntimeError('strict Slack concrete parent unavailable')
    source = adapter._team_clients[team]
    prepare(adapter, 'send', chat_id=parent, metadata=metadata)
    source = copy.copy(source)
    source.headers = copy.deepcopy(source.headers)
    source.default_params = copy.deepcopy(source.default_params)
    if source.default_params:
        raise RuntimeError('strict Slack creator default parameters unsupported')
    from cron.scheduler_delivery import _cron_display_name
    root = {'channel': parent, 'text': 'Hermes — ' + _cron_display_name(state.job)[:80],
            'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False}
    messages, entries, identities = [], [], []
    path_keys = {'send_document': 'file_path', 'send_image_file': 'image_path',
                 'send_video': 'video_path', 'send_voice': 'audio_path'}
    for method, kwargs in calls:
        require_strict_transport(adapter, method)
        if method == 'send':
            text = kwargs['content']
            chunks = adapter.truncate_message(adapter.format_message(text), adapter.MAX_MESSAGE_LENGTH)
            blocks = adapter._maybe_blocks(text) if len(chunks) == 1 else None
            for index, chunk in enumerate(chunks):
                message = {'channel': parent, 'text': chunk, 'mrkdwn': True, **_slack_unfurl_kwargs(adapter.config.extra)}
                if blocks and index == 0:
                    message['blocks'] = blocks
                if adapter.config.extra.get('reply_broadcast', False) and index == 0:
                    message['reply_broadcast'] = True
                messages.append(message)
        elif method in path_keys:
            path = kwargs[path_keys[method]]
            data = Path(path).read_bytes()
            name = kwargs.get('file_name') or Path(path).name
            voice = kwargs.get('is_voice', False)
            entries.append((data, name, kwargs.get('caption') or ''))
            identities.append([path, voice, hashlib.sha256(data).hexdigest(), name])
        else:
            raise RuntimeError('strict Slack thread payload method unsupported: ' + method)
    if [entry[:2] for entry in identities] != [[str(path), voice] for path, voice in retained]:
        raise RuntimeError('strict Slack frozen media metadata mismatch')
    return {'source': source, 'root': root, 'messages': messages, 'entries': entries, 'media': identities,
            'workspace': team, 'parent': parent}


def thread_binding(frozen):
    """Credential fingerprints are persisted; credential bytes remain process-local."""
    import hashlib
    import json
    source = frozen['source']
    transport = {key: str(getattr(source, key)) for key in ('token', 'base_url', 'timeout', 'proxy', 'ssl')}
    transport['headers'] = dict(source.headers)
    return {'workspace': frozen['workspace'], 'parent': frozen['parent'], 'root': frozen['root'],
            'messages': frozen['messages'], 'media': frozen['media'],
            'transport_sha256': hashlib.sha256(json.dumps(transport, sort_keys=True).encode()).hexdigest()}


async def create_thread(frozen):
    """Exactly one SDK mutation; a rejected/lost ACK is never retried or downgraded."""
    import re
    async with single_client(None, None, None, source=frozen['source']) as client:
        response = await client.chat_postMessage(**frozen['root'])
    data = {key: response.get(key) for key in ('ok', 'channel', 'ts', 'message')}
    if (data['ok'] is not True or data['channel'] != frozen['parent']
            or not isinstance(data['ts'], str) or not re.fullmatch(r'[0-9]+\.[0-9]{6}', data['ts'])
            or not isinstance(data['message'], dict) or data['message'].get('text') != frozen['root']['text']):
        raise RuntimeError('strict Slack root ACK identity mismatch')
    return data


async def send_thread_body(frozen, root):
    """Consume frozen bytes and the durable dependency without resolving a live adapter route."""
    async with single_client(None, None, None, source=frozen['source']) as client:
        for message in frozen['messages']:
            response = await client.chat_postMessage(**message, thread_ts=root)
            if not response.get('ts') or response.get('channel') != frozen['parent']:
                raise RuntimeError('strict Slack body ACK missing')
        entries = frozen['entries']
        for start in range(0, len(entries), 10):
            batch = entries[start:start + 10]
            response = await client.files_upload_v2(channel=frozen['parent'], thread_ts=root,
                file_uploads=[{'content': data, 'filename': name} for data, name, caption in batch],
                initial_comment='\n'.join(caption for data, name, caption in batch if caption))
            files = response.get('files') or []
            if len(files) != len(batch) or any(not file.get('id') for file in files):
                raise RuntimeError('strict Slack thread attachment ACK incomplete')
