import pytest

from gateway import pending_final as pf
from gateway.platforms.base import SendResult
from tests.gateway import test_pending_final as fixtures
from tests.gateway.test_pending_final_original_identity import settle


@pytest.mark.asyncio
async def test_recover_preserves_exact_text_sendresult_for_ack_transaction(tmp_path, monkeypatch):
    db, epoch, row, _, descriptor, authority = await fixtures._sealed(tmp_path)
    try:
        settle(db, epoch, row, descriptor)
        from gateway.session import SessionSource
        from types import SimpleNamespace
        source = SessionSource.from_dict(descriptor['binding']['source'])
        adapter = SimpleNamespace(_owner_profile=None)
        entry = SimpleNamespace(session_id=row['target_session_id'], suspended=False)
        runner = SimpleNamespace(session_authority=authority, _delivery_adapter_for=lambda s: adapter,
            session_store=SimpleNamespace(lookup_by_session_key=lambda route: entry))
        monkeypatch.setattr('gateway.session_authorities.owner_scope', lambda a: __import__('contextlib').nullcontext())
        monkeypatch.setattr('gateway.session_envelope.check_native_route', lambda *a: checked(source, descriptor['binding']['route']))
        value = pf.pending(db)[0]
        calls = [('send', {'content': 'sealed final'})]
        monkeypatch.setattr(pf, 'prepare_delivery', lambda *a: checked([('text', value['parts']['text'], calls)]))
        marker = object()
        original = SendResult(success=True, message_id='exact', raw_response={'publication_receipt_v1': marker})
        async def send(*args, **kwargs):
            return original
        seen = []
        async def finish(receipt, kind, result):
            seen.append(result)
            return result
        monkeypatch.setattr('gateway.strict_transport.strict_call', send)
        monkeypatch.setattr('gateway.pending_final_claim.current', lambda *a: True)
        monkeypatch.setattr(pf, 'finish_event', finish)
        monkeypatch.setattr(pf, 'claim', lambda *a, **kw: (value, True, 'attempting'))
        assert await pf.recover(runner) == 1
        assert seen == [original] and seen[0] is original
        assert seen[0].raw_response['publication_receipt_v1'] is marker
    finally:
        db.close()


async def checked(*values):
    return values[0] if len(values) == 1 else values
