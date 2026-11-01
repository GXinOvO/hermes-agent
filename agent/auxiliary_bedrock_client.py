"""Bedrock auxiliary client facade and physical Converse receipt boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class BedrockAuxiliaryClient:
    """OpenAI-client-compatible wrapper over AWS Bedrock Converse API."""

    def __init__(self, region: str, model: str):
        self._region = region
        self._model = model
        self.api_key = "aws-sdk"
        self.base_url = f"https://bedrock-runtime.{region}.amazonaws.com"
        self.chat = _ChatShim(_BedrockCompletionsAdapter(region, model, receipt_client=self))

    def close(self):
        pass


class _ChatShim:
    def __init__(self, adapter: Any):
        self.completions = adapter


class _BedrockCompletionsAdapter:
    """Translate chat completions calls into Bedrock Converse calls."""

    def __init__(self, region: str, model: str, *, logical_context: dict[str, Any] | None = None,
                 receipt_client: Any = None):
        self._region = region
        self._model = model
        self._logical_context = logical_context
        self._receipt_client = receipt_client or SimpleNamespace(
            base_url=f"https://bedrock-runtime.{region}.amazonaws.com"
        )

    def create(self, **kwargs) -> Any:
        from agent.auxiliary_client import _RELAY_AUX_CALL_CONTEXT, _notify_aux_provider_response
        from agent.auxiliary_wire_receipt import (
            bedrock_response_view, finish_wire_receipt, start_wire_receipt,
        )
        from agent.bedrock_adapter import _get_bedrock_runtime_client, call_converse

        model = kwargs.get("model", self._model)
        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        stop = kwargs.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        logical_context = _RELAY_AUX_CALL_CONTEXT.get() or self._logical_context
        runtime_client = _get_bedrock_runtime_client(self._region)

        def physical_call(request: dict[str, Any]) -> Any:
            receipt = start_wire_receipt(self._receipt_client, request, logical_context=logical_context)
            try:
                raw = runtime_client.converse(**request)
            except BaseException as exc:  # health: allow BLE001 -- physical SDK boundary must preserve cancellation and fail-open receipt
                finish_wire_receipt(receipt, error=exc)
                raise
            finish_wire_receipt(receipt, response=bedrock_response_view(raw))
            return raw

        response = call_converse(
            region=self._region, model=model, messages=kwargs.get("messages", []), tools=kwargs.get("tools"),
            max_tokens=int(max_tokens) if max_tokens else None, temperature=kwargs.get("temperature"),
            top_p=kwargs.get("top_p"), stop_sequences=stop, physical_call=physical_call,
        )
        _notify_aux_provider_response()
        return response


class AsyncBedrockAuxiliaryClient:
    """Async-compatible wrapper; the sync adapter owns the SDK boundary."""

    def __init__(self, sync_wrapper: BedrockAuxiliaryClient):
        from agent.auxiliary_client import _AsyncCompletionsAdapter, _ChatShim
        self.chat = _ChatShim(_AsyncCompletionsAdapter(sync_wrapper.chat.completions))
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url
        self._real_client = sync_wrapper
