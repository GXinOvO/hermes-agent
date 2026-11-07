import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def consume(home, slack_url, bot_url, stop):
    from cron import delivery_queue as queue, scheduler, strict_thread_delivery as stages
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionStore
    from hermes_cli import gateway_runtime
    from plugins.platforms.slack.adapter import SlackAdapter
    from slack_sdk.web.async_client import AsyncWebClient
    pconfig = PlatformConfig(enabled=True, token='inert-bot-fanout')
    config = GatewayConfig(platforms={Platform.SLACK: pconfig})
    adapter = SlackAdapter(pconfig)
    client = AsyncWebClient(token=pconfig.token, base_url=slack_url + '/', proxy='', timeout=10)
    adapter._app = SimpleNamespace(client=client)
    adapter._team_clients = {'TLOCAL': client}
    adapter._channel_team.update({'CLOCAL': 'TLOCAL', 'C12345678': 'TLOCAL'})
    adapter._session_store = SessionStore(Path(home) / 'sessions', config)
    endpoint = gateway_runtime.GatewayEndpoint(str(home), 'fixture', 1, 1, bot_url, 'none',
                                             frozenset({'session-authority-v1'}))
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
        result = original_claim(**kwargs)
        if result:
            if stop == 'after_claim':
                os._exit(88)
            def no_discovery(*args, **kwargs):
                raise AssertionError('no discovery inside frozen send')
            gateway_runtime.discover_gateway_endpoint = no_discovery
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False}}))
        stack.enter_context(patch('gateway.config.load_gateway_config', return_value=config))
        stack.enter_context(patch.object(gateway_runtime, 'discover_gateway_endpoint',
                                 return_value=gateway_runtime.GatewayDiscovery('ready', endpoint)))
        stack.enter_context(patch('hermes_cli.gateway_client._session_ticket', return_value='inert-bot-ticket'))
        stack.enter_context(patch.object(stages, 'transition', transition))
        stack.enter_context(patch.object(queue, 'claim_next', claim))
        count = await asyncio.to_thread(scheduler.drain_delivery_queue, {Platform.SLACK: adapter}, asyncio.get_running_loop())
    with queue._transaction() as conn:
        row = dict(conn.execute('SELECT * FROM deliveries').fetchone())
    print(json.dumps({'pid': os.getpid(), 'processed': count, 'row': row}), flush=True)


if __name__ == '__main__':
    asyncio.run(consume(*sys.argv[1:]))
