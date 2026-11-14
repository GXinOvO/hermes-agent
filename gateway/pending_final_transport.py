import asyncio
import hashlib



def snapshot(value):
    from gateway.pending_final_claim import snapshot as fingerprint
    return fingerprint(value)


def identity(value, kind, index):
    binding = value['binding']
    execution = 'final:' + binding['admission_id'] + ':' + str(binding['generation']) + ':' + binding['input_hash']
    return {'execution_id': execution,
            'claim_id': kind + ':' + value['parts'][kind]['payload_hash'] + ':' + str(index)}


async def prepare(adapter, value, source):
    from gateway.pending_final import DeliveryUncertain
    from gateway.platforms.base import _ExtractedResponse
    from gateway.final_delivery import attachment_calls, _attachments_content
    from gateway.strict_transport import prepare_call
    from gateway.session_delivery_policy import STRICT
    plans = []
    for kind in ('text', 'attachments'):
        part = value['parts'].get(kind)
        if part is None or part['state'] == 'delivered':
            continue
        if part['state'] != 'pending':
            if value['binding']['policy'] == STRICT:
                raise DeliveryUncertain('earlier delivery is not confirmed')
            continue
        if kind == 'text':
            calls = [('send', {'content': part['content'], 'reply_to': value['reply_to']})]
        else:
            extracted = _ExtractedResponse(**part['extracted'])
            if await asyncio.to_thread(_attachments_content, extracted) != part['content']:
                raise RuntimeError('strict delivery payload changed before claim')
            calls = attachment_calls(adapter, extracted)
        prepared = []
        for index, (method, kwargs) in enumerate(calls):
            if value['binding']['policy'] == STRICT:
                prepared.append(await prepare_call(adapter, method, chat_id=source.chat_id,
                    metadata=value['metadata'], **identity(value, kind, index), **kwargs))
            else:
                prepared.append((method, dict(chat_id=source.chat_id, metadata=value['metadata'], **kwargs)))
        plans.append((kind, part, prepared))
    return plans


async def event_plans(adapter, event, value):
    from gateway.pending_final import DeliveryUncertain
    current = snapshot(value)
    cache = getattr(event, '_prepared_final_transport', None)
    if cache is not None:
        cached_adapter, cached_snapshot, plans = cache
        if cached_adapter is not adapter or cached_snapshot != current:
            raise DeliveryUncertain('prepared final context changed')
        return plans
    plans = await prepare(adapter, value, event.source)
    event._prepared_final_transport = (adapter, current, plans)
    return plans


async def calls_for_event(adapter, event, kind, content, calls, metadata):
    from gateway.pending_final import DeliveryUncertain, prepare_event
    from gateway.strict_transport import prepare_call
    saved = await prepare_event(adapter, event)
    if saved is not None:
        value, plans = saved
        part = value['parts'].get(kind)
        if part is None or part['content'] != content:
            raise DeliveryUncertain('prepared final payload changed')
        return next((prepared for name, _part, prepared in plans if name == kind), [])
    from gateway.session_delivery_policy import claim_for_event
    claim = claim_for_event(event)
    if claim is None:
        raise DeliveryUncertain('runtime claim missing')
    execution = 'final:' + claim.admission + ':' + str(claim.generation) + ':' + claim.payload_digest
    digest = hashlib.sha256(content.encode()).hexdigest()
    return [await prepare_call(adapter, method, chat_id=event.source.chat_id, metadata=metadata,
            execution_id=execution, claim_id=kind + ':' + digest + ':' + str(index), **kwargs)
            for index, (method, kwargs) in enumerate(calls)]
