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
