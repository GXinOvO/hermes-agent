"""Content-free receipts at the final auxiliary provider request boundary.

The receipt is deliberately emitted only after the SDK response has been
consumed.  It joins the physical request to a compression attempt and the
logical auxiliary call without retaining prompts, response text, credentials,
or the endpoint itself.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import threading
import uuid
from typing import Any, Callable

logger = logging.getLogger("agent.auxiliary_wire")


def _safe_log(level: int, message: str, *args: Any, **kwargs: Any) -> None:
    """Never let an observer logging handler change provider control flow."""
    try:
        logger.log(level, message, *args, **kwargs)
    except BaseException:  # health: allow BLE001 S110 -- observer logging must never alter provider control flow
        pass

_COMPRESSION_ATTEMPT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "compression_attempt_id", default=None
)
_CURRENT_RECEIPT: contextvars.ContextVar[WireReceipt | None] = contextvars.ContextVar(
    "auxiliary_wire_receipt", default=None
)
_LAST_RECEIPT: contextvars.ContextVar[WireReceipt | None] = contextvars.ContextVar(
    "auxiliary_last_wire_receipt", default=None
)
_LOCAL_COUNTER: contextvars.ContextVar[int] = contextvars.ContextVar("auxiliary_wire_counter", default=0)
_RECEIPT_LOCK = threading.Lock()
_LAST_RECEIPTS_BY_ATTEMPT: dict[str, WireReceipt] = {}
_MAX_RETAINED_ATTEMPT_RECEIPTS = 256

_MAX_FIELD_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")
_REASONING_TOP_LEVEL = frozenset({
    "additionalModelRequestFields", "reasoning", "reasoning_effort", "thinking", "_reasoning_config",
})
_REASONING_NESTED = frozenset({"reasoning", "thinking", "reasoning_effort", "reasoning_config"})


@contextlib.contextmanager
def compression_attempt_scope(attempt_id: str | None):
    """Bind the durable compression attempt ID to its provider calls."""
    token = _COMPRESSION_ATTEMPT.set(str(attempt_id) if attempt_id else None)
    try:
        yield
    finally:
        _COMPRESSION_ATTEMPT.reset(token)


def current_compression_attempt_id() -> str | None:
    return _COMPRESSION_ATTEMPT.get()


def _safe_metadata(value: Any, *, max_chars: int = 128) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_chars]
    return f"<{type(value).__name__}>"


def _effective_request(request: dict[str, Any]) -> dict[str, Any]:
    """Flatten provider passthrough fields that can replace top-level wire fields."""
    effective = dict(request)
    extra = request.get("extra_body")
    if isinstance(extra, dict):
        for name in (*_MAX_FIELD_NAMES, "model", "stream"):
            if name in extra:
                effective[name] = extra[name]
    if "model" not in effective and "modelId" in request:
        effective["model"] = request["modelId"]
    inference = request.get("inferenceConfig")
    if isinstance(inference, dict) and "maxTokens" in inference:
        effective["max_tokens"] = inference["maxTokens"]
    return effective


def compression_receipt_ids(route_info: dict[str, Any] | None, logical_context: dict[str, Any] | None) -> list[str] | None:
    """Read the current call's receipt IDs without making telemetry part of the request path."""
    if isinstance(route_info, dict) and route_info.get("wire_receipt_ids"):
        return [value for value in str(route_info["wire_receipt_ids"]).split(",") if value]
    if isinstance(logical_context, dict) and isinstance(logical_context.get("wire_receipt_ids"), list):
        return [str(value) for value in logical_context["wire_receipt_ids"] if value]
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    result = getattr(value, name, default)
    return default if result is None else result


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_text_chars(value.get(key)) for key in ("text", "content", "summary", "thinking"))
    return 0


def _message_and_choice(response: Any) -> tuple[Any, Any]:
    choices = _field(response, "choices") or []
    if not choices:
        return None, None
    choice = choices[0]
    return _field(choice, "message"), choice


