"""Progress-aware auxiliary request execution with physical wire receipts.

Kept outside ``auxiliary_client.py`` so the router facade stays below its
ratcheted file-size cap. Imports from the facade are lazy because the facade
owns the provider helpers and imports this module at call time.
"""

from __future__ import annotations

from typing import Any, Optional


def create_with_progress_once(
    client: Any, kwargs: dict[str, Any], task: Optional[str] = None, *, force_stream: bool = False, provider: str = ""
) -> Any:
    """Issue one auxiliary request, aggregating streams and finalizing one wire receipt."""
    from agent.auxiliary_client import (
        _RELAY_AUX_CALL_CONTEXT,
        _STREAM_REJECTED,
        _ClassifiedAttempt,
        _aggregate_chat_stream,
        _aux_progress_active,
        _classify_failure,
        _client_streams_internally,
        _notify_aux_dispatch,
        _notify_aux_provider_response,
        _stream_request_plan,
        aux_errors,
        bypass_chat_sdk_request_transform,
    )
    from agent.auxiliary_wire_receipt import (
        finish_wire_receipt,
        run_with_wire_receipt,
        start_wire_receipt,
    )
    from agent.auxiliary_stream_watchdog import chat_stream_windows

    kwargs = bypass_chat_sdk_request_transform(kwargs, client)
    logical_context = _RELAY_AUX_CALL_CONTEXT.get()
    _notify_aux_dispatch()
    if (not _aux_progress_active() and not force_stream) or _client_streams_internally(client):
        if _client_streams_internally(client):
            response = client.chat.completions.create(**kwargs)
        else:
            response = run_with_wire_receipt(
                client, kwargs, lambda: client.chat.completions.create(**kwargs),
                logical_context=logical_context,
            )
        if not _client_streams_internally(client):
            _notify_aux_provider_response()
        return response

    stream_kwargs, model, total_ceiling = _stream_request_plan(kwargs, client)
    receipt = start_wire_receipt(client, stream_kwargs, logical_context=logical_context)
    try:
        chunks = client.chat.completions.create(**stream_kwargs)
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        if not isinstance(exc, Exception):
            raise
        if force_stream:
            raise
        failure = _classify_failure(exc, provider, client, kwargs.get("model"))
        if aux_errors.stream_negotiation_reraises(failure.verdict):
            raise _ClassifiedAttempt(failure)
        import logging
        logging.getLogger("agent.auxiliary_client").debug(
            "Auxiliary %s: streamed request failed (%s); retrying non-streaming",
            task or "call", exc,
        )
        chunks = _STREAM_REJECTED
    if chunks is _STREAM_REJECTED:
        _notify_aux_dispatch()
        response = run_with_wire_receipt(
            client, kwargs, lambda: client.chat.completions.create(**kwargs),
            logical_context=logical_context,
        )
        _notify_aux_provider_response()
        return response

    if hasattr(chunks, "choices"):
        _notify_aux_provider_response()
        finish_wire_receipt(receipt, response=chunks)
        return chunks

    try:
        response = _aggregate_chat_stream(
            chunks, model=model, total_ceiling=total_ceiling,
            no_progress=chat_stream_windows(client, kwargs, task),
        )
    except BaseException as exc:
        finish_wire_receipt(receipt, error=exc)
        raise
    finish_wire_receipt(receipt, response=response)
    return response
