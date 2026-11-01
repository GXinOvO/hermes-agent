"""Billing failures on hosted custom endpoints must not quarantine a distinct local endpoint."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import auxiliary_client as aux


class _PaymentError(Exception):
    status_code = 402


def test_custom_billing_failure_keeps_distinct_endpoint_eligible():
    hosted_url = "https://hosted.example/v1"
    local_url = "http://127.0.0.1:8080/v1"
    payment_error = _PaymentError("Payment Required: weekly usage limit")
    hosted_client = MagicMock(base_url=hosted_url)
    hosted_client.chat.completions.create.side_effect = payment_error
    local_client = MagicMock(base_url=local_url)
    local_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="local-ok"))],
    )
    fallback_entry = {
        "provider": "custom", "model": "local-model", "base_url": local_url, "api_key": "local",
    }
    aux._aux_unhealthy_until.clear()
    aux._aux_unhealthy_logged_at.clear()
    try:
        with (
            patch.object(aux, "_resolve_task_provider_model", return_value=(
                "custom", "hosted-model", hosted_url, "hosted", None)),
            patch.object(aux, "_get_cached_client", return_value=(hosted_client, "hosted-model")),
            patch.object(aux, "_get_auxiliary_task_config", return_value={"fallback_chain": [fallback_entry]}),
            patch.object(aux, "_resolve_fallback_entry", return_value=(local_client, "local-model")),
            # Fix this unrelated fixture axis: the regression tests billing, not context eligibility.
            patch.object(aux, "get_model_context_length", return_value=100_000),
        ):
            response = aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
        assert response.choices[0].message.content == "local-ok"
        assert aux._is_provider_unhealthy("custom", hosted_url) is True
        assert aux._is_provider_unhealthy("custom", local_url) is False
        assert hosted_client.chat.completions.create.call_count == 1
        assert local_client.chat.completions.create.call_count == 1
    finally:
        aux._aux_unhealthy_until.clear()
        aux._aux_unhealthy_logged_at.clear()
