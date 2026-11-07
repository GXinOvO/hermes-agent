from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input, settle_session_input


class PeerAdapter(BasePlatformAdapter):
    def __init__(self, url, session):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.url = url
        self.session = session

    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {}

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        async with self.session.post(self.url, json={'text': content}) as response:
            return SendResult(success=response.status == 200, message_id='ack')


def admission(db):
    db.create_session('sid', source='slack')
    epoch = begin_runtime_epoch(db, instance_id='queue-fixture')
    route = 'agent:main:slack:dm:chat'
    identity = 'completed-process-result'
    payload = {'text':'completed result', 'native_text_v1':{'route':route,
        'automation':{'owner':'sid', 'identity':identity, 'result_recovery_v1':'consume_only'}}}
    admit_session_input(db, epoch=epoch, principal_id='automation:' + route,
        session_id='sid', request_id=identity, payload=payload)
    return epoch, route, claim_session_input(db, epoch=epoch, session_id='sid')


@pytest.mark.asyncio
async def test_strict_queued_response_waits_for_settlement_without_dequeuing(tmp_path):
    posts = []

    async def receive(request):
        posts.append(await request.json())
        return web.json_response({'ok':True})

    app = web.Application()
    app.router.add_post('/send', receive)
    peer = web.AppRunner(app)
    await peer.setup()
    site = web.TCPSite(peer, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/send'
    try:
        with SessionDB(tmp_path / 'state.db') as db:
            epoch, route, row = admission(db)
            source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
            event = MessageEvent(text='completed result', source=source, message_id=row['request_id'], internal=True)
            followup = MessageEvent(text='next input', source=source, message_id='next')
            runner = object.__new__(GatewayRunner)
            runner._draining = False
            runner._promote_queued_event = lambda key, adapter, pending:pending
            runner._pending_event_audio_paths = lambda event:[]
            runner._merge_leftover_steer = lambda result, adapter, source, key, event, text:(event, text)
            async with aiohttp.ClientSession() as session:
                adapter = PeerAdapter(url, session)
                adapter._pending_messages[route] = followup
                result = {'final_response':'sealed final', 'messages':[]}
                async with automation_delivery_scope(SimpleNamespace(db=db), row, event):
                    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, source, route)
                    if pending_event or pending:
                        await runner._deliver_queued_first_response('sealed final', source=source, adapter=adapter,
                            session_key=route, inbound_message_id=row['request_id'])
                    assert posts == [], 'a queued response escaped before its admission was settled'
                    assert (pending_event, pending) == (None, None)
                    assert adapter._pending_messages.get(route) is followup
                settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                    outcome='completed', result={'result':result, 'usage':{}})
                for _ in range(2):
                    sent, _ = await adapter.send_final_ledgered(event, route, 'sealed final', {}, reply_to=None)
                    assert sent.success
                assert posts == [{'text':'sealed final'}]
                assert adapter.get_pending_message(route) is followup
    finally:
        await peer.cleanup()


@pytest.mark.asyncio
async def test_ordinary_human_queue_still_dequeues_next_input():
    source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
    event = MessageEvent(text='next input', source=source)
    adapter = SimpleNamespace(get_pending_message=lambda key:event)
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._promote_queued_event = lambda key, adapter, pending:pending
    runner._pending_event_audio_paths = lambda event:[]
    runner._merge_leftover_steer = lambda result, adapter, source, key, event, text:(event, text)
    assert await runner._run_agent_drain_pending({'final_response':'human final'}, adapter, source, 'route') == (event, 'next input')
