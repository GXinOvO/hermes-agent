import json


async def prepare(state, calls, execution, target, index):
    from cron.strict_thread_delivery import digest, encode
    from gateway.strict_transport import prepare_call
    claim = 'target:' + str(index) + ':' + digest(target)
    prepared = [await prepare_call(state.runtime_adapter, method, chat_id=str(state.chat_id),
                execution_id=execution, claim_id=claim + ':' + str(number), **kwargs)
                for number, (method, kwargs) in enumerate(calls)]
    if all(kwargs['prepared_plan'] is None for _method, kwargs in prepared):
        return calls
    if any(method != 'send' or kwargs['prepared_plan'] is None for method, kwargs in prepared):
        raise RuntimeError('cron opaque transport requires a complete text plan')
    identity = binding(state.runtime_adapter, prepared)
    encode(identity)
    return {'kind': 'prepared', '_prepared_transport': True, 'adapter': state.runtime_adapter,
            'calls': prepared, 'identity': identity}


def binding(adapter, calls):
    from cron.strict_thread_delivery import digest, encode
    hook = getattr(adapter, '_strict_transport_binding', None)
    if hook is None:
        raise RuntimeError('cron opaque transport audit binding unavailable')
    value = [{'method': method, 'binding': hook(kwargs['prepared_plan']),
              'arguments_sha256': digest({key: value for key, value in kwargs.items() if key != 'prepared_plan'})}
             for method, kwargs in calls]
    return json.loads(encode(value))


def prepare_target(state, calls, job, content, failure, target, index, loop):
    from agent.async_utils import safe_schedule_threadsafe
    from cron.strict_delivery import _durable_media
    execution, media = _durable_media(job, content, failure)
    future = safe_schedule_threadsafe(prepare(state, calls, execution, target, index), loop)
    if future is None:
        raise RuntimeError('cron transport loop unavailable')
    value = future.result(timeout=30)
    if isinstance(value, dict) and media:
        raise RuntimeError('cron opaque media transport unavailable')
    return value


def send(state, frozen):
    from agent.async_utils import safe_schedule_threadsafe
    from cron import scheduler_delivery as delivery
    from gateway.strict_transport import strict_call
    adapter, calls = frozen['adapter'], frozen['calls']
    if adapter is not state.runtime_adapter or binding(adapter, calls) != frozen['identity']:
        raise RuntimeError('cron opaque transport changed after claim')

    async def submit():
        last_id = None
        for method, kwargs in calls:
            result = await strict_call(adapter, method, **kwargs)
            field = delivery._result_field
            if not field(result, 'success', False) or (field(result, 'raw_response', None) or {}).get('ambiguous'):
                raise RuntimeError(str(field(result, 'error') or 'cron opaque ACK missing'))
            last_id = field(result, 'message_id') or last_id
        return last_id

    future = safe_schedule_threadsafe(submit(), state.loop)
    if future is None:
        raise RuntimeError('cron opaque delivery loop unavailable after claim')
    last_id = future.result(timeout=delivery._get_standalone_send_timeout())
    delivery._seed_live_delivery_sessions(state, last_id)
    return {'ok': True, 'message_id': last_id}
