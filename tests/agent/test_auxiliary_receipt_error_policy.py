import json
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as aux
from agent.auxiliary_wire_receipt import compression_attempt_scope


def _receipts(caplog):
    prefix = "auxiliary wire receipt: "
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


def test_stream_certificate_failure_is_sent_once_and_closes_receipt(caplog):
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    create = MagicMock(side_effect=error)
    client = SimpleNamespace(base_url="https://receipt.invalid/v1", api_key="synthetic",
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (caplog.at_level("INFO", logger="agent.auxiliary_wire"),
          aux._relay_aux_call_scope(("compression",), {}),
          compression_attempt_scope("certificate-attempt"),
          patch.object(aux, "_aux_progress_active", return_value=True),
          patch.object(aux, "_client_streams_internally", return_value=False),
          pytest.raises(Exception) as caught):
        aux._create_with_progress_once(client, {"model": "synthetic", "messages": []}, "compression")
    actual_error = getattr(getattr(caught.value, "failure", None), "error", caught.value)
    assert actual_error is error
    assert create.call_count == 1
    [receipt] = _receipts(caplog)
    assert receipt["status"] == "error"
    assert receipt["compression_attempt_id"] == "certificate-attempt"


def test_nonstream_retry_failure_does_not_inherit_negotiation_error(caplog):
    negotiation = ValueError("stream option unsupported")
    failure = RuntimeError("nonstream backend failed")
    create = MagicMock(side_effect=[negotiation, failure])
    client = SimpleNamespace(base_url="https://receipt.invalid/v1", api_key="synthetic",
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (caplog.at_level("INFO", logger="agent.auxiliary_wire"),
          aux._relay_aux_call_scope(("compression",), {}),
          compression_attempt_scope("negotiation-attempt"),
          patch.object(aux, "_aux_progress_active", return_value=True),
          patch.object(aux, "_client_streams_internally", return_value=False),
          pytest.raises(RuntimeError) as caught):
        aux._create_with_progress_once(client, {"model": "synthetic", "messages": []}, "compression")
    assert caught.value is failure
    assert failure.__context__ is None
    assert create.call_count == 2
    receipts = _receipts(caplog)
    assert [receipt["status"] for receipt in receipts] == ["error", "error"]
    assert len({receipt["auxiliary_request_id"] for receipt in receipts}) == 1
    assert [receipt["physical_attempt"] for receipt in receipts] == [1, 2]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["plain", "streamed"])
def test_account_policy_block_does_not_resend_and_keeps_provider_fallback(is_async, stream):
    import httpx

    from tests.agent import test_auxiliary_error_policy as fixtures

    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(403, json={"error": {"message": "blocked for a previous policy violation"}},
                              request=request)

    client = fixtures._wire(handle, key="synthetic", is_async=is_async)
    with (patch.object(aux, "_aux_progress_active", return_value=stream),
          patch.object(aux.aux_errors, "classify_attempt", wraps=aux.aux_errors.classify_attempt) as classify):
        outcome = fixtures.run_ladder(None, provider="openrouter", is_async=is_async, client=client)
    assert len(sent) == 1
    assert json.loads(sent[0].content).get("stream", False) == stream
    assert outcome.fallback_attempts == 1
    assert outcome.result.choices[0].message.content == "fallback"
    assert classify.call_count == 1
    assert classify.call_args.args[1] == "openrouter"
