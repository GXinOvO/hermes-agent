"""Preclaimed native AES/CDN requests with two restricted response dependencies.

Only the selected upload URL and the CDN encrypted reference are response slots.
Random per-file keys survive a fresh owner through an authenticated envelope;
no credential, context token or plaintext media key is persisted in the binding.
The caller still owns durable claim, unknown-outcome and no-replay accounting.
"""
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qsl, quote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from plugins.platforms import frozen_delivery as frozen


@dataclass(frozen=True, repr=False)
class Media:
    allocation: frozen.Request
    cdn_base: str
    filekey: str
    ciphertext: bytes
    messages: tuple


def _base_url(value):
    value = frozen.validate_url(str(value).rstrip('/'))
    parsed = urlsplit(value)
    if (parsed.query or any(part in {'.', '..'} for part in parsed.path.split('/')) or '%' in parsed.path
            or '\\' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError('Weixin frozen base URL must have a canonical path and no query')
    if parsed.scheme != 'https':
        try:
            local = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError('Weixin frozen transport requires HTTPS except explicit loopback')
    # Accessing port validates its numeric range before the first claim.
    _ = parsed.port
    return value


def _identity(state, calls, media, base, cdn, token, account, context):
    from hermes_constants import get_hermes_home
    job = getattr(state, 'job', None) or {}
    return {'home': str(get_hermes_home().resolve()), 'scope': str(getattr(state, 'profile', '') or ''),
        'execution': getattr(state, 'execution_id', None) or job.get('execution_id'),
        'destination': str(state.chat_id), 'target': getattr(state, 'target', None),
        'thread': getattr(state, 'thread_id', None), 'account': account, 'api_base': base, 'cdn_base': cdn,
        'credential_sha256': frozen.digest(token.encode('utf-8')),
        'context_sha256': frozen.digest((context or '').encode('utf-8')),
        'calls_sha256': frozen.digest(frozen.encode(calls)), 'media': media}


def _envelope_key(token, identity):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=hashlib.sha256(frozen.encode(identity)).digest(),
                info=b'hermes-weixin-frozen-media-v1').derive(token.encode('utf-8'))


def _crypto_context(token, identity, count, saved):
    aad = frozen.encode(identity)
    key = _envelope_key(token, identity)
    if saved is None:
        keys = [[secrets.token_hex(16), secrets.token_hex(16)] for _ in range(count)]
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(key).encrypt(nonce, frozen.encode(keys), aad)
        envelope = {'version': 1, 'algorithm': 'HKDF-SHA256/AES-256-GCM',
            'nonce': base64.b64encode(nonce).decode('ascii'),
            'ciphertext': base64.b64encode(encrypted).decode('ascii')}
        return keys, envelope
    try:
        binding = dict(saved)
        expected = binding.pop('transport_sha256')
        if expected != frozen.digest(frozen.encode(binding)) or binding['identity'] != identity:
            raise ValueError('saved binding identity mismatch')
        envelope = binding['crypto_context']
        if (set(envelope) != {'version', 'algorithm', 'nonce', 'ciphertext'} or type(envelope['version']) is not int
                or envelope['version'] != 1 or envelope['algorithm'] != 'HKDF-SHA256/AES-256-GCM'):
            raise ValueError('saved crypto context schema mismatch')
        nonce = base64.b64decode(envelope['nonce'], validate=True)
        encrypted = base64.b64decode(envelope['ciphertext'], validate=True)
        if len(nonce) != 12 or len(encrypted) > 128 * count + 1024:
            raise ValueError('saved crypto context size mismatch')
        keys = json.loads(AESGCM(key).decrypt(nonce, encrypted, aad))
        if not isinstance(keys, list) or len(keys) != count:
            raise ValueError('saved crypto context count mismatch')
        for pair in keys:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not str or not re.fullmatch('[0-9a-f]{32}', v) for v in pair):
                raise ValueError('saved crypto context key mismatch')
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError('Weixin saved binding authentication or identity mismatch') from exc
    return keys, envelope


