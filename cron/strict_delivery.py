"""A cron queue claim owns one frozen, prevalidated transport plan."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

logger = logging.getLogger('cron.scheduler')

_current: ContextVar[object] = ContextVar('cron_delivery_claim', default=None)


class UnknownDelivery(str):
    pass


class DeferredDelivery(Exception):
    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = receipt


def media_identity(content):
    import hashlib
    from gateway.platforms.base import BasePlatformAdapter
    files, _ = BasePlatformAdapter.extract_media(content)
    identities = []
    for path, voice in files:
        digest = hashlib.sha256()
        try:
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError:
            identities.append([path, voice, None])
        else:
            identities.append([path, voice, digest.hexdigest()])
    return identities


@contextmanager
def delivery_scope(execution_id, prepared):
    token = _current.set((execution_id, prepared))
    try:
        yield
    finally:
        _current.reset(token)


def active():
    return _current.get() is not None


def standalone_send(state, content, media, *, prepared_plan=None, execution_id=None, claim_id=None):
    import asyncio
    import concurrent.futures
    import contextvars
    from tools.send_message_tool import _send_to_platform
    from gateway.strict_transport import strict_transport_scope
    from cron.scheduler_delivery import _get_standalone_send_timeout
    timeout = _get_standalone_send_timeout()
    async def send():
        with strict_transport_scope():
            result = await asyncio.wait_for(_send_to_platform(state.platform, state.pconfig,
                state.chat_id, content, thread_id=state.thread_id, media_files=media, notify=state.notify_delivery,
                prepared_plan=prepared_plan, execution_id=execution_id, claim_id=claim_id), timeout=timeout)
        if not isinstance(result, dict) or not result.get('success') or result.get('ambiguous') or result.get('warnings'):
            raise RuntimeError(str(result.get('error') if isinstance(result, dict) else 'ACK missing'))
        return result
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(send()), None
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(contextvars.copy_context().run, asyncio.run, send()).result(timeout=timeout + 1), None
        finally:
            pool.shutdown(wait=False)
    except Exception as exc:
        logger.exception('Cron standalone outcome unknown; no retry')
        return None, 'cron standalone outcome unknown: ' + str(exc)


def drain_queue(adapters, loop):
    from cron.delivery_queue import _path, drain
    from cron import scheduler
    if not _path().exists():
        return 0
    def check(job, content, failure):
        return prepare(job, content, failure, adapters=adapters, loop=loop)
    def send(job, content, failure):
        error = scheduler._deliver_result(job, content, adapters=adapters, loop=loop, for_failure=failure)
        if not error and failure and not job.get('_notification_all_targets_suppressed'):
            scheduler._mark_incident_alerted(job.get('_failure_incident_id'))
        return error
    return drain(send, prepare=check)


def _durable_media(job, content, for_failure):
    """Read the canonical pending row; the queue CAS still owns admission."""
    import json
    from pathlib import Path
    from contextlib import closing
    from cron.delivery_queue import _connect
    execution = job.get('execution_id')
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT execution_id, job_json, content, for_failure, media_json FROM deliveries "
            "WHERE status='pending' AND (? IS NULL OR execution_id=?) "
            "AND job_json=? AND content=? AND for_failure=? "
            "ORDER BY julianday(created_at), created_at, execution_id LIMIT 2",
            (execution, execution, json.dumps(job, ensure_ascii=False, sort_keys=True), content, int(bool(for_failure)))).fetchall()
    if len(rows) != 1:
        raise RuntimeError('cron durable plan identity unavailable or ambiguous')
    row = rows[0]
    try:
        media = json.loads(row['media_json'])
        if not isinstance(media, list):
            raise ValueError('media list required')
        expected = []
        for entry in media:
            path, voice, digest = entry
            if (not isinstance(path, str) or type(voice) is not bool or not isinstance(digest, str)
                    or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest)):
                raise ValueError('invalid media identity')
            expected.append([path, voice, digest, Path(path).name])
    except (TypeError, ValueError) as exc:
        raise RuntimeError('cron durable media identity unavailable') from exc
    return row['execution_id'], expected


def _prepare_strict_native(state, calls, retained, job, content, for_failure, target):
    from cron.strict_native_delivery import handles, prepare as prepare_native
    if not handles(job, target):
        return None
    try:
        return prepare_native(state, calls, retained, job, content, for_failure)
    except ValueError as exc:
        if 'SDK/request unavailable' not in str(exc):
            raise
        return None


def prepare(job, content, for_failure, *, adapters, loop):
    from cron import scheduler_delivery as delivery
    from gateway.config import load_gateway_config
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.media_policy import apply_media_policy_env
    from gateway.warning_notifications import warning_notifications_enabled
    from gateway.final_delivery import attachment_calls
    from gateway.platforms.base import _ExtractedResponse
    from cron.scheduler_results import validate_binding
    validate_binding({'binding':job.get('_cron_result_binding')}, job)
    cfg = delivery._sched.load_config() or {}
    apply_media_policy_env(cfg)
    config = load_gateway_config()
    name = job.get('name', job['id'])
    rendered = (f'Cronjob Response: {name}\n(job_id: {job.get("id", "")})\n-------------\n\n{content}\n\n'
        f'To stop or manage this job, send me a new message (e.g. "stop reminder {name}").'
        if cfg.get('cron', {}).get('wrap_response', True) else content)
    media, text = BasePlatformAdapter.extract_media(rendered)
    text = delivery._redact_cron_payload(text, 'delivery content')
    retained = BasePlatformAdapter.filter_media_delivery_paths(media)
    if len(retained) != len(media):
        raise RuntimeError('cron media unavailable before claim')
    _, mirror = BasePlatformAdapter.extract_media(content)
    mirror = delivery._redact_cron_payload((mirror or '').strip(), 'mirror payload')
    from uuid import uuid4
    claim_id = uuid4().hex
    execution_id = job.get('execution_id')
    durable_media = None
    plans = []
    for index, target in enumerate(delivery._resolve_delivery_targets(job, for_failure=for_failure)):
        platform = target['platform']
        if for_failure and platform != delivery.BOT_CHAT_PLATFORM and not warning_notifications_enabled(platform, cfg):
            continue
        if for_failure and platform == delivery.BOT_CHAT_PLATFORM:
            from cron.strict_bot_delivery import suppressed
            if suppressed(target, job, content):
                continue
        if platform in {delivery.BOT_CHAT_PLATFORM, delivery.ROOM_PLATFORM, 'local'}:
            plans.append((target, None, text, []))
            continue
        if platform in {'mattermost', 'feishu', 'google_chat', 'weixin'}:
            from cron.strict_live_http import saved_done
            frozen = saved_done(job, target)
            if frozen is not None:
                plans.append((target, None, text, frozen))
                continue
        errors = []
        state = delivery._prepare_target_delivery(job, target, adapters=adapters, loop=loop, config=config,
            notify_delivery=delivery._cron_delivery_notify_enabled(cfg),
            mirror_enabled=delivery._cron_mirror_delivery_enabled(job, cfg), mirror_text=mirror,
            delivery_errors=errors, open_thread=False)
        if state is None or state.is_relay:
            raise RuntimeError('cron strict live transport unavailable: ' + platform)
        if not state.live_adapter_ready:
            from tools.send_message_strict import prepare as prepare_standalone, prepared_media_identity
            if durable_media is None:
                execution_id, durable_media = _durable_media(job, content, for_failure)
            plan = prepare_standalone(state.platform, state.pconfig, state.chat_id, text,
                media_files=retained, thread_id=state.thread_id, notify=state.notify_delivery,
                execution_id=execution_id, claim_id=claim_id)
            if prepared_media_identity(plan) != durable_media:
                raise RuntimeError('cron frozen media identity differs from durable result')
            plans.append((target, state, text, plan))
            continue
        _, metadata, media_metadata = delivery._live_route_metadata(state)
        from gateway.delivery import DeliveryRouter
        target_text = DeliveryRouter(config, state.target_adapters)._cap_oversized_output(state.runtime_adapter, text, job['id'])
        calls = [('send', {'content':target_text.strip(), 'metadata':metadata})] if target_text.strip() else []
        extracted = _ExtractedResponse(text_content='', images=[], media_files=retained, local_files=[],
            force_document_attachments=False, pre_extract='')
        calls.extend((method, dict(kwargs, metadata=media_metadata)) for method, kwargs in attachment_calls(state.runtime_adapter, extracted))
        if not calls:
            raise RuntimeError('empty cron payload')
        if platform in {'mattermost', 'feishu', 'google_chat', 'weixin'}:
            from cron.strict_live_http import prepare as prepare_live_http
            frozen = prepare_live_http(state, calls, retained, job, content, for_failure, loop, target=target)
            plans.append((target, state, target_text, frozen))
            continue
        frozen = _prepare_strict_native(state, calls, retained, job, content, for_failure, target)
        if frozen is not None:
            plans.append((target, state, target_text, frozen))
            continue
        if state.mirror_this_target and not state.in_channel_surface and not state.thread_id:
            if platform != 'slack':
                raise RuntimeError('strict cron thread creation unverified')
            from plugins.platforms.slack.strict_delivery import freeze_thread
            frozen = freeze_thread(state.runtime_adapter, state, calls, retained)
            if durable_media is None:
                execution_id, durable_media = _durable_media(job, content, for_failure)
            if frozen['media'] != durable_media:
                raise RuntimeError('cron frozen media identity differs from durable result')
            plans.append((target, state, target_text, frozen))
            continue
        from cron.strict_prepared_transport import prepare_target
        frozen = prepare_target(state, calls, job, content, for_failure, target, index, loop)
        plans.append((target, state, target_text, frozen))
    from cron.strict_thread_delivery import prepare_bundle
    thread = prepare_bundle(plans, job, content, for_failure)
    return {'targets':plans, 'media':media_identity(content), 'claim_id':claim_id, 'execution_id':execution_id, 'thread':thread}


def deliver(job, content, for_failure):
    from cron import scheduler_delivery as delivery
    from agent.async_utils import safe_schedule_threadsafe
    from gateway.strict_transport import strict_call
    from gateway.session_cron import deliver_to_local_chat
    from hermes_constants import get_hermes_home
    execution, prepared = _current.get()
    plans = prepared['targets']
    if job.get('execution_id') not in (None, execution) or prepared['execution_id'] not in (None, execution):
        return UnknownDelivery('cron delivery identity changed')
    if prepared.get('thread') is None and any(state is None or state.live_adapter_ready for _, state, _, _ in plans) and prepared['media'] != media_identity(content):
        return UnknownDelivery('cron media bytes changed after preflight')
    if not plans:
        job['_notification_all_targets_suppressed'] = True
        return None
    for _, _, _, frozen in plans:
        if isinstance(frozen, dict) and frozen.get('kind') == 'bot-chat' and (frozen.get('receipt') or {}).get('refused') is True:
            try:
                from cron.strict_bot_delivery import recheck_refusal
                recheck_refusal(frozen)
            except Exception:
                return UnknownDelivery('strict Bot refusal could not be revalidated')
            return UnknownDelivery('strict Bot delivery refused; not resent')
    if prepared.get('thread') is not None:
        from cron.strict_thread_delivery import deliver as deliver_thread
        try:
            deliver_thread(execution, plans)
        except DeferredDelivery as exc:
            return exc
        except Exception as exc:
            logger.exception('Cron thread outcome unknown; no fallback')
            return UnknownDelivery('cron thread outcome unknown: ' + type(exc).__name__)
        return None
    for target, state, text, calls in plans:
        platform = target['platform']
        if state is None:
            if platform == delivery.BOT_CHAT_PLATFORM:
                error = delivery._deliver_to_bot_chat(job, content, target['chat_id'], for_failure=for_failure)
                if error:
                    receipt_target = f"bot-chat:{target['chat_id'] or '(own)'}"
                    receipt = job.get("_bot_chat_delivery_receipts", {}).get(receipt_target)
                    if receipt and receipt.get("status") in {"queued", "claimed"}:
                        return DeferredDelivery(str(error))
                    return UnknownDelivery(str(error))
            elif platform == delivery.ROOM_PLATFORM:
                error = delivery.deliver_to_room(job, content, target['chat_id'], for_failure=for_failure)
            else:
                error = deliver_to_local_chat(get_hermes_home(), target['chat_id'], text, diagnostic=for_failure)
            if error:
                return UnknownDelivery(str(error))
            continue
        if not state.live_adapter_ready:
            from tools.send_message_strict import PreparedPlan
            media = calls.binding.media if isinstance(calls, PreparedPlan) else []
            _result, error = standalone_send(state, text, media, prepared_plan=calls,
                execution_id=prepared['execution_id'], claim_id=prepared['claim_id'])
            if error:
                return UnknownDelivery(error)
            delivery._maybe_mirror_cron_delivery(state.job, state.platform_name, state.chat_id,
                state.mirror_text, thread_id=state.thread_id, user_id=state.origin_user_id,
                enabled=state.mirror_this_target)
            continue
        async def send(state=state, calls=calls):
            last_id = None
            for method, kwargs in calls:
                result = await strict_call(state.runtime_adapter, method, chat_id=str(state.chat_id), **kwargs)
                field = delivery._result_field
                if not field(result, 'success', False) or (field(result, 'raw_response', None) or {}).get('ambiguous'):
                    raise RuntimeError(str(field(result, 'error') or 'cron transport ACK missing'))
                last_id = field(result, 'message_id') or last_id
            return last_id
        future = safe_schedule_threadsafe(send(), state.loop)
        try:
            if future is None:
                raise RuntimeError('cron delivery loop unavailable after claim')
            last_id = future.result(timeout=delivery._get_standalone_send_timeout())
        except Exception as exc:
            logger.exception('Cron send outcome unknown; no fallback: %s', type(exc).__name__)
            return UnknownDelivery('cron transport outcome unknown; no fallback: ' + type(exc).__name__)
        delivery._seed_live_delivery_sessions(state, last_id)
    return None
