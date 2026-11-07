import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from gateway.config import Platform, PlatformConfig
from gateway.final_delivery import send_text
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from gateway.strict_transport import single_attempt_transport
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input


class HeldAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {}

    @single_attempt_transport
    async def send(self, chat_id, content, **kwargs):
        self.calls.append((chat_id, content))
        self.entered.set()
        await self.release.wait()
        return SendResult(success=True, message_id='inert-ack')


@pytest.mark.asyncio
@pytest.mark.parametrize('rewind', [False, True])
async def test_authenticated_rewind_cannot_turn_invalidated_final_into_delivered(tmp_path, monkeypatch, rewind):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('sid', source='slack')
        db.append_message('sid', role='user', content='original turn')
        message_id = db.get_messages('sid')[0]['id']
        epoch = begin_runtime_epoch(db, instance_id='owner')
        route = 'agent:main:slack:dm:chat'
        source = SessionSource(platform=Platform.SLACK, chat_id='chat', chat_type='dm')
        payload = {'text': 'original turn', 'native_text_v1': {'route': route, 'source': source.to_dict(),
                   'automation': {'owner': 'sid', 'identity': 'task', 'result_recovery_v1': 'consume_only'}}}
        admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
                            request_id='task', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        adapter = HeldAdapter()
        runner = SimpleNamespace(_draining=False, _adapter_for_source=lambda source: adapter,
                                 _evict_cached_agent=lambda route: None)
        authority = SessionAuthority(runner, profile_id='owned', instance_id='owner', db=db, epoch=epoch)
        authority.sessions['sid'] = LiveSession(None, route)
        event = MessageEvent(text='original turn', source=source, internal=True, message_id='task')
        async with automation_delivery_scope(authority, row, event):
            descriptor = await pf.prepare(authority, row, event, route, 'sealed final')
        finish_result(db, epoch=epoch, row=row, response='sealed final', outcome='completed',
                      result={'result': {'final_response': 'sealed final', 'messages': [],
                                         '_pending_final_v1': descriptor}, 'usage': {}})
        owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
        delivery = asyncio.create_task(send_text(adapter, event, route, 'sealed final',
            reply_to=descriptor['reply_to'], metadata=descriptor['metadata']))
        try:
            await asyncio.wait_for(adapter.entered.wait(), 15)
            part = pf.pending(db)[0]['parts']['text']
            assert part['state'] == 'attempting' and part['attempts'] == 1
            mutation = None
            if rewind:
                before = db.get_session('sid')
                mutation = await owner.dispatch({'id': 1, 'method': 'session.mutate', 'params': {
                    'session_id': 'sid', 'request_id': 'rewind-after-claim',
                    'expected_revision': before['runtime_revision'],
                    'expected_generation': before['runtime_generation'], 'operation': 'rewind',
                    'payload': {'target_message_id': message_id}}})
                assert 'result' in mutation, mutation
            with db._read_ctx() as conn:
                valid = pf._valid(conn, pf._read(conn, row['admission_id']), str(tmp_path))
            assert valid is (not rewind)
            adapter.release.set()
            result = await asyncio.wait_for(delivery, 15)
            assert result.success is (not rewind)
            part = pf.pending(db)[0]['parts']['text']
            expected = 'unknown' if rewind else 'delivered'
            assert (part['state'], part['attempts']) == (expected, 1)
            again = await send_text(adapter, event, route, 'sealed final',
                                   reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert again.success is (not rewind)
            assert adapter.calls == [('chat', 'sealed final')]
            assert pf.pending(db)[0]['parts']['text']['state'] == expected
            print('FINAL_LIFECYCLE ' + json.dumps({'rewind': rewind, 'valid_after_mutation': valid,
                'mutation': mutation, 'finish_state': expected, 'attempts': part['attempts'],
                'inert_send_calls': len(adapter.calls), 'network_posts': 0}))
        finally:
            adapter.release.set()
            if not delivery.done():
                await asyncio.wait_for(delivery, 15)
            await owner.close()
