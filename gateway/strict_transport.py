"""Runtime suppression of gateway transport retries after a durable send claim.

Use ``with strict_transport_scope(): await adapter.send(...)`` in a consumer that
already owns a persisted send claim. This scope grants no admission or delivery
identity. It disables BasePlatformAdapter._send_with_retry retries, fallbacks and
failure notices. Adapter/SDK-specific internal retries require their own audited
support; the scope is not a claim of platform-wide exactly-once delivery.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_strict: ContextVar[bool] = ContextVar('strict_outbound_transport', default=False)
_SINGLE_ATTEMPT = object()
_UNPREPARED = object()


def strict_transport():
    return _strict.get()


def single_attempt_transport(method):
    method._single_attempt_transport = _SINGLE_ATTEMPT
    return method


def supports_strict_transport(adapter, method='send'):
    operation = getattr(adapter, method, None)
    operation = getattr(operation, '__func__', operation)
    return getattr(operation, '_single_attempt_transport', None) is _SINGLE_ATTEMPT


def require_strict_transport(adapter, method='send'):
    operation = getattr(adapter, method, None)
    function = getattr(operation, '__func__', operation)
    if getattr(function, '_single_attempt_transport', None) is not _SINGLE_ATTEMPT:
        raise RuntimeError('strict_transport_unverified: ' + type(adapter).__name__ + '.' + method)
    return operation


async def strict_call(adapter, method='send', *, prepared_plan=_UNPREPARED,
                      execution_id=None, claim_id=None, **kwargs):
    require_strict_transport(adapter, method)
    identity = {} if execution_id is None and claim_id is None else {'execution_id': execution_id, 'claim_id': claim_id}
    if prepared_plan is _UNPREPARED:
        prepared_plan = await prepare_strict_transport(adapter, method, **identity, **kwargs)
    if prepared_plan is None and getattr(type(adapter), '_strict_plan_requires_claim', False):
        raise RuntimeError('strict_transport_unavailable: claim-bound plan required')
    if prepared_plan is not None:
        kwargs['prepared_plan'] = prepared_plan
        kwargs.update(identity)
    operation = require_strict_transport(adapter, method)
    with strict_transport_scope():
        return await operation(**kwargs)


def _local_payloads(kwargs):
    from pathlib import Path
    from urllib.parse import unquote, urlsplit
    paths = [kwargs[key] for key in ('image_path', 'audio_path', 'video_path', 'file_path') if kwargs.get(key)]
    urls = [kwargs[key] for key in ('image_url', 'animation_url') if kwargs.get(key)]
    urls.extend(url for url, _caption in kwargs.get('images', ()))
    paths.extend(unquote(urlsplit(url).path) for url in urls if str(url).startswith('file://'))
    for path in paths:
        with Path(path).open('rb') as stream:
            stream.read(1)


async def prepare_strict_transport(adapter, method='send', **kwargs):
    """Capability probe plus generic payload preparation, never a durable claim plan.

    An adapter whose plans bind a durable claim (``_strict_plan_requires_claim``)
    refuses here: nothing in this call carries the claim the plan would bind, so
    minting one would produce a plan no send can honour. Those adapters issue
    plans through ``prepare_call``.
    """
    import asyncio
    require_strict_transport(adapter, method)
    if getattr(type(adapter), '_strict_plan_requires_claim', False):
        return None
    try:
        await asyncio.to_thread(_local_payloads, kwargs)
        prepare = getattr(type(adapter), '_prepare_strict_transport', None)
        if prepare is not None:
            return await prepare(adapter, method, **kwargs)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError('strict_transport_payload_unavailable: ' + str(exc)) from exc


async def prepare_call(adapter, method='send', *, execution_id=None, claim_id=None, **kwargs):
    import asyncio
    import copy
    kwargs = copy.deepcopy(kwargs)
    identity = {} if execution_id is None and claim_id is None else {'execution_id': execution_id, 'claim_id': claim_id}
    require_strict_transport(adapter, method)
    claim_bound = getattr(type(adapter), '_strict_plan_requires_claim', False)
    if claim_bound and (not isinstance(execution_id, str) or not execution_id
                        or not isinstance(claim_id, str) or not claim_id):
        raise ValueError('strict_transport_claim_identity_required')
    plan = None
    try:
        await asyncio.to_thread(_local_payloads, kwargs)
        prepare = getattr(type(adapter), '_prepare_strict_transport', None)
        if prepare is not None:
            plan = await prepare(adapter, method, **identity, **kwargs)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError('strict_transport_payload_unavailable: ' + str(exc)) from exc
    if plan is not None:
        kwargs.update(identity)
    return method, dict(kwargs, prepared_plan=plan)


@contextmanager
def strict_transport_scope():
    token = _strict.set(True)
    try:
        yield
    finally:
        _strict.reset(token)
