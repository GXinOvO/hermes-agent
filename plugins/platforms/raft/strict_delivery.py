"""Frozen text-only Raft sends using the official sendV2 HTTP contract.

The ordinary adapter remains CLI-owned. A prepared object is an in-process,
claim-bound capability; credential bytes never enter its public binding. There
is one POST, no redirect, reconciliation, CLI, SDK retry or transport fallback.
"""
from dataclasses import dataclass, field, fields
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid
import weakref

import aiohttp

from agent.secret_scope import current_secret_scope, current_secret_scope_home
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult

_TARGET = re.compile(r'#[^\s:#]+(?::[0-9a-f]{8})?\Z')
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
_NAMESPACE = uuid.UUID('b111dcb1-79f9-5b5e-ae53-65f5d99eed59')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError('Raft strict invalid ' + name)
    return value


def _profile_path():
    slug = _text(get_scoped_secret('RAFT_PROFILE'), 'profile')
    if not _PROFILE.fullmatch(slug) or slug in {'.', '..'}:
        raise ValueError('Raft strict invalid profile slug')
    alias = get_scoped_secret('SLOCK_PROFILE')
    if alias and alias != slug:
        raise ValueError('Raft profile aliases conflict')
    direct, legacy = get_scoped_secret('RAFT_PROFILE_DIR'), get_scoped_secret('SLOCK_PROFILE_DIR')
    if direct and legacy and direct != legacy:
        raise ValueError('Raft profile directories conflict')
    # This slice supports explicit profile credentials, never a managed proxy runtime.
    if get_scoped_secret('SLOCK_CLI_TRANSPORT_DIR') or get_scoped_secret('SLOCK_AGENT_LAUNCH_DIR'):
        raise ValueError('Raft managed runtime unsupported for strict publication')
    if direct or legacy:
        directory = Path(_text(direct or legacy, 'profile directory'))
    else:
        root = get_scoped_secret('RAFT_HOME') or get_scoped_secret('SLOCK_HOME')
        if root:
            directory = Path(_text(root, 'home')) / 'profiles' / slug
        else:
            if current_secret_scope() is not None:
                raise ValueError('Raft strict scoped profile requires its own directory or home')
            # Same real-home convention as RaftAdapter._spawn_bridge.
            home = Path(os.environ.get('HERMES_REAL_HOME') or str(Path.home()))
            state = home / '.slock'
            legacy_state = any((state / entry).exists() for entry in ('agents', 'computer', 'machines', 'profiles', 'agent-state'))
            directory = (state if legacy_state else home / '.raft') / 'profiles' / slug
    if not directory.is_absolute():
        raise ValueError('Raft strict profile directory must be absolute')
    return slug, directory / 'credential.json'


def _endpoint(value):
    value = _text(value, 'server URL')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '%' in parsed.netloc):
        raise ValueError('Raft strict invalid server URL')
    if parsed.scheme == 'http':
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname == 'localhost'
        if not local:
            raise ValueError('Raft strict cleartext server must be loopback')
    return value.rstrip('/') + '/internal/agent-api/v2/send'


def _credential():
    slug, path = _profile_path()
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        token = _text(data.get('apiKey'), 'credential')
        if not token.isascii() or any(c.isspace() for c in token):
            raise ValueError()
        agent = _text(data.get('agentId'), 'agent ID')
        server = data.get('serverId')
        if server is not None:
            _text(server, 'server ID')
        endpoint = _endpoint(data.get('serverUrl'))
    except (OSError, UnicodeError, ValueError, TypeError):
        # Neither credential content nor parser exception text is safe to log.
        raise ValueError('Raft strict profile credential invalid or unavailable') from None
    return slug, str(path.resolve()), _sha(raw), token, agent, server, endpoint


@dataclass(frozen=True)
class FrozenSend:
    profile_home: str
    adapter_profile: str | None
    profile: str
    credential_path: str
    credential_sha256: str
    credential_fingerprint: str
    agent_id: str
    server_id: str | None
    endpoint: str
    target: str
    content: str
    reply_to: str | None
    metadata_json: str
    execution_id: str
    claim_id: str
    idempotency_key: str
    body: bytes = field(repr=False)
    timeout_seconds: float
    token: str = field(repr=False)
    adapter: object = field(repr=False, compare=False)


@dataclass(eq=False, repr=False)
class PreparedSend:
    """One object per claim; no shared-config or adapter cache."""
    _frozen: FrozenSend
    _used: bool = False