def api_request(base, token, account, endpoint, payload):
    from plugins.platforms.weixin import adapter as native
    body = json.dumps({**payload, 'base_info': {'channel_version': native.CHANNEL_VERSION}},
                      ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    headers = native._headers(token, body.decode('utf-8'))
    # Native UIN is a random uint32, unrelated to media crypto. A deterministic
    # uint32 preserves its format and permits exact saved-plan reconstruction.
    headers['X-WECHAT-UIN'] = base64.b64encode(str(int(frozen.digest(body)[:8], 16)).encode('ascii')).decode('ascii')
    return frozen.request(base + '/' + endpoint, body, headers, 'weixin-message',
                          account=account, timeout=native.API_TIMEOUT_MS // 1000)


def _message(base, token, account, context, chat, items, identity, index):
    from plugins.platforms.weixin import adapter as native
    import uuid
    client_id = 'hermes-weixin-' + uuid.uuid5(uuid.NAMESPACE_URL,
        frozen.digest(frozen.encode([identity, index, items]))).hex
    message = {'from_user_id': '', 'to_user_id': chat, 'client_id': client_id,
        'message_type': native.MSG_TYPE_BOT, 'message_state': native.MSG_STATE_FINISH, 'item_list': items}
    if context:
        message['context_token'] = context
    return api_request(base, token, account, native.EP_SEND_MESSAGE, {'msg': message})


def _text(adapter, value):
    text = adapter.format_message(value)
    files, _ = adapter.extract_media(text)
    images, _ = adapter.extract_images(text)
    if files or images:
        raise ValueError('Weixin frozen text requires pre-extracted media')
    return text


def _media_step(adapter, method, kwargs, plaintext, pair, base, cdn, token, account, context, chat, identity, index):
    from plugins.platforms.weixin import adapter as native
    path = str(kwargs[frozen.PATH_KEYS[method]])
    media_type, builder = adapter._outbound_media_builder(path, force_file_attachment=method == 'send_voice')
    if media_type == native.MEDIA_VOICE:
        raise ValueError('Weixin frozen native voice bubble transport is unqualified; use send_voice file semantics')
    filekey, aes_hex = pair
    ciphertext = native._aes128_ecb_encrypt(plaintext, bytes.fromhex(aes_hex))
    md5 = hashlib.md5(plaintext).hexdigest()
    allocation = api_request(base, token, account, native.EP_GET_UPLOAD_URL,
        {'filekey': filekey, 'media_type': media_type, 'to_user_id': chat, 'rawsize': len(plaintext),
         'rawfilemd5': md5, 'filesize': len(ciphertext), 'no_need_thumb': True, 'aeskey': aes_hex})
    item = builder(encrypt_query_param=frozen.UPLOAD_REF,
        aes_key_for_api=base64.b64encode(aes_hex.encode('ascii')).decode('ascii'),
        ciphertext_size=len(ciphertext), plaintext_size=len(plaintext), filename=Path(path).name, rawfilemd5=md5)
    caption = kwargs.get('caption') or ''
    if method == 'send_voice' and not caption:
        caption = adapter.warning_text(native.t('platform.weixin.voice_as_attachment'))
    messages = []
    if caption:
        text = _text(adapter, caption)
        messages.append(_message(base, token, account, context, chat,
            [{'type': native.ITEM_TEXT, 'text_item': {'text': text}}], identity, [index, 'caption']))
    messages.append(_message(base, token, account, context, chat, [item], identity, [index, 'media']))
    return Media(allocation, cdn, filekey, ciphertext, tuple(messages))


def _prepared(steps, secret_values):
    prepared = []
    for step in steps:
        if isinstance(step, frozen.Request):
            prepared.append(step.public(secret_values))
        else:
            allocation = step.allocation.public(secret_values)
            allocation['ack'] = 'weixin-upload-allocation'
            allocation['response_slot'] = {'name': 'upload_url', 'origin': _origin(step.cdn_base),
                'path': urlsplit(step.cdn_base).path + '/upload', 'filekey_sha256': frozen.digest(step.filekey.encode('ascii')),
                'loopback_direct_path': '/cdn-upload' if not urlsplit(step.cdn_base).path else None}
            prepared.append(allocation)
            prepared.append({'method': 'POST', 'url_response_slot': 'upload_url',
                'body_sha256': frozen.digest(step.ciphertext), 'bytes': len(step.ciphertext),
                'headers': {'Content-Type': 'application/octet-stream'}, 'timeout': 120,
                'response_slot': 'encrypted_query_param', 'ack': 'weixin-cdn-reference',
                'redirects': False, 'retries': 0, 'auth_replay': False, 'proxy': None})
            prepared.extend(request.public(secret_values) for request in step.messages)
    return prepared


async def freeze(adapter, state, calls, loaded, media, *, saved_binding=None):
    base, cdn = _base_url(adapter._base_url), _base_url(adapter._cdn_base_url)
    token, account = str(adapter._token or ''), str(adapter._account_id or '')
    if not token or not account:
        raise ValueError('Weixin authentication/account unavailable')
    context = adapter._token_store.get(account, str(state.chat_id))
    identity = json.loads(frozen.encode(_identity(state, calls, media, base, cdn, token, account, context)))
    count = sum(method != 'send' for method, _ in calls)
    keys, envelope = _crypto_context(token, identity, count, saved_binding)
    secret_values = [context]
    for filekey, aes_hex in keys:
        secret_values.extend([filekey, aes_hex, base64.b64encode(aes_hex.encode('ascii')).decode('ascii')])
    steps, media_index = [], 0
    for index, (method, kwargs) in enumerate(calls):
        if method == 'send':
            from plugins.platforms.weixin import adapter as native
            text = _text(adapter, kwargs['content'])
            for chunk_index, chunk in enumerate(adapter._split_text(text)):
                steps.append(_message(base, token, account, context, str(state.chat_id),
                    [{'type': native.ITEM_TEXT, 'text_item': {'text': chunk}}], identity, [index, chunk_index]))
        else:
            path = str(kwargs[frozen.PATH_KEYS[method]])
            steps.append(_media_step(adapter, method, kwargs, loaded[path], keys[media_index],
                base, cdn, token, account, context, str(state.chat_id), identity, index))
            media_index += 1
    binding = {'version': 2, 'platform': 'weixin', 'actualdestination': str(state.chat_id),
        'parent': [], 'thread': getattr(state, 'thread_id', None), 'account': account,
        'scope': identity['scope'], 'media': media, 'identity': identity, 'crypto_context': envelope,
        'prepared': _prepared(steps, secret_values)}
    binding['transport_sha256'] = frozen.digest(frozen.encode(binding))
    if saved_binding is not None and binding != saved_binding:
        raise ValueError('Weixin saved binding prepared transport drift')
    return frozen.RuntimePlan(steps, binding)


def _origin(url):
    parsed = urlsplit(url)
    return [parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)]


def _reference(value):
    if type(value) is not str or not value.strip() or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RuntimeError('Weixin frozen ACK missing or invalid upload reference')
    if frozen.UPLOAD_REF in value:
        raise RuntimeError('Weixin frozen ACK contains reserved reference marker')
    return value


def _upload_url(step, payload):
    frozen.acknowledge('weixin-message', payload)
    full, param = payload.get('upload_full_url'), payload.get('upload_param')
    if full is None or full == '':
        param = _reference(param)
        return step.cdn_base + '/upload?encrypted_query_param=' + quote(param, safe='') + '&filekey=' + step.filekey
    full = _reference(full)
    if param is not None:
        _reference(param)
    try:
        frozen.validate_url(full)
        parsed = urlsplit(full)
        if '\\' in full or any(ord(c) <= 32 or ord(c) == 127 for c in full):
            raise ValueError('upload URL has ambiguous characters')
        expected = urlsplit(step.cdn_base).path + '/upload'
        if _origin(full) != _origin(step.cdn_base):
            raise ValueError('upload origin drift')
        # Retain the original loopback peer's explicitly named direct endpoint.
        direct = parsed.path == '/cdn-upload' and not urlsplit(step.cdn_base).path and ipaddress.ip_address(parsed.hostname or '').is_loopback
        if direct and not parsed.query:
            return full
        if parsed.path != expected:
            raise ValueError('upload path drift')
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(pairs) != 2 or set(query) != {'encrypted_query_param', 'filekey'} or query['filekey'] != step.filekey:
            raise ValueError('upload query drift')
        _reference(query['encrypted_query_param'])
    except ValueError as exc:
        raise RuntimeError('Weixin frozen allocation ACK violates selected CDN policy') from exc
    return full


def _check_cancelled():
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def _post_json(session, request, body=None):
    import aiohttp
    _check_cancelled()
    body = request.body if body is None else body
    headers = dict(request.headers)
    headers['Content-Length'] = str(len(body))
    async with session.request(request.method, request.url, data=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=request.timeout), allow_redirects=False) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError('Weixin frozen HTTP ACK unconfirmed: ' + str(response.status))
        payload = await response.json()
    _check_cancelled()
    frozen.acknowledge('weixin-message', payload)
    return payload


