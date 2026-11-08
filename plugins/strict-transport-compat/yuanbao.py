import hashlib
import logging
import sys
from pathlib import Path
from types import MethodType

from gateway.strict_transport import single_attempt_transport, strict_transport

logger = logging.getLogger(__name__)
_METHODS = ('send', 'send_image', 'send_image_file', 'send_document')
_AUDITED = {
    'adapter.py': 'c5afef3c220200bc17b603c45696c6afdc9b20fcf38dd818b66b40a1d446e6c5',
    'send_ack.py': '81e9577e9fb5c07c63971e7c4d5e5d62a284596d808f5bcac44feea3f0c8b825',
    'yuanbao_media.py': 'ef28de9f8382d987a511e63b9ba1005df4f524b52800d382d7a526ca947fd9ce',
    'yuanbao_proto.py': '95c97b23acbea1a0303c326110a512032b60e59f6ae35f74923119fadc479c23',
}


def _audited_module(adapter):
    try:
        module = sys.modules[type(adapter).__module__]
        if type(adapter) is not module.YuanbaoAdapter:
            return None
        filename = module.__file__
        if filename is None:
            return None
        root = Path(filename).resolve().parent
        for filename, expected in _AUDITED.items():
            if hashlib.sha256((root / filename).read_bytes()).hexdigest() != expected:
                return None
        sender = adapter._outbound.sender
        if type(sender) is not module.MessageSender or type(adapter._connection) is not module.ConnectionManager:
            return None
        if sender.send_text_chunk.__func__ is not module.MessageSender.send_text_chunk:
            return None
        if sender._dispatch_encoded is not module.MessageSender._dispatch_encoded:
            return None
        if any(getattr(adapter, name).__func__ is not getattr(module.YuanbaoAdapter, name) for name in _METHODS):
            return None
        return module
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None


def adapt(native, adapter):
    if getattr(adapter, '_fork_yuanbao_strict', False):
        return
    module = _audited_module(adapter)
    if module is None:
        logger.warning('Yuanbao strict transport unavailable: unaudited plugin implementation')
        return
    sender = adapter._outbound.sender
    original_chunk = sender.send_text_chunk
    original_dispatch = sender._dispatch_encoded
    state = {'active': 0, 'retired': False}

    def forward(original):
        @single_attempt_transport
        async def call(self, *args, **kwargs):
            if state['retired'] and strict_transport():
                raise RuntimeError('strict transport compatibility was unloaded')
            state['active'] += 1
            try:
                return await original(*args, **kwargs)
            finally:
                state['active'] -= 1
                if state['retired'] and not state['active']:
                    restore()
        return MethodType(call, adapter)

    public = {name: forward(getattr(adapter, name)) for name in _METHODS}

    async def chunk(self, chat_id, text, reply_to=None, retry=3, group_code=''):
        return await original_chunk(chat_id, text, reply_to,
            retry=1 if strict_transport() else retry, group_code=group_code)

    async def dispatch(target, encoded, req_id):
        if not strict_transport():
            return await original_dispatch(target, encoded, req_id)
        try:
            response = await target._connection.send_biz_request(encoded, req_id=req_id)
            head = response.get('head') if isinstance(response, dict) else None
            if (not isinstance(head, dict) or head.get('cmd_type') != module.CMD_TYPE['Response']
                    or head.get('status') != 0 or head.get('msg_id') != req_id):
                return {'success': False, 'error': 'Yuanbao message ACK missing or rejected'}
            return module.ack_from_response(target.name, response)
        except TimeoutError:
            return {'success': False, 'error': 'Yuanbao message ACK timed out'}
        except Exception as exc:
            logger.warning('Yuanbao strict delivery ACK failed', exc_info=True)
            return {'success': False, 'error': str(exc) or type(exc).__name__}

    targets = [(sender, 'send_text_chunk', MethodType(chunk, sender)),
               (sender, '_dispatch_encoded', dispatch),
               *((adapter, name, method) for name, method in public.items()),
               (adapter, '_fork_yuanbao_strict', True)]
    previous = [(target, name, name in vars(target), vars(target).get(name), method)
                for target, name, method in targets]
    for target, name, method in targets:
        setattr(target, name, method)

    def restore():
        for target, name, present, value, method in reversed(previous):
            if vars(target).get(name) is method:
                if present:
                    setattr(target, name, value)
                else:
                    delattr(target, name)

    def undo():
        state['retired'] = True
        for method in public.values():
            vars(method.__func__).pop('_single_attempt_transport', None)
        if not state['active']:
            restore()
    return undo
