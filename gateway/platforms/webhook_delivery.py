"""Validated webhook destinations live in the owner's admitted input, not a TTL cache."""
from copy import deepcopy
import asyncio
import hashlib
import json
import os
import subprocess

from hermes_state_runtime import RuntimeStoreError

GITHUB_API = 'https://api.github.com'


def route_digest(adapter, chat_id):
    from gateway.platforms.webhook import _WebhookDeliveryIdentity
    adapter._reload_dynamic_routes()
    try:
        route_name = _WebhookDeliveryIdentity.route_from_chat_id(chat_id)
    except (ValueError, TypeError, IndexError):
        raise RuntimeStoreError('not_found') from None
    route = adapter._routes.get(route_name)
    if route is None:
        raise RuntimeStoreError('not_found')
    return hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_destination(delivery):
    from gateway.platforms.webhook import _is_known_platform, _REPO_RE
    if (not isinstance(delivery, dict)
            or not {'deliver', 'deliver_extra'} <= set(delivery)
            or set(delivery) - {'deliver', 'deliver_extra', 'profile', 'mirror', 'route'}
            or (delivery.get('profile') is not None and not isinstance(delivery['profile'], str))
            or not isinstance(delivery.get('mirror', False), bool)
            or (delivery.get('route') is not None and not isinstance(delivery['route'], str))):
        raise RuntimeStoreError('invalid_params')
    target, extra = delivery['deliver'], delivery['deliver_extra']
    if not isinstance(target, str) or not isinstance(extra, dict):
        raise RuntimeStoreError('invalid_params')
    if target == 'github_comment':
        repo, number = extra.get('repo'), extra.get('pr_number')
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo) or not str(number).isdigit() or int(number) <= 0:
            raise RuntimeStoreError('invalid_params')
    elif target != 'log' and not _is_known_platform(target):
        raise RuntimeStoreError('invalid_params')
    # Configuration extras may contain credentials or whole rendered payloads.
    # Persist only fields the selected sender actually consumes.
    fields = {'log': (), 'github_comment': ('repo', 'pr_number')}.get(
        target, ('chat_id', 'message_thread_id', 'thread_id'))
    result = {'deliver': target, 'deliver_extra': deepcopy({key: extra[key] for key in fields if key in extra})}
    # Omit the unbound defaults to preserve pre-profile receipt fingerprints.
    if delivery.get('profile'):
        result['profile'] = delivery['profile']
    # Route opt-in (``mirror_to_session: true``): the delivered text is mirrored into the target chat's
    # session; the destination row carries it so a replayed delivery mirrors exactly like the first.
    if delivery.get('mirror') is True:
        result['mirror'] = True
        if delivery.get('route'):
            result['route'] = delivery['route']  # the mirror's label only
    return result


def snapshot_destination(adapter, delivery):
    from gateway.config import Platform

    result = validate_destination(delivery)
    if result['deliver'] not in {'log', 'github_comment'} and not result['deliver_extra'].get('chat_id'):
        profile = result.get('profile')
        with adapter._profile_scope(profile):
            home = adapter._delivery_config(profile).get_home_channel(Platform(result['deliver']))
        if home is None:
            raise RuntimeStoreError('not_found')
        result['deliver_extra']['chat_id'] = home.chat_id
    return result


async def prepare_strict_delivery(adapter, delivery, content=''):
    from gateway.config import Platform
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.strict_transport import prepare_strict_transport
    kind = delivery['deliver']
    if kind == 'log':
        return
    if kind == 'github_comment':
        await asyncio.to_thread(_github_credentials, adapter, delivery)
        return
    target = adapter._find_adapter(Platform(kind), delivery.get('profile'))
    if target is None or isinstance(target, WebhookAdapter):
        raise RuntimeError('strict webhook destination unavailable')
    with adapter._profile_scope(delivery.get('profile')):
        chat_id = delivery['deliver_extra'].get('chat_id')
        if not chat_id:
            home = adapter._delivery_config(delivery.get('profile')).get_home_channel(Platform(kind))
            if home is None:
                raise RuntimeError('strict webhook destination missing')
            chat_id = home.chat_id
        thread_id = delivery['deliver_extra'].get('message_thread_id') or delivery['deliver_extra'].get('thread_id')
        gate = adapter.gateway_runner._shared_bot_target_allowed
        if not gate(Platform(kind), delivery.get('profile'), chat_id, thread_id):
            raise RuntimeError('strict webhook destination not routed to profile')
        await prepare_strict_transport(target, 'send', chat_id=chat_id, content=content,
            metadata={'thread_id':thread_id} if thread_id else None)