@dataclass
class _Consumption:
    signature: str
    used: bool = False


_issued = weakref.WeakKeyDictionary()
_claims = weakref.WeakKeyDictionary()
_issuance_lock = threading.RLock()
_MAX_CLAIMS = 4096


def _scope_identity():
    return current_secret_scope(), current_secret_scope_home()


def _issue(frozen):
    plan = PreparedSend(frozen)
    key = (frozen.profile_home, frozen.adapter_profile, frozen.execution_id, frozen.claim_id)
    signature = _sha(_json([frozen.profile, frozen.credential_path, frozen.credential_sha256,
                           frozen.agent_id, frozen.server_id, frozen.endpoint,
                           frozen.target, frozen.content, frozen.reply_to, frozen.metadata_json,
                           frozen.idempotency_key, frozen.timeout_seconds]).encode())
    with _issuance_lock:
        claims = _claims.setdefault(frozen.adapter, {})
        state = claims.get(key)
        if state is None:
            if len(claims) >= _MAX_CLAIMS:
                raise ValueError('Raft strict prepared claim capacity reached')
            state = _Consumption(signature)
            claims[key] = state
        if state.signature != signature or state.used:
            raise ValueError('Raft strict claim binding changed or already consumed')
        _issued[plan] = (frozen, tuple(getattr(frozen, f.name) for f in fields(frozen)), _scope_identity(), state)
    return plan


def _authenticated(plan):
    if type(plan) is not PreparedSend:
        raise ValueError('Raft strict pre-claim plan missing')
    with _issuance_lock:
        issued = _issued.get(plan)
        if issued is None or issued[0] is not plan._frozen:
            raise ValueError('Raft strict foreign or replaced plan')
        frozen, values, scope, state = issued
        if values != tuple(getattr(frozen, f.name) for f in fields(frozen)):
            raise ValueError('Raft strict frozen plan changed')
        if plan._used or state.used:
            raise ValueError('Raft strict plan already consumed')
    from hermes_constants import get_hermes_home
    current_scope, current_home = _scope_identity()
    if (str(get_hermes_home().resolve()) != frozen.profile_home
            or getattr(frozen.adapter, '_owner_profile', None) != frozen.adapter_profile
            or current_scope is not scope[0] or current_home != scope[1]):
        raise ValueError('Raft strict plan owner context changed')
    if frozen.body != _json({'target': frozen.target, 'content': frozen.content,
                             'idempotencyKey': frozen.idempotency_key}).encode():
        raise ValueError('Raft strict wire payload changed')
    return FrozenSend(*values), state


def binding(plan):
    """Persistable audit only. Never serialize plan or its private token bytes."""
    frozen = plan._frozen
    names = ('profile_home', 'adapter_profile', 'profile', 'credential_path', 'credential_sha256',
             'credential_fingerprint', 'agent_id', 'server_id', 'endpoint', 'target', 'execution_id',
             'claim_id', 'idempotency_key', 'reply_to', 'metadata_json', 'timeout_seconds')
    return {**{name: getattr(frozen, name) for name in names}, 'body_sha256': _sha(frozen.body),
            'transport': 'raft-sendV2-single-post-v1'}


def prepare(adapter, method, *, chat_id, content, reply_to=None, metadata=None,
            execution_id=None, claim_id=None, prepared_plan=None, timeout_seconds=30):
    """Only explicit caller identity can mint a pre-claim transport capability."""
    if prepared_plan is not None:
        validate(adapter, prepared_plan, chat_id, content, reply_to, metadata, execution_id, claim_id)
        return prepared_plan
    if method != 'send' or not isinstance(chat_id, str) or not _TARGET.fullmatch(chat_id):
        raise ValueError('Raft strict requires an exact channel target; wake runtime is aggregate')
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError('Raft strict invalid metadata')
    if reply_to or metadata and (metadata.get('thread_id') or metadata.get('thread_ts')):
        raise ValueError('Raft strict reply thread unsupported; supply the exact qualified target')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('Raft strict requires completed text')
    media, _ = BasePlatformAdapter.extract_media(content)
    if media or 'MEDIA:' in content or '[[image' in content.lower():
        raise ValueError('Raft strict media unsupported')
    execution_id, claim_id = _text(execution_id, 'execution identity'), _text(claim_id, 'claim identity')
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise ValueError('Raft strict invalid timeout')
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home().resolve())
    profile, path, digest, token, agent, server, endpoint = _credential()
    # A fresh delivery owner gets the same key for the same sealed result/part.
    key = str(uuid.uuid5(_NAMESPACE, _json([home, execution_id, claim_id, profile, agent, server, chat_id])))
    body = _json({'target': chat_id, 'content': content, 'idempotencyKey': key}).encode()
    frozen = FrozenSend(home, getattr(adapter, '_owner_profile', None), profile, path, digest, _sha(token.encode()),
                        agent, server, endpoint, chat_id, content, reply_to, _json(metadata or {}),
                        execution_id, claim_id, key, body, timeout_seconds, token, adapter)
    return _issue(frozen)


