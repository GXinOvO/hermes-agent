from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import pending_final as pf
from gateway.final_delivery import send_attachments, send_text
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from tests.gateway.test_strict_sdk_preflight import connected
from tests.gateway.test_strict_sdk_delivery import peer


async def sealed(home, name, adapter, chat, *, human=False):
    db = SessionDB(home / 'state.db')
    db.create_session('sid', source=name)
    epoch = begin_runtime_epoch(db, instance_id='owner')
    source = SessionSource(platform=Platform(name), chat_id=chat, chat_type='dm')
    route = f'agent:main:{name}:dm:{chat}'
    payload = {'text':'work', 'native_text_v1':{'route':route, 'source':source.to_dict(),
        'automation':{'owner':'sid', 'identity':'whole-plan', 'result_recovery_v1':'consume_only'}}}
    if human:
        payload['native_text_v1'].pop('automation')
        payload['native_text_v1']['source']['is_bot'] = False
    admit_session_input(db, epoch=epoch, principal_id='messaging:sender' if human else 'automation:' + route, session_id='sid',
        request_id='whole-plan', payload=payload)
    row = claim_session_input(db, epoch=epoch, session_id='sid')
    event = MessageEvent(text='work', source=source, internal=True, message_id='whole-plan')
    path = home / 'proof.png'
    path.write_bytes(b'proof')
    response = 'sealed answer\nMEDIA:' + str(path)
    authority = SimpleNamespace(db=db, runner=SimpleNamespace(_adapter_for_source=lambda source:adapter))
    async with automation_delivery_scope(authority, row, event):
        descriptor = await pf.prepare(authority, row, event, route, response)
    assert set(descriptor['parts']) == {'text', 'attachments'}
    finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
        result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
    extracted = await adapter._extract_response_content(response, event, route, is_ephemeral_response=False)
    return db, event, descriptor, extracted, path


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
@pytest.mark.parametrize('fault', ['missing-file', 'unverified-media'])
async def test_entire_final_plan_refused_before_text_claim(name, fault, tmp_path, monkeypatch):
    async with peer() as wire, connected(name, wire, monkeypatch) as (adapter, chat):
        db, event, descriptor, extracted, path = await sealed(tmp_path, name, adapter, chat)
        original = adapter.send_image_file
        try:
            if fault == 'missing-file':
                path.unlink()
            else:
                monkeypatch.setattr(adapter, 'send_image_file', AsyncMock())
            result = await send_text(adapter, event, descriptor['binding']['route'], extracted.text_content,
                reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert not result.success and 'pending' in result.error
            assert wire.events == []
            assert [(p['state'], p['attempts']) for p in pf.pending(db)[0]['parts'].values()] == [('pending', 0)] * 2
            path.write_bytes(b'proof')
            monkeypatch.setattr(adapter, 'send_image_file', original)
            result = await send_text(adapter, event, descriptor['binding']['route'], extracted.text_content,
                reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert result.success
            outcomes = []
            await send_attachments(adapter, event, extracted, descriptor['metadata'],
                anything_sent=True, record_delivery=outcomes.append)
            assert len(outcomes) == 1 and outcomes[0].success
            assert {p['state'] for p in pf.pending(db)[0]['parts'].values()} == {'delivered'}
            assert len(wire.events) == (4 if name == 'slack' else 2)
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['telegram', 'discord', 'slack'])
async def test_unknown_text_ack_prevents_later_attachment_claim(name, tmp_path, monkeypatch):
    async with peer(['lost']) as wire, connected(name, wire, monkeypatch) as (adapter, chat):
        db, event, descriptor, extracted, _path = await sealed(tmp_path, name, adapter, chat)
        try:
            result = await send_text(adapter, event, descriptor['binding']['route'], extracted.text_content,
                reply_to=descriptor['reply_to'], metadata=descriptor['metadata'])
            assert not result.success
            outcomes = []
            await send_attachments(adapter, event, extracted, descriptor['metadata'],
                anything_sent=True, record_delivery=outcomes.append)
            assert len(outcomes) == 1 and not outcomes[0].success
            assert 'automation_delivery_unknown' in outcomes[0].error
            parts = pf.pending(db)[0]['parts']
            assert (parts['text']['state'], parts['text']['attempts']) == ('unknown', 1)
            assert (parts['attachments']['state'], parts['attachments']['attempts']) == ('pending', 0)
            assert len(wire.events) == 1
        finally:
            db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'attempting', 'unknown', 'delivered'])
async def test_attachment_cas_waits_for_preceding_text_ack(state, tmp_path, monkeypatch):
    from gateway.platforms.base import SendResult
    async with peer() as wire, connected('slack', wire, monkeypatch) as (adapter, chat):
        db, _event, descriptor, _extracted, _path = await sealed(tmp_path, 'slack', adapter, chat)
        try:
            if state != 'pending':
                assert pf.claim(db, descriptor['binding']['admission_id'], 'text',
                                descriptor['parts']['text']['content'])[1]
                if state != 'attempting':
                    assert pf.finish(db, descriptor['binding']['admission_id'], 'text',
                                     SendResult(success=state == 'delivered'))
            content = descriptor['parts']['attachments']['content']
            value, won, actual = pf.claim(db, descriptor['binding']['admission_id'], 'attachments', content)
            assert won is (state == 'delivered')
            assert value['parts']['attachments']['attempts'] == int(state == 'delivered')
            assert actual == ('attempting' if state == 'delivered' else 'earlier_part_' + state)
            assert wire.events == []
        finally:
            db.close()


@pytest.mark.asyncio
async def test_human_attachment_claim_and_recovery_plan_ignore_strict_order_gate(tmp_path, monkeypatch):
    async with peer() as wire, connected('slack', wire, monkeypatch) as (adapter, chat):
        db, event, descriptor, _extracted, _path = await sealed(tmp_path, 'slack', adapter, chat, human=True)
        try:
            def uncertain(conn):
                value = pf._read(conn, descriptor['binding']['admission_id'])
                value['parts']['text']['state'] = 'unknown'
                pf._put(conn, descriptor['binding']['admission_id'], value)
            db._execute_write(uncertain)
            value = pf.pending(db)[0]
            plans = await pf.prepare_delivery(adapter, value, event.source)
            assert [kind for kind, _part, _calls in plans] == ['attachments']
            assert pf.claim(db, descriptor['binding']['admission_id'], 'attachments',
                descriptor['parts']['attachments']['content'])[1:] == (True, 'attempting')
            assert wire.events == []
        finally:
            db.close()
