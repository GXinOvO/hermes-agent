"""At-most-once automatic transport attempts for runtime-bound automation finals."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from gateway.pending_final import DeliveryUncertain
from gateway.session_delivery_policy import delivery_binding, claim_for_event
from gateway.strict_transport import strict_call

logger = logging.getLogger(__name__)


async def _claim(adapter, event, route, content, kind, *, reply_to=None, metadata=None):
    from gateway.pending_final import claim_event
    receipt = await claim_event(event, kind, content, route=route,
        adapter_profile=getattr(adapter, '_owner_profile', None), check_profile=True,
        metadata=metadata, reply_to=reply_to, check_context=True)
    if receipt is not None:
        return receipt, receipt[2], receipt[3]
    from gateway.delivery_ledger import claim_automation_send
    binding = await asyncio.to_thread(delivery_binding, event, route)
    if binding is None:
        raise RuntimeError('strict delivery requires a runtime automation claim')
    return await asyncio.to_thread(claim_automation_send, binding=binding, session_key=route,
        platform=str(getattr(event.source.platform, 'value', event.source.platform)),
        chat_id=event.source.chat_id, thread_id=getattr(event.source, 'thread_id', None),
        content=content, adapter_profile=getattr(adapter, '_owner_profile', None), payload_kind=kind)


def _blocked(state, error=''):
    from gateway.platforms.base import SendResult
    return SendResult(success=state == 'delivered', error=None if state == 'delivered' else
        ('automation_delivery_pending: transport refused before claim' if state == 'pending' else
         'automation_delivery_unknown: reconcile before retrying') + (': ' + error if error else ''))


async def _finish(oid, result, kind='text'):
    if isinstance(oid, tuple):
        from gateway.pending_final import finish_event
        return await finish_event(oid, kind, result)
    from gateway.delivery_ledger import mark_delivered, mark_unknown
    try:
        if getattr(result, 'success', False):
            await asyncio.to_thread(mark_delivered, oid)
        else:
            await asyncio.to_thread(mark_unknown, oid, str(getattr(result, 'error', '') or 'ACK missing'))
    except Exception:
        # ACK in memory is not a durable receipt. Keep attempting for orphan -> unknown.
        logger.exception('Automation final receipt persistence failed for %s', oid)
        return _blocked('unknown', 'receipt persistence failed')
    return result


async def send_text(adapter, event, route, text, *, reply_to, metadata):
    """One adapter invocation; never transient retry, fallback or an extra failure notice."""
    try:
        from gateway.pending_final_transport import calls_for_event
        calls = await calls_for_event(adapter, event, 'text', text,
            [('send', {'content': text, 'reply_to': reply_to})], metadata)
    except DeliveryUncertain as exc:
        return _blocked('unknown', str(exc))
    except (RuntimeError, OSError) as exc:
        return _blocked('pending', str(exc))
    try:
        oid, claimed, state = await _claim(adapter, event, route, text, 'text', reply_to=reply_to, metadata=metadata)
    except Exception as exc:
        logger.exception('Automation final claim refused; no send attempted')
        return _blocked('unknown', str(exc))
    if not claimed:
        return _blocked(state)
    try:
        if isinstance(oid, tuple):
            from gateway.pending_final_claim import current
            if not await asyncio.to_thread(current, oid, 'text'):
                return await _finish(oid, _blocked('unknown', 'pending final claim invalidated'))
        method, kwargs = calls[0]
        result = await strict_call(adapter, method, **kwargs)
    except Exception as exc:
        logger.exception('Automation final transport raised; delivery is unknown')
        result = _blocked('unknown', str(exc))
    return await _finish(oid, result)


def _attachments_content(extracted):
    """Freeze exact local bytes as well as the routing/URLs, without persisting file contents."""
    files = [p for p, _ in extracted.media_files] + list(extracted.local_files)
    local = []
    for path in files:
        digest = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        local.append([str(path), digest.hexdigest()])
    return json.dumps({'images':extracted.images, 'media':extracted.media_files,
        'files':local, 'as_document':extracted.force_document_attachments}, sort_keys=True)


async def send_attachments(adapter, event, extracted, metadata, *, anything_sent, record_delivery):
    return await _send_attachments(adapter, event, extracted, metadata, record_delivery=record_delivery)


def attachment_calls(adapter, extracted):
    """Resolve the actual methods up front so capability refusal cannot spend a claim."""
    from gateway.platforms.base import _IMAGE_EXTS, _VIDEO_EXTS, should_send_media_as_audio
    from gateway.strict_transport import supports_strict_transport
    batch = supports_strict_transport(adapter, 'send_multiple_images')
    calls = [('send_multiple_images', {'images':[(url, alt)]}) if batch else
             ('send_image', {'image_url':url, 'caption':alt}) for url, alt in extracted.images]
    files = [(path, voice, True) for path, voice in extracted.media_files]
    files += [(path, False, False) for path in extracted.local_files]
    for path, voice, media_tag in files:
        ext = Path(path).suffix.lower()
        if media_tag and should_send_media_as_audio(adapter.platform, ext, is_voice=voice):
            method, kwargs = 'send_voice', {'audio_path':path, 'is_voice':voice}
        elif ext in _VIDEO_EXTS:
            method, kwargs = 'send_video', {'video_path':path}
        elif ext in _IMAGE_EXTS and not voice and not extracted.force_document_attachments:
            method, kwargs = 'send_image_file', {'image_path':path}
        else:
            method, kwargs = 'send_document', {'file_path':path}
        calls.append((method, kwargs))
    return calls


async def _send_attachments(adapter, event, extracted, metadata, *, record_delivery):
    """One whole attachment bundle claim: a partial batch stays unknown, never replayed."""
    from gateway.platforms.base import SendResult
    if not (extracted.images or extracted.media_files or extracted.local_files):
        return
    from gateway.session_delivery_policy import native_claim_for_event
    claim = native_claim_for_event(event) or claim_for_event(event)
    if claim is None:
        record_delivery(_blocked('unknown', 'runtime claim missing'))
        return
    try:
        from gateway.pending_final_transport import calls_for_event
        content = await asyncio.to_thread(_attachments_content, extracted)
        calls = await calls_for_event(adapter, event, 'attachments', content,
            attachment_calls(adapter, extracted), metadata)
    except DeliveryUncertain as exc:
        record_delivery(_blocked('unknown', str(exc)))
        return
    except RuntimeError as exc:
        record_delivery(_blocked('pending', str(exc)))
        return
    except OSError as exc:
        logger.exception('Automation attachment payload unavailable before claim')
        record_delivery(_blocked('pending', str(exc)))
        return
    try:
        oid, claimed, state = await _claim(adapter, event, claim.route, content, 'attachments', metadata=metadata)
    except Exception as exc:
        logger.exception('Automation attachment claim refused; no upload attempted')
        record_delivery(_blocked('unknown', str(exc)))
        return
    if not claimed:
        record_delivery(_blocked(state))
        return
    results = []
    try:
        for method, kwargs in calls:
            if isinstance(oid, tuple):
                from gateway.pending_final_claim import current
                if not await asyncio.to_thread(current, oid, 'attachments'):
                    raise DeliveryUncertain('pending final claim invalidated')
            results.append(await strict_call(adapter, method, **kwargs))
            if not getattr(results[-1], 'success', False):
                break
        result = SendResult(success=bool(results) and all(getattr(r, 'success', False) for r in results),
                            error=None if results and all(getattr(r, 'success', False) for r in results)
                            else 'attachment ACK missing or partial delivery')
    except Exception as exc:
        logger.exception('Automation attachment transport raised; delivery is unknown')
        result = _blocked('unknown', str(exc))
    record_delivery(await _finish(oid, result, 'attachments'))
