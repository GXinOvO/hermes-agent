"""Exercise the public attachment dispatcher with real enterprise transports."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_delivery_policy import automation_delivery_scope
from gateway import pending_final as pf
from gateway.final_delivery import attachment_calls, send_attachments
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input
from tests.gateway.test_strict_enterprise_transport import peer
from tests.gateway.test_strict_enterprise_media import platform_peer


@pytest.mark.asyncio
@pytest.mark.parametrize('name,kind', [(name, kind) for name in ['feishu', 'wecom', 'dingtalk', 'google_chat', 'teams']
                                     for kind in (['image'] if name == 'dingtalk' else ['image', 'image_file', 'document', 'voice', 'video'])])
@pytest.mark.parametrize('fault', [None, 'lost'])
async def test_public_attachment_dispatch_delivered_or_unknown_no_replay(name, kind, fault, tmp_path, monkeypatch):
    from tools import url_safety
    monkeypatch.setenv('HERMES_ALLOW_PRIVATE_URLS', 'true')
    url_safety._reset_allow_private_cache()
    final = {'feishu': '/open-apis/im/v1/messages', 'wecom': 'aibot_send_msg', 'dingtalk': '/ding',
             'google_chat': '/v1/spaces/peer/messages', 'teams': '/v3/conversations/chat/activities'}[name]
    path = tmp_path / ({'image_file': 'proof.png', 'document': 'proof.txt', 'voice': 'proof.ogg', 'video': 'proof.mp4'}.get(kind, 'proof.txt'))
    await asyncio.to_thread(path.write_bytes, b'\x89PNG\r\n\x1a\nproof' if kind == 'image_file' else b'#!AMR\nproof' if kind == 'voice' else b'proof')
    try:
        async with peer({final: fault} if fault else {}) as wire, platform_peer(name, wire) as (adapter, chat):
            if name == 'google_chat':
                adapter._user_credentials, adapter._user_chat_api = adapter._credentials, adapter._chat_api
            db = SessionDB(tmp_path / 'state.db')
            db.create_session('sid', source=name)
            epoch = begin_runtime_epoch(db, instance_id='owner')
            source = SessionSource(platform=Platform(name), chat_id=chat, chat_type='dm')
            route = f'agent:main:{name}:dm:{chat}'
            identity = 'saved-media'
            payload = {'text': 'work', 'native_text_v1': {'route': route, 'source': source.to_dict(),
                'automation': {'owner': 'sid', 'identity': identity, 'result_recovery_v1': 'consume_only'}}}
            admit_session_input(db, epoch=epoch, principal_id='automation:' + route, session_id='sid', request_id=identity, payload=payload)
            row = claim_session_input(db, epoch=epoch, session_id='sid')
            event = MessageEvent(text='work', source=source, internal=True, message_id=identity)
            authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source: adapter))
            response = f'![proof]({wire.base}/proof.png)' if kind == 'image' else ('[[audio_as_voice]]\n' if kind == 'voice' else '') + f'MEDIA:{path}'
            try:
                async with automation_delivery_scope(authority, row, event):
                    prepared = await pf.prepare(authority, row, event, route, response)
                assert prepared is not None
                finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
                    result={'result': {'final_response': response, 'messages': [], '_pending_final_v1': prepared}, 'usage': {}})
                extracted = await adapter._extract_response_content(response, event, route, is_ephemeral_response=False)
                calls = attachment_calls(adapter, extracted)
                assert [method for method, _ in calls] == [{'image': 'send_multiple_images', 'image_file': 'send_image_file',
                    'document': 'send_document', 'voice': 'send_voice', 'video': 'send_video'}[kind]], calls
                outcomes = []
                await send_attachments(adapter, event, extracted, prepared['metadata'], anything_sent=False, record_delivery=outcomes.append)
                assert len(outcomes) == 1 and outcomes[0].success is (fault is None), (outcomes, wire.events)
                part = pf.pending(db)[0]['parts']['attachments']
                assert (part['state'], part['attempts']) == ('delivered' if fault is None else 'unknown', 1)
                before = list(wire.events)
                await send_attachments(adapter, event, extracted, prepared['metadata'], anything_sent=False, record_delivery=outcomes.append)
                assert wire.events == before
                assert len([entry for entry in wire.events if entry[1] == final]) == (2 if name == 'wecom' and kind in {'image', 'voice'} and fault is None else 1)
            finally:
                db.close()
    finally:
        url_safety._reset_allow_private_cache()