def native_response_view(response: Any) -> Any:
    """Convert a native Messages/Responses result to a content-free chat-shaped view."""
    if _field(response, "choices"):
        return response
    blocks = _field(response, "content") or []
    text: list[str] = []
    reasoning: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        block_text = _field(block, "text")
        if isinstance(block_text, str):
            target = reasoning if str(_field(block, "type", "")) in {"thinking", "reasoning"} else text
            target.append(block_text)
    usage = _field(response, "usage")
    usage_view = None
    if usage is not None:
        details = _field(usage, "output_tokens_details") or _field(usage, "completion_tokens_details")
        reasoning_tokens = _field(usage, "reasoning_tokens", _field(details, "reasoning_tokens"))
        usage_view = {
            "prompt_tokens": _field(usage, "input_tokens", _field(usage, "prompt_tokens")),
            "completion_tokens": _field(usage, "output_tokens", _field(usage, "completion_tokens")),
            "total_tokens": _field(usage, "total_tokens"),
        }
        if reasoning_tokens is not None:
            usage_view["completion_tokens_details"] = {"reasoning_tokens": _int_or_none(reasoning_tokens)}
    return {
        "id": _safe_metadata(_field(response, "id")),
        "model": _safe_metadata(_field(response, "model")),
        "usage": usage_view,
        "choices": [{"finish_reason": _safe_metadata(
            _field(response, "stop_reason", _field(response, "status", "stop"))
        ), "message": {"content": "".join(text), "reasoning_content": "".join(reasoning)}}],
    }


def bedrock_response_view(response: Any) -> Any:
    """Convert a raw Bedrock Converse response to the content-free receipt view."""
    output = _field(response, "output") or {}
    message = _field(output, "message") or {}
    blocks = _field(message, "content") or []
    text: list[str] = []
    reasoning: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        block_text = _field(block, "text")
        if isinstance(block_text, str):
            text.append(block_text)
        reasoning_block = _field(block, "reasoningContent")
        if isinstance(reasoning_block, dict) and isinstance(reasoning_block.get("text"), str):
            reasoning.append(reasoning_block["text"])
    usage = _field(response, "usage") or {}
    return {
        "id": _safe_metadata(_field(response, "requestId")),
        "model": _safe_metadata(_field(response, "modelId")),
        "usage": {
            "present": bool(usage),
            "input_tokens": _int_or_none(_field(usage, "inputTokens")),
            "output_tokens": _int_or_none(_field(usage, "outputTokens")),
            "total_tokens": _int_or_none(_field(usage, "totalTokens")),
            "reasoning_tokens": None,
        },
        "choices": [{
            "finish_reason": _safe_metadata(_field(response, "stopReason", "stop")),
            "message": {"content": "".join(text), "reasoning_content": "".join(reasoning)},
        }],
    }


def _usage_receipt(response: Any) -> dict[str, Any]:
    usage = _field(response, "usage")
    if usage is None:
        return {"present": False, "input_tokens": None, "output_tokens": None,
                "total_tokens": None, "reasoning_tokens": None}
    details = _field(usage, "completion_tokens_details") or _field(usage, "output_tokens_details")
    reasoning = _field(usage, "reasoning_tokens")
    if reasoning is None:
        reasoning = _field(details, "reasoning_tokens")
    return {
        "present": True,
        "input_tokens": _int_or_none(_field(usage, "prompt_tokens", _field(usage, "input_tokens"))),
        "output_tokens": _int_or_none(_field(usage, "completion_tokens", _field(usage, "output_tokens"))),
        "total_tokens": _int_or_none(_field(usage, "total_tokens")),
        "reasoning_tokens": _int_or_none(reasoning),
    }


def _finish_reason(response: Any) -> tuple[Any, str | None]:
    _message, choice = _message_and_choice(response)
    raw = _field(response, "raw_finish_reason")
    if raw is None:
        raw = _field(choice, "finish_reason")
    if raw is None:
        raw = _field(response, "finish_reason")
    safe_raw = _safe_metadata(raw)
    normalized = str(safe_raw).strip().lower() if safe_raw is not None else None
    return safe_raw, normalized


