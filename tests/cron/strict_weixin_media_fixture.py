import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, url, stop):
    from cron import delivery_queue as queue, scheduler, strict_thread_delivery as stages
    from gateway.config import GatewayConfig, Platform
    from tests.gateway import frozen_live_http_peer as native
    home = Path(home)
    async with native.adapter_for('weixin', type('Wire', (), {'base': url})()) as (adapter, _chat):
        adapter.config.enabled = True
        config = GatewayConfig(platforms={Platform.WEIXIN: adapter.config})
        original_transition = stages.transition
        original_claim = queue.claim_next
        original_finish = queue._finish

        def snapshot(name, row):
            saved = row['thread_json']
            if saved is not None:
                with (home / name).open('a') as stream:
                    stream.write(json.dumps(json.loads(saved), sort_keys=True) + '\n')

        def transition(execution, index, before, after, receipt=None):
            if stop == f'before:{index}:{after}':
                os._exit(88)
            value = original_transition(execution, index, before, after, receipt)
            if value and stop == f'after:{index}:{after}':
                snapshot('stage-snapshots', queue.get_status(execution))
                os._exit(88)
            return value

        def claim(**kwargs):
            value = original_claim(**kwargs)
            if value:
                snapshot('claim-snapshots', value)
                if stop == 'after_claim':
                    os._exit(88)
                if stop == 'mutate':
                    (home / 'proof.bin').write_bytes(b'MUTATED')
                    adapter._token = adapter._account_id = adapter._base_url = adapter._cdn_base_url = 'MUTATED'
                    adapter._token_store._cache.clear()
            return value

        def finish(execution, **kwargs):
            snapshot('finish-snapshots', queue.get_status(execution))
            return original_finish(execution, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False}}))
            stack.enter_context(patch('gateway.config.load_gateway_config', return_value=config))
            stack.enter_context(patch.object(stages, 'transition', transition))
            stack.enter_context(patch.object(queue, 'claim_next', claim))
            stack.enter_context(patch.object(queue, '_finish', finish))
            count = await asyncio.to_thread(scheduler.drain_delivery_queue,
                {Platform.WEIXIN: adapter}, asyncio.get_running_loop())
        with queue._transaction() as conn:
            row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
        print(json.dumps({'pid': os.getpid(), 'processed': count, 'row': row}), flush=True)


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))
