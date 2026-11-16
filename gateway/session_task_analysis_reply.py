import asyncio
import logging
from contextvars import copy_context
from functools import partial

from gateway.session_llm_oneshot import DeferredReply, _pool
from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)


class AnalysisReply(DeferredReply):
    def __init__(self, work, finalize):
        super().__init__(work)
        self.finalize = finalize

    def start(self, connection, rid, check=None):
        pool = _pool(connection.authority)
        call = pool.submit(partial(copy_context().run, self.work))
        task = asyncio.get_running_loop().create_task(self.answer(pool, connection, rid, call, check))
        pool.answers[task] = connection

        def settled(done):
            pool.answers.pop(done, None)
            if done.cancelled():
                call.cancel()
        task.add_done_callback(settled)

    async def answer(self, pool, connection, rid, call, check):
        reply = asyncio.wrap_future(call)
        stopping = asyncio.ensure_future(pool.stopping.wait())
        try:
            await asyncio.wait((reply, stopping), return_when=asyncio.FIRST_COMPLETED)
            if pool.stopping.is_set() or not reply.done() or reply.cancelled():
                raise RuntimeStoreError('runtime_draining')
            result = self.finalize(reply.result())
            if check is not None:
                check(result)
            frame = {'jsonrpc': '2.0', 'id': rid, 'result': result}
        except asyncio.CancelledError:
            reply.cancel()
            raise
        except RuntimeStoreError as exc:
            frame = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': 4001, 'message': exc.reason,
                     'data': {'reason': exc.reason, **({'detail': exc.detail} if getattr(exc, 'detail', None) else {})}}}
        except Exception as exc:
            logger.debug('Task analysis failed', exc_info=True)
            frame = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': 5030, 'message': 'analysis_failed',
                         'data': {'reason': 'analysis_failed', 'detail': type(exc).__name__}}}
        finally:
            stopping.cancel()
            if not reply.done():
                reply.cancel()
        await connection.transport.write_async(frame)