async def _upload(session, step, url):
    import aiohttp
    _check_cancelled()
    async with session.post(url, data=step.ciphertext, headers={'Content-Type': 'application/octet-stream'},
            timeout=aiohttp.ClientTimeout(total=120), allow_redirects=False) as response:
        if response.status != 200:
            raise RuntimeError('Weixin frozen CDN ACK unconfirmed: ' + str(response.status))
        refs = response.headers.getall('x-encrypted-param', [])
        if len(refs) != 1:
            raise RuntimeError('Weixin frozen CDN ACK missing unique encrypted reference')
        reference = _reference(refs[0])
        await response.read()
    _check_cancelled()
    return reference


def _resolve_reference(body, uploaded):
    # Preserve native JSON field order and encoding. Replace one JSON *value*,
    # never interpolate unescaped CDN text or rebuild the whole body sorted.
    marker = json.dumps(frozen.UPLOAD_REF).encode('ascii')
    if marker not in body:
        return body
    if body.count(marker) != 1:
        raise RuntimeError('Weixin frozen media template reference mismatch')
    return body.replace(marker, json.dumps(uploaded, ensure_ascii=False).encode('utf-8'))


async def send(plan):
    import aiohttp
    steps = plan.consume()
    last_id = None
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        for step in steps:
            if isinstance(step, frozen.Request):
                await _post_json(session, step)
                last_id = json.loads(step.body)['msg']['client_id']
            else:
                payload = await _post_json(session, step.allocation)
                url = _upload_url(step, payload)
                uploaded = await _upload(session, step, url)
                for request in step.messages:
                    body = _resolve_reference(request.body, uploaded)
                    await _post_json(session, request, body)
                    last_id = json.loads(body)['msg']['client_id']
    return {'ok': True, 'message_id': last_id}
