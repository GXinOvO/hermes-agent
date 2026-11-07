import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, platform, url, stop):
    from cron import delivery_queue as queue, scheduler, strict_thread_delivery as stages
    from gateway.config import GatewayConfig, Platform
    from tests.gateway import frozen_live_http_peer as native
    async with native.adapter_for(platform, type('Wire', (), {'base': url})()) as (adapter, _chat):
        adapter.config.enabled = True
        config = GatewayConfig(platforms={Platform(platform): adapter.config})
        original_transition = stages.transition
        original_claim = queue.claim_next

        def transition(execution, index, before, after, receipt=None):
            if stop == f'before:{index}:{after}':
                os._exit(88)
            value = original_transition(execution, index, before, after, receipt)
            if value and stop == f'after:{index}:{after}':
                os._exit(88)
            return value

        def claim(**kwargs):
            value = original_claim(**kwargs)
            if value:
                if stop == 'after_claim':
                    os._exit(88)
                if stop == 'mutate':
                    adapter.config.token = 'mutated-inert'
                    for key in ('_token', '_account_id', '_base_url'):
                        if hasattr(adapter, key):
                            setattr(adapter, key, 'mutated-inert')
            return value

        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False}}))
            stack.enter_context(patch('gateway.config.load_gateway_config', return_value=config))
            stack.enter_context(patch.object(stages, 'transition', transition))
            stack.enter_context(patch.object(queue, 'claim_next', claim))
            count = await asyncio.to_thread(scheduler.drain_delivery_queue,
                {Platform(platform): adapter}, asyncio.get_running_loop())
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        print(json.dumps({'processed': count, 'row': row}), flush=True)


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))