def validate(adapter, plan, chat_id, content, reply_to, metadata, execution_id, claim_id):
    frozen, _state = _authenticated(plan)
    if frozen.adapter is not adapter:
        raise ValueError('Raft strict plan already consumed or foreign adapter')
    actual = (chat_id, content, reply_to, _json(metadata or {}), execution_id, claim_id)
    expected = (frozen.target, frozen.content, frozen.reply_to, frozen.metadata_json, frozen.execution_id, frozen.claim_id)
    if actual != expected:
        raise ValueError('Raft strict caller binding changed')
    return frozen


def _ack(data, frozen):
    if not isinstance(data, dict) or data.get('ok') is not True or data.get('state') != 'sent':
        raise ValueError('Raft strict sent ACK missing')
    _text(data.get('messageId'), 'ACK message ID')
    seq = data.get('messageSeq')
    if seq is not None and (type(seq) not in (int, float) or not math.isfinite(seq)):
        raise ValueError('Raft strict invalid ACK sequence')
    # These identity fields are not guaranteed by the official response schema.
    # If supplied, refuse a contradiction; absence is request binding only.
    for name, expected in (('target', frozen.target), ('agentId', frozen.agent_id), ('serverId', frozen.server_id),
                           ('idempotencyKey', frozen.idempotency_key)):
        if name in data and data[name] != expected:
            raise ValueError('Raft strict ACK identity mismatch')
    return data['messageId']


async def send(adapter, chat_id, content, reply_to, metadata, *, prepared_plan, execution_id, claim_id):
    try:
        frozen = validate(adapter, prepared_plan, chat_id, content, reply_to, metadata, execution_id, claim_id)
        with _issuance_lock:
            _record, state = _authenticated(prepared_plan)
            if state.used:
                raise ValueError('Raft strict plan already consumed')
            state.used = True
            prepared_plan._used = True
    except ValueError as exc:
        # A refused declaration is not an unknown ACK: reusing one plan, a foreign
        # plan or a changed binding must surface as a refusal a caller cannot retry.
        # An unprepared call is ordinary delivery, so it keeps the failure result.
        if type(prepared_plan) is not PreparedSend:
            return SendResult(success=False, retryable=False,
                              error='strict_delivery_unknown: Raft ACK or plan unavailable')
        raise RuntimeError('strict_transport_unavailable: ' + str(exc)) from exc
    try:
        # A new private POST session consumes frozen values only. aiohttp's
        # reconnect retries apply to idempotent methods; this operation is POST.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=frozen.timeout_seconds),
                                         trust_env=False, connector=aiohttp.TCPConnector(force_close=True)) as client:
            async with client.post(frozen.endpoint, data=frozen.body, allow_redirects=False,
                                   headers={'Authorization': 'Bearer ' + frozen.token,
                                            'Accept': 'application/json', 'Content-Type': 'application/json'}) as response:
                if not 200 <= response.status < 300:
                    raise ValueError('Raft strict HTTP ACK missing')
                data = json.loads(await response.read())
                message_id = _ack(data, frozen)
        return SendResult(success=True, message_id=message_id, raw_response={
            'binding': binding(prepared_plan), 'state': 'sent', 'messageId': message_id,
            'server_confirmed_target': 'target' in data})
    except (ValueError, TypeError, OSError, RuntimeError, OverflowError, aiohttp.ClientError, TimeoutError):
        return SendResult(success=False, retryable=False, error='strict_delivery_unknown: Raft ACK or plan unavailable')


def _pending_snapshot(value):
    part = value['parts']['text']
    return _sha(_json([value['binding'], value['target'], value['reply_to'], value['metadata'],
                      value['adapter_profile'], value['response'],
                      part['content'], part['payload_hash']]).encode())


