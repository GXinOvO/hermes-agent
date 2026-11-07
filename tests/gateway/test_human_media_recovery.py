import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from tests.gateway.test_pending_final import _sealed, Adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', [True, False, 'cancelled'])
async def test_human_media_claims_before_upload_and_never_replays_an_attempt(tmp_path, monkeypatch, ack):
    from gateway import pending_final as pf
    db, epoch, row, event, _, authority = await _sealed(tmp_path, human=True)
    path = tmp_path / 'sealed.txt'
    path.write_text('saved attachment')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    writes = []

    class HumanAdapter(Adapter):
        async def send_document(self, chat_id, file_path, **kwargs):
            part = pf.pending(db)[0]['parts']['attachments']
            assert part['state'] == 'attempting' and part['attempts'] == 1
            writes.append((chat_id, path.read_text()))
            if ack == 'cancelled':
                raise asyncio.CancelledError
            return SendResult(success=ack, error=None if ack else 'lost ACK')

    adapter = HumanAdapter()
    adapter._notify_media_delivery_failure = AsyncMock()
    authority.runner._adapter_for_source = lambda source:adapter
    response = 'MEDIA:' + str(path)
    outcomes = []
    try:
        async with automation_delivery_scope(authority, row, event):
            descriptor = await pf.prepare(authority, row, event, 'agent:main:slack:dm:chat', response)
        finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
            result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        assert pf.pending(db)[0]['parts']['attachments']['state'] == 'pending'
        extracted = await adapter._extract_response_content(response, event, descriptor['binding']['route'], is_ephemeral_response=False)
        if ack == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                await adapter._deliver_attachments(event, extracted, descriptor['metadata'], anything_sent=False, record_delivery=outcomes.append)
        else:
            await adapter._deliver_attachments(event, extracted, descriptor['metadata'], anything_sent=False, record_delivery=outcomes.append)
        await adapter._deliver_attachments(event, extracted, descriptor['metadata'], anything_sent=False, record_delivery=outcomes.append)
        assert writes == [('chat', 'saved attachment')]
        part = pf.pending(db)[0]['parts']['attachments']
        assert part['state'] == ('delivered' if ack is True else 'unknown') and part['attempts'] == 1
        assert bool(outcomes[0].success) is (ack is True)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_changed_human_attachment_does_not_spend_a_pending_claim(tmp_path, monkeypatch):
    from gateway import pending_final as pf
    db, epoch, row, event, _, authority = await _sealed(tmp_path, human=True)
    path = tmp_path / 'sealed.txt'
    path.write_text('original')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    adapter = Adapter()
    adapter.send_document = AsyncMock(return_value=SendResult(success=True))
    authority.runner._adapter_for_source = lambda source:adapter
    response = 'MEDIA:' + str(path)
    try:
        async with automation_delivery_scope(authority, row, event):
            descriptor = await pf.prepare(authority, row, event, 'agent:main:slack:dm:chat', response)
        finish_result(db, epoch=epoch, row=row, response=response, outcome='completed',
            result={'result':{'final_response':response, 'messages':[], '_pending_final_v1':descriptor}, 'usage':{}})
        path.write_text('changed')
        extracted = await adapter._extract_response_content(response, event, descriptor['binding']['route'], is_ephemeral_response=False)
        outcomes = []
        await adapter._deliver_attachments(event, extracted, descriptor['metadata'], anything_sent=False, record_delivery=outcomes.append)
        adapter.send_document.assert_not_awaited()
        part = pf.pending(db)[0]['parts']['attachments']
        assert part['state'] == 'pending' and part['attempts'] == 0
        assert not outcomes[0].success
    finally:
        db.close()
