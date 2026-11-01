"""Auxiliary route receipts include the actual endpoint on sync and async paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.auxiliary_client import async_call_llm, call_llm


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_actual_endpoint_is_recorded_for_both_entry_points(async_mode):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="complete"))])
    client = SimpleNamespace(base_url="https://actual.invalid/v1")
    client.chat = SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response) if async_mode else lambda **kwargs: response,
    ))
    route = {"main_route_attempted": "true"}  # A caller may reuse its output dictionary across calls.
    config = {"auxiliary": {"compression": {"provider": "custom", "model": "same-model"}}}
    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("agent.auxiliary_client._get_cached_client", return_value=(client, "same-model")),
    ):
        kwargs = {"task": "compression", "messages": [{"role": "user", "content": "summary"}], "route_info": route}
        result = await async_call_llm(**kwargs) if async_mode else call_llm(**kwargs)
    assert result.choices[0].message.content == "complete"
    assert route["provider"] == "custom"
    assert route["model"] == "same-model"
    assert route["base_url"] == "https://actual.invalid/v1"
    assert route["wire_receipt_ids"].startswith("wire-")
