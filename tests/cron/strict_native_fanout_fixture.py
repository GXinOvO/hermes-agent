import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, platform, url, stop):
    import pytest
    from cron import delivery_queue as queue, scheduler, strict_thread_delivery as stages
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore
    from tests.gateway import strict_native_thread_support as support
    monkey = pytest.MonkeyPatch()
    try:
        async with support.connected(platform, SimpleNamespace(base=url), monkey) as (adapter, _fixture):
            adapter._session_store = SessionStore(Path(home) / 'sessions', GatewayConfig())
            config = GatewayConfig(platforms={Platform(platform): adapter.config})
            original_transition = stages.transition
            original_claim = queue.claim_next

            def transition(execution, index, before, after, receipt=None):
                if stop == f'before:{index}:{after}':
                    os._exit(88)
                result = original_transition(execution, index, before, after, receipt)
                if result and stop == f'after:{index}:{after}':
                    os._exit(88)
                return result

            def claim(**kwargs):
                value = original_claim(**kwargs)
                settings = Path(home) / 'native-settings.json'
                if value is not None and settings.exists():
                    media = json.loads(settings.read_text()).get('mutate_after_claim')
                    if media:
                        Path(media).write_bytes(b'different bytes after claim')
                if value is not None and stop == 'after_claim':
                    os._exit(88)
                return value

            with ExitStack() as stack:
                stack.enter_context(patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False}}))
                stack.enter_context(patch('gateway.config.load_gateway_config', return_value=config))
                stack.enter_context(patch.object(stages, 'transition', transition))
                stack.enter_context(patch.object(queue, 'claim_next', claim))
                count = await asyncio.to_thread(scheduler.drain_delivery_queue,
                    {Platform(platform): adapter}, asyncio.get_running_loop())
            adapter._session_store._ensure_loaded()
            inbound = None
            if stop.startswith('inbound:'):
                from tests.cron.strict_native_inbound_support import check
                inbound = await check(adapter, config, platform, stop.split(':', 1)[1])
            with queue._transaction() as conn:
                row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
            print(json.dumps({'processed': count, 'row': row,
                'sessions': {key: {'id': entry.session_id, 'transcript': adapter._session_store.load_transcript(entry.session_id)}
                             for key, entry in adapter._session_store._entries.items()},
                'tracker': list(getattr(getattr(adapter, '_threads', None), '_threads', {})), 'inbound': inbound}), flush=True)
    finally:
        monkey.undo()


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))
