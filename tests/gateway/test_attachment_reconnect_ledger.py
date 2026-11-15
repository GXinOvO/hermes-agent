from types import SimpleNamespace

import pytest

from gateway import pending_final as pf
from gateway.session_delivery_policy import automation_delivery_scope
from gateway.session_results import finish_result
from tests.gateway.test_attachments_follow_live_adapter import _adapter
from tests.gateway.test_pending_final import _sealed


@pytest.mark.asyncio
async def test_reconnected_human_attachment_uses_one_durable_claim(tmp_path):
    db, epoch, row, event, _, authority = await _sealed(tmp_path, human=True)
    retired, live = _adapter(transport_open=False), _adapter(transport_open=True)
    retired.gateway_runner = SimpleNamespace(_delivery_adapter_for=lambda source: live)
    authority.runner = SimpleNamespace(_adapter_for_source=lambda source: retired)
    image = tmp_path / "preview.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    response = f"Here is the preview.\nMEDIA:{image}"
    route = row["payload"]["native_text_v1"]["route"]
    try:
        async with automation_delivery_scope(authority, row, event):
            descriptor = await pf.prepare(authority, row, event, route, response)
        finish_result(db, epoch=epoch, row=row, response=response, outcome="completed",
                      result={"result": {"final_response": response, "messages": [],
                                         "_pending_final_v1": descriptor}, "usage": {}})
        extracted = await retired._extract_response_content(response, event, route,
                                                             is_ephemeral_response=False)
        results = []
        for _ in range(2):
            await retired._deliver_attachments(event, extracted, descriptor["metadata"],
                                               anything_sent=True, record_delivery=results.append)
        part = pf.pending(db)[0]["parts"]["attachments"]
        assert (part["state"], part["attempts"]) == ("delivered", 1)
        assert len(live.uploaded) == 1 and live.uploaded[0].endswith("preview.png")
        assert retired.uploaded == []
        assert len(results) == 2 and all(result.success for result in results)
    finally:
        db.close()