def _max_fields(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    effective = _effective_request(request)
    return {
        name: {"present": name in effective, "value": _int_or_none(effective.get(name))}
        for name in _MAX_FIELD_NAMES
    }


def _reasoning_fields(request: dict[str, Any]) -> list[str]:
    fields = [name for name in _REASONING_TOP_LEVEL if name in request]
    extra = request.get("extra_body")
    if isinstance(extra, dict):
        fields.extend(f"extra_body.{name}" for name in _REASONING_NESTED if name in extra)
    return sorted(fields)


def _route(client: Any, request: dict[str, Any], logical_context: dict[str, Any] | None) -> dict[str, Any]:
    context = logical_context or {}
    endpoint = str(getattr(client, "base_url", "") or "")
    effective = _effective_request(request)
    api_mode = str(context.get("physical_api_mode") or context.get("api_mode") or "")
    if not api_mode or api_mode == "chat_completions":
        native_client = getattr(client, "_real_client", client)
        if hasattr(native_client, "responses") or "instructions" in request or "input" in request:
            api_mode = "codex_responses"
        elif hasattr(native_client, "messages") or "thinking" in request:
            api_mode = "anthropic_messages"
        elif "modelId" in request or "bedrock-runtime" in endpoint:
            api_mode = "bedrock_converse"
    if not api_mode:
        api_mode = "chat_completions"
    return {
        "provider": _safe_metadata(
            context.get("physical_provider") or context.get("provider")
            or getattr(client, "_hermes_aux_effective_provider", "") or ""
        ),
        "model": _safe_metadata(effective.get("model") or context.get("physical_model") or context.get("model") or ""),
        "api_mode": api_mode,
        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest() if endpoint else None,
    }


class WireReceipt:
    """One physical SDK request, kept mutable until its stream is consumed."""

    def __init__(self, client: Any, request: dict[str, Any], logical_context: dict[str, Any] | None) -> None:
        context = logical_context or {}
        if logical_context is not None:
            count = int(context.get("physical_attempt_count") or 0) + 1
            context["physical_attempt_count"] = count
        else:
            count = _LOCAL_COUNTER.get() + 1
            _LOCAL_COUNTER.set(count)
        self.wire_receipt_id = f"wire-{uuid.uuid4().hex}"
        self.logical_context = logical_context
        self.record: dict[str, Any] = {
            "event": "auxiliary_wire_receipt",
            "wire_receipt_id": self.wire_receipt_id,
            "compression_attempt_id": current_compression_attempt_id(),
            "auxiliary_request_id": context.get("request_id"),
            "physical_attempt": count,
            "route": _route(client, request, logical_context),
            "max_fields": _max_fields(request),
            "reasoning_fields": _reasoning_fields(request),
                "request_stream": bool(_effective_request(request).get("stream")),
        }
        self._token = _CURRENT_RECEIPT.set(self)
        self._finished = False

    def finish(self, response: Any = None, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            _LAST_RECEIPT.set(self)
            attempt_id = self.record.get("compression_attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                with _RECEIPT_LOCK:
                    _LAST_RECEIPTS_BY_ATTEMPT[attempt_id] = self
                    while len(_LAST_RECEIPTS_BY_ATTEMPT) > _MAX_RETAINED_ATTEMPT_RECEIPTS:
                        _LAST_RECEIPTS_BY_ATTEMPT.pop(next(iter(_LAST_RECEIPTS_BY_ATTEMPT)))
            if self.logical_context is not None:
                self.logical_context.setdefault("wire_receipt_ids", []).append(self.wire_receipt_id)
                route_info = self.logical_context.get("route_info")
                receipt_ids = self.logical_context.get("wire_receipt_ids")
                if isinstance(route_info, dict) and isinstance(receipt_ids, list):
                    route_info["wire_receipt_ids"] = ",".join(str(value) for value in receipt_ids if value)
            if error is not None:
                self.record["status"] = "error"
                self.record["error"] = {"type": type(error).__name__, "status_code": _int_or_none(_field(error, "status_code"))}
            else:
                message, _choice = _message_and_choice(response)
                raw_finish, normalized_finish = _finish_reason(response)
                self.record["status"] = "returned"
                self.record["response"] = {
                    "id": _safe_metadata(_field(response, "id")),
                    "model": _safe_metadata(_field(response, "model")),
                    "finish_reason_raw": raw_finish,
                    "finish_reason": normalized_finish,
                    "usage": _usage_receipt(response),
                    "visible_content_chars": _text_chars(_field(message, "content")),
                    "reasoning_chars": _text_chars(_field(message, "reasoning"))
                    + _text_chars(_field(message, "reasoning_content"))
                    + _text_chars(_field(message, "reasoning_details")),
                    "local_reasoning_clipped": False,
                }
            _safe_log(logging.INFO, "auxiliary wire receipt: %s", json.dumps(self.record, sort_keys=True, separators=(",", ":")))
        except BaseException:  # health: allow BLE001 -- receipt serialization is advisory and fail-open
            _safe_log(logging.DEBUG, "auxiliary wire receipt finalization failed", exc_info=True)
        finally:
            try:
                _CURRENT_RECEIPT.reset(self._token)
            except (ValueError, LookupError):
                # A provider adapter may finish on a copied context; the receipt is advisory.
                pass

    def mark_local_reasoning_clipped(self) -> None:
        response = self.record.get("response")
        if not isinstance(response, dict) or response.get("local_reasoning_clipped") is True:
            return
        response["local_reasoning_clipped"] = True
        try:
            _safe_log(logging.INFO, "auxiliary wire receipt update: %s", json.dumps({
                "event": "auxiliary_wire_receipt_update",
                "wire_receipt_id": self.wire_receipt_id,
                "compression_attempt_id": self.record.get("compression_attempt_id"),
                "local_reasoning_clipped": True,
            }, sort_keys=True, separators=(",", ":")))
        except Exception:  # health: allow BLE001 -- receipt update serialization is advisory and fail-open
            _safe_log(logging.DEBUG, "auxiliary wire receipt update failed", exc_info=True)


def start_wire_receipt(
    client: Any, request: dict[str, Any], *, logical_context: dict[str, Any] | None = None,
) -> WireReceipt | None:
    try:
        return WireReceipt(client, request, logical_context)
    except BaseException:  # health: allow BLE001 -- receipt construction cannot block the provider call
        _safe_log(logging.DEBUG, "auxiliary wire receipt start failed", exc_info=True)
        return None


def mark_local_reasoning_clipped() -> None:
    attempt_id = current_compression_attempt_id()
    if not attempt_id:
        return
    receipt = _CURRENT_RECEIPT.get() or _LAST_RECEIPT.get()
    if receipt is None or receipt.record.get("compression_attempt_id") != attempt_id:
        with _RECEIPT_LOCK:
            receipt = _LAST_RECEIPTS_BY_ATTEMPT.get(attempt_id)
    if receipt is not None and receipt.record.get("compression_attempt_id") == attempt_id:
        receipt.mark_local_reasoning_clipped()


def finish_wire_receipt(receipt: WireReceipt | None, response: Any = None, error: BaseException | None = None) -> None:
    if receipt is None:
        return
    try:
        receipt.finish(response=response, error=error)
    except BaseException:  # health: allow BLE001 -- receipt finalization cannot replace the provider result
        _safe_log(logging.DEBUG, "auxiliary wire receipt finish failed", exc_info=True)


def run_native_with_wire_receipt(
    client: Any, request: dict[str, Any], call: Callable[[], Any], *,
    logical_context: dict[str, Any] | None = None,
    response_view: Callable[[Any], Any] | None = None,
) -> Any:
    """Observe a native adapter call; the adapter owns the actual provider wire."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        response = call()
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    observed = response
    if response_view is not None:
        try:
            observed = response_view(response)
        except BaseException:  # health: allow BLE001 -- observer normalization is fail-open
            observed = None
    finish_wire_receipt(receipt, response=observed if response_view else native_response_view(response))
    return response


@contextlib.contextmanager
def native_stream_with_wire_receipt(
    client: Any, request: dict[str, Any], open_stream: Callable[[], Any], *,
    logical_context: dict[str, Any] | None = None,
    response_holder: dict[str, Any] | None = None,
):
    """Observe one native stream context from open through close."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        raw_context = open_stream()
        with raw_context as stream:
            try:
                yield stream
            except BaseException as exc:
                finish_wire_receipt(receipt, error=exc)
                raise
            response = response_holder.get("response") if isinstance(response_holder, dict) else None
            get_final = getattr(stream, "get_final_message", None)
            if response is None and callable(get_final):
                try:
                    response = get_final()
                except BaseException as exc:
                    finish_wire_receipt(receipt, error=exc)
                    raise
            if response is None:
                response = getattr(stream, "response", None)
            finish_wire_receipt(receipt, response=native_response_view(response) if response is not None else None)
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise


def observe_stream_with_wire_receipt(
    client: Any, request: dict[str, Any], open_stream: Callable[[], Any], *,
    logical_context: dict[str, Any] | None = None,
) -> Any:
    """Return a transparent raw-stream iterator while finalizing one receipt on exhaustion."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        stream = open_stream()
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    if hasattr(stream, "choices"):
        finish_wire_receipt(receipt, response=stream)
        return stream

    closed = False
    finished = False

    def close_stream() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:  # health: allow BLE001 -- observer cleanup is fail-open
                _safe_log(logging.DEBUG, "auxiliary raw stream close failed", exc_info=True)

    def finish_error(error: BaseException) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        finish_wire_receipt(receipt, error=error)

    def finish_response(response: Any) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        finish_wire_receipt(receipt, response=response)

    def observed():
        response_id = ""
        response_model = str(request.get("model") or "")
        finish_reason = None
        usage = None
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            for chunk in stream:
                response_id = _field(chunk, "id") or response_id
                response_model = _field(chunk, "model") or response_model
                usage = _field(chunk, "usage") or usage
                choices = _field(chunk, "choices") or []
                choice = choices[0] if choices else None
                finish_reason = _field(choice, "finish_reason") or finish_reason
                delta = _field(choice, "delta")
                content = _field(delta, "content")
                reasoning = _field(delta, "reasoning") or _field(delta, "reasoning_content")
                if isinstance(content, str):
                    content_parts.append(content)
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
                yield chunk
            finish_response({
                "id": response_id, "model": response_model, "usage": usage,
                "choices": [{"finish_reason": finish_reason or "stop", "message": {
                    "content": "".join(content_parts), "reasoning_content": "".join(reasoning_parts),
                }}],
            })
        except BaseException as exc:
            finish_error(exc)
            raise
        finally:
            close_stream()

    generator = observed()

    class _ObservedRawStream:
        def __iter__(self):
            return self

        def __next__(self):
            return next(generator)

        def close(self) -> None:
            try:
                generator.close()
            finally:
                close_stream()
                if not finished:
                    finish_error(GeneratorExit())

    return _ObservedRawStream()


def run_with_wire_receipt(
    client: Any, request: dict[str, Any], call: Callable[[], Any], *,
    logical_context: dict[str, Any] | None = None,
    response_view: Callable[[Any], Any] | None = None,
) -> Any:
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        response = call()
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    observed = response
    if response_view is not None:
        try:
            observed = response_view(response)
        except BaseException:  # health: allow BLE001 -- observer normalization is fail-open
            observed = None
    finish_wire_receipt(receipt, response=observed)
    return response


def run_stream_with_wire_receipt(
    client: Any, request: dict[str, Any], open_stream: Callable[[], Any], consume: Callable[[Any], Any], *,
    logical_context: dict[str, Any] | None = None,
) -> Any:
    """Open and consume one stream, finishing the receipt after the final aggregate exists."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        stream = open_stream()
        if hasattr(stream, "choices"):
            response = stream
        else:
            response = consume(stream)
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    finish_wire_receipt(receipt, response=response)
    return response


async def run_async_with_wire_receipt(
    client: Any, request: dict[str, Any], call: Callable[[], Any], *,
    logical_context: dict[str, Any] | None = None,
) -> Any:
    """Async counterpart for a non-streaming SDK call."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        response = await call()
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    finish_wire_receipt(receipt, response=response)
    return response


async def run_async_stream_with_wire_receipt(
    client: Any, request: dict[str, Any], open_stream: Callable[[], Any], consume: Callable[[Any], Any], *,
    logical_context: dict[str, Any] | None = None,
) -> Any:
    """Async counterpart for opening and consuming one stream."""
    receipt = start_wire_receipt(client, request, logical_context=logical_context)
    try:
        stream = await open_stream()
        if hasattr(stream, "choices"):
            response = stream
        else:
            response = await consume(stream)
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    finish_wire_receipt(receipt, response=response)
    return response
