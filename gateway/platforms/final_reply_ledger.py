"""Ordinary reply ledger phases retained behind BasePlatformAdapter's small facade."""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def deliver_attachments(adapter, event, extracted, metadata, *, anything_sent, record_delivery):
    from gateway.session_delivery_policy import strict_event
    from gateway.final_delivery import send_attachments, _attachments_content
    from gateway.pending_final import has_pending_final, claim_event, finish_event
    from gateway.platforms.base import SendResult
    if strict_event(event):
        return await send_attachments(adapter, event, extracted, metadata,
            anything_sent=anything_sent, record_delivery=record_delivery)
    if not (extracted.images or extracted.media_files or extracted.local_files) or not await asyncio.to_thread(has_pending_final, event):
        return await adapter._deliver_attachments_unsealed(event, extracted, metadata,
            anything_sent=anything_sent, record_delivery=record_delivery)
    delivery_adapter = adapter._final_delivery_adapter(event.source)
    try:
        content = await asyncio.to_thread(_attachments_content, extracted)
        receipt = await claim_event(event, 'attachments', content,
            adapter_profile=getattr(delivery_adapter, '_owner_profile', None), check_profile=True,
            metadata=metadata, check_context=True)
        if receipt is None or not receipt[2]:
            state = receipt[3] if receipt is not None else 'missing'
            record_delivery(SendResult(success=state == 'delivered', error=None if state == 'delivered' else state))
            return
    except Exception as exc:
        logger.exception('Human attachment claim refused before upload')
        record_delivery(SendResult(success=False, error=str(exc)))
        return
    results = []
    try:
        await delivery_adapter._deliver_attachments_unsealed(event, extracted, metadata,
            anything_sent=anything_sent, record_delivery=results.append)
        result = SendResult(success=bool(results) and all(getattr(r, 'success', False) for r in results),
            error=None if results and all(getattr(r, 'success', False) for r in results) else 'attachment ACK missing or partial delivery')
    except BaseException as exc:
        await asyncio.shield(finish_event(receipt, 'attachments', SendResult(success=False, error=str(exc))))
        raise
    record_delivery(await finish_event(receipt, 'attachments', result))


async def record(adapter, event, session_key, text, delivery_adapter, ephemeral):
    """Best-effort for ordinary replies; automation uses final_delivery's strict claim."""
    from gateway.pending_final import claim_event
    receipt = await claim_event(event, 'text', text, route=session_key,
        adapter_profile=getattr(delivery_adapter, '_owner_profile', None), check_profile=True)
    if receipt is not None:
        if not receipt[2]:
            raise RuntimeError('pending final claim refused: ' + receipt[3])
        return receipt
    if ephemeral or str(event.text or '').lstrip().startswith(('/', adapter.typed_command_prefix or '!')):
        return None
    try:
        from gateway.delivery_ledger import (
            compute_obligation_id, ledger_enabled, mark_attempting, record_obligation)
        if not await asyncio.to_thread(ledger_enabled):
            return None
        source = event.source
        message_id = getattr(event, 'ledger_message_id', None)
        if message_id is None:
            message_id = getattr(event, 'message_id', '')
        oid = compute_obligation_id(session_key, str(message_id or ''), text)
        await asyncio.to_thread(record_obligation, obligation_id=oid, session_key=session_key,
            platform=str(getattr(source.platform, 'value', source.platform)), chat_id=source.chat_id,
            thread_id=getattr(source, 'thread_id', None), content=text,
            adapter_profile=getattr(delivery_adapter, '_owner_profile', None))
        await asyncio.to_thread(mark_attempting, oid)
        return oid
    except Exception:
        logger.debug('delivery ledger record failed', exc_info=True)
        return None


async def finalize(adapter, oid, result, event, delivery_adapter):
    """Preserve the ordinary reconnect/flood retry contract and timer ownership."""
    if isinstance(oid, tuple):
        from gateway.pending_final import finish_event
        receipt = oid
        if getattr(result, 'success', False):
            await finish_event(receipt, 'text', result)
            return
        from gateway.delivery_ledger import record_human_final_failure
        oid = await asyncio.to_thread(record_human_final_failure, receipt[4],
                                      str(getattr(result, 'error', '') or ''), db=receipt[0])
        await finish_event(receipt, 'text', result)
        if oid is None:
            return
    try:
        from gateway.dead_targets import classify_dead_error
        from gateway.delivery_ledger import is_reconnect_only, mark_delivered, mark_failed
        if getattr(result, 'success', False):
            await asyncio.to_thread(mark_delivered, oid)
            return
        error = str(getattr(result, 'error', '') or '')
        await asyncio.to_thread(mark_failed, oid, error)
        if is_reconnect_only(error):
            redeliver = getattr(adapter.gateway_runner, '_redeliver_failed_obligations_for_platform', None)
            live = adapter._final_delivery_adapter(event.source)
            if live is not delivery_adapter and callable(redeliver):
                await redeliver(event.source.platform, profile=getattr(delivery_adapter, '_owner_profile', None))
        elif classify_dead_error(error) is None:
            schedule = getattr(adapter.gateway_runner, '_schedule_flood_redelivery', None)
            if callable(schedule):
                schedule(event.source.platform, profile=getattr(delivery_adapter, '_owner_profile', None))
    except Exception:
        logger.debug('delivery ledger update failed', exc_info=True)
