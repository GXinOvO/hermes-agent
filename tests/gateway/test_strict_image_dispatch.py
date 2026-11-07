from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.gateway.test_weixin_strict_delivery import connected, peer, posts, SEND


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_weixin_image_final_uses_verified_single_image_and_never_replays(tmp_path, monkeypatch, fault):
    from gateway import pending_final as pf
    from gateway.final_delivery import send_attachments
    from gateway.session_results import finish_result
    async with peer({SEND:[fault]} if fault else {}) as wire, connected(wire, tmp_path, monkeypatch) as adapter:
        db = SessionDB(tmp_path / 'state.db')
        db.create_session('sid', source='weixin')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        route = 'agent:main:weixin:dm:peer'
        source = SessionSource(platform=Platform.WEIXIN, chat_id='peer', chat_type='dm')
        payload = {'text':'work', 'native_text_v1':{'route':route, 'source':source.to_dict(),
            'automation':{'owner':'sid', 'identity':'saved-image', 'result_recovery_v1':'consume_only'}}}
        admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid',
            request_id='saved-image', payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='sid')
        event = MessageEvent(text='work', source=source, internal=True, message_id='saved-image')
        authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source:adapter))
        response = f'![proof]({wire.base}/image.png)'
        try:
            async with automation_delivery_scope(authority, row, event):
                prepared = await pf.prepare(authority, row, event, route, response)
            assert prepared is not None
            assert prepared['parts']['attachments']['extracted']['images']
            finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
                result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':prepared}, 'usage':{}})
            extracted = await adapter._extract_response_content(response, event, route, is_ephemeral_response=False)
            outcomes = []
            await send_attachments(adapter, event, extracted, prepared['metadata'],
                anything_sent=False, record_delivery=outcomes.append)
            assert len(outcomes) == 1 and outcomes[0].success is (fault is None)
            part = pf.pending(db)[0]['parts']['attachments']
            assert (part['state'], part['attempts']) == ('delivered' if fault is None else 'unknown', 1)
            assert len(posts(wire)) == (2 if fault is None else 1)
            before = list(wire.events)
            await send_attachments(adapter, event, extracted, prepared['metadata'],
                anything_sent=False, record_delivery=outcomes.append)
            assert wire.events == before
        finally:
            db.close()