def _github_credentials(adapter, delivery):
    from gateway.platforms.webhook import _GH_TOKEN_VARS
    with adapter._profile_scope(delivery.get('profile')):
        env = adapter._github_env(delivery.get('profile'))
        env = os.environ if env is None else env
        host = env.get('GH_HOST', 'github.com')
        if host != 'github.com':
            raise RuntimeError('strict enterprise GitHub host unverified')
        token = next((env.get(name) for name in _GH_TOKEN_VARS if env.get(name)), None)
        if not token:
            try:
                result = subprocess.run(['gh', 'auth', 'token', '--hostname', host], env=dict(env),
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
                token = result.stdout.strip() if result.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                token = None
        if not token:
            raise RuntimeError('strict GitHub authentication unavailable')
        return {'Authorization':'Bearer ' + token, 'Accept':'application/vnd.github+json',
            'User-Agent':'hermes-webhook', 'X-GitHub-Api-Version':'2022-11-28'}


async def send_github_comment(adapter, content, delivery):
    import aiohttp
    from gateway.platforms.base import SendResult
    extra = delivery['deliver_extra']
    try:
        headers = await asyncio.to_thread(_github_credentials, adapter, delivery)
        url = f"{GITHUB_API}/repos/{extra['repo']}/issues/{int(extra['pr_number'])}/comments"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            async with session.post(url, json={'body':content}, headers=headers, allow_redirects=False) as response:
                if response.status != 201:
                    return SendResult(success=False, error=f'GitHub comment ACK missing: HTTP {response.status}')
                data = await response.json()
                if type(data.get('id')) is not int or data['id'] <= 0:
                    return SendResult(success=False, error='GitHub comment ACK invalid')
                return SendResult(success=True, message_id=str(data['id']))
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
        return SendResult(success=False, error='GitHub comment ACK missing: ' + type(exc).__name__)


def retained_destination(adapter, chat_id):
    """Readback also checks today's connector and authorization; retained data is not a grant."""
    from gateway.session_envelope import restore_native, _validate_native
    runner = adapter.gateway_runner or getattr(adapter._message_handler, '__self__', None)
    from gateway.session_authorities import active_authority, all_authorities, serving_authorities
    if not all_authorities(runner):
        return validate_destination(adapter._delivery_info.get(chat_id))
    # The reply leg runs in the routed profile's scope; that ledger holds the destination. A
    # delivery id is unique per route, so an unscoped caller may search every served ledger (a
    # parked profile's retained store is not served: reading it could fail every other profile's).
    scoped = active_authority(runner)
    candidates = [scoped] if scoped is not None else serving_authorities(runner)
    # Only the signed ingress admission carries the destination; later automation rows
    # (process/watch completions) on the same chat share its source but never compete.
    query = """SELECT payload_json FROM session_admissions
        WHERE json_extract(payload_json, '$.native_text_v1.source.chat_id')=?
          AND json_extract(payload_json, '$.native_text_v1.source.platform')='webhook'
          AND json_extract(payload_json, '$.native_text_v1.automation') IS NULL"""
    rows = [row for authority in candidates for row in authority.db._read_all(query, (chat_id,))]
    if len(rows) != 1:
        raise RuntimeStoreError('not_found')
    payload = json.loads(rows[0]['payload_json'])
    envelope = payload['native_text_v1']
    event = restore_native(payload, runner)
    _validate_native(runner, event, envelope.get('provenance'))
    if runner._adapter_for_source(event.source) is not adapter:
        raise RuntimeStoreError('permission_denied')
    if envelope.get('webhook_route') != route_digest(adapter, chat_id):
        raise RuntimeStoreError('admission_conflict')
    return validate_destination(envelope.get('webhook_delivery'))