def _pending_value(adapter, db, admission_id, *, expected=None, claimed=None):
    from gateway import pending_final as pending
    from gateway.session_delivery_policy import STRICT
    from hermes_constants import get_hermes_home

    with db._read_ctx() as conn:
        value = pending._read(conn, admission_id)
        pending._check_descriptor(pending.PREFIX + admission_id, value)
        home = str(Path(db.db_path).resolve().parent)
        if (home != str(get_hermes_home().resolve()) or value['binding']['profile_home'] != home
                or not pending._valid(conn, value, home)):
            raise ValueError('Raft pending final no longer valid')
        if expected is not None and _pending_snapshot(value) != expected:
            raise ValueError('Raft pending final snapshot changed')
        if claimed is not None:
            part = value['parts']['text']
            identity = ('state', 'attempts', 'owner_pid', 'owner_started_at', 'delivery_epoch')
            if any(part.get(key) != claimed.get(key) for key in identity):
                raise ValueError('Raft pending final claim changed')
            epoch = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()[0]
            if epoch != part['delivery_epoch']:
                raise ValueError('Raft pending final claim epoch changed')
    source = value['binding']['source']
    if (value['binding']['policy'] != STRICT or source.get('platform') != 'raft'
            or value['adapter_profile'] != getattr(adapter, '_owner_profile', None)
            or source.get('thread_id') or set(value['parts']) != {'text'}):
        raise ValueError('Raft pending final unsupported binding or payload')
    return value


async def publish_pending(adapter, db, admission_id, *, recovering=False):
    """Consume an authentic settled descriptor with the existing pending-final CAS.

    This bounded plugin entry is available to startup/caller owners. The generic
    pending_final recovery loop still needs explicit per-call plan plumbing; no
    implicit adapter cache is installed to conceal that missing caller ABI.
    """
    import asyncio
    from gateway import pending_final as pending
    from gateway.strict_transport import strict_call

    try:
        value = await asyncio.to_thread(_pending_value, adapter, db, admission_id)
        source = value['binding']['source']
        part = value['parts']['text']
        if part['state'] != 'pending':
            return SendResult(success=part['state'] == 'delivered', retryable=False,
                              error=None if part['state'] == 'delivered' else 'strict_delivery_unknown: Raft previous attempt')
        expected = _pending_snapshot(value)
        claim_id = admission_id + ':text'
        plan = await asyncio.to_thread(prepare, adapter, 'send', chat_id=source['chat_id'], content=part['content'],
                       reply_to=value['reply_to'], metadata=value['metadata'],
                       execution_id=admission_id, claim_id=claim_id)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error):
        return SendResult(success=False, retryable=False, error='strict_delivery_pending: Raft preclaim refused')
    claimed_value, won, state = await asyncio.to_thread(pending.claim, db, admission_id, 'text', part['content'], recovering=recovering)
    if not won:
        return SendResult(success=state == 'delivered', retryable=False,
                          error=None if state == 'delivered' else 'strict_delivery_' + state + ': Raft claim refused')
    try:
        if _pending_snapshot(claimed_value) != expected:
            raise ValueError('Raft pending final claim snapshot changed')
        claimed = claimed_value['parts']['text']
        await asyncio.to_thread(_pending_value, adapter, db, admission_id, expected=expected, claimed=claimed)
        result = await strict_call(adapter, 'send', chat_id=source['chat_id'], content=part['content'],
                                  reply_to=value['reply_to'], metadata=value['metadata'], prepared_plan=plan,
                                  execution_id=admission_id, claim_id=claim_id)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error,
            aiohttp.ClientError, TimeoutError):
        result = SendResult(success=False, retryable=False, error='strict_delivery_unknown: Raft consumed send failed')
    try:
        if result.success:
            await asyncio.to_thread(_pending_value, adapter, db, admission_id, expected=expected,
                                    claimed=claimed_value['parts']['text'])
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
        result = SendResult(success=False, retryable=False, error='strict_delivery_unknown: Raft result invalidated')
    try:
        await asyncio.to_thread(pending.finish, db, admission_id, 'text', result)
    except (ValueError, TypeError, OSError, RuntimeError, sqlite3.Error):
        return SendResult(success=False, retryable=False, error='strict_delivery_unknown: Raft receipt persistence failed')
    return result
