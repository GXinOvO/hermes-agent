"""Non-sensitive provider-bound request metadata for Cron observers."""

from __future__ import annotations

from contextvars import ContextVar
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)
_request_context: ContextVar[tuple[Any, str, int, int] | None] = ContextVar(
    "cron_provider_audit_request", default=None)

_WIRE_REASONING_KEYS = frozenset({
    "reasoning_effort", "reasoning", "thinking", "thinking_config", "thinkingConfig",
    "reasoningConfig", "maxReasoningEffort", "clear_thinking", "preserve_thinking",
    "output_config",
})
_REASONING_SCALAR_KEYS = frozenset({
    "reasoning_effort", "effort", "enabled", "type", "maxReasoningEffort",
    "budget_tokens", "max_tokens", "max_output_tokens",
    "display",
})
_REASONING_ENUMS = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
    "enabled", "disabled", "adaptive", "auto", "fast", "thinking",
    "summarized", "omitted",
})


def _safe_route_identifier(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or len(value) > max_length or any(ord(char) < 32 for char in value):
        return None
    if not re.fullmatch(r"[\w][\w./:+@-]*", value, flags=re.ASCII):
        return None
    if ("://" in value or value.startswith(("/", "\\\\")) or "@" in value):
        return None
    from agent.redact import redact_sensitive_text
    return value if redact_sensitive_text(value, force=True) == value else None


def _safe_model_identity(model: Any, agent: Any = None) -> str | None:
    value = _safe_route_identifier(model, max_length=256)
    if value is None:
        return None
    if agent is not None and value != getattr(agent, "model", None):
        from hermes_cli.observability.shared_metrics_catalog import public_model_ids
        if value.lower() not in public_model_ids():
            return None
    return value


def _safe_provider_identity(provider: Any) -> str | None:
    return _safe_route_identifier(provider, max_length=128)


def _safe_api_mode_identity(api_mode: Any) -> str | None:
    return _safe_route_identifier(api_mode, max_length=64)


def _safe_delivery_target(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        return None
    lower = value.lower()
    if "://" in value or "@" in value or lower.startswith("webhook:"):
        return None
    if any(token in lower for token in ("token=", "secret=", "password=", "api_key=", "api-key=", "auth=")):
        return None
    from agent.redact import redact_sensitive_text
    return value if redact_sensitive_text(value, force=True) == value else None


def _safe_fallback_route(value: Any) -> dict[str, str | None] | None:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return {
            "model": _safe_model_identity(value[0]),
            "provider": _safe_provider_identity(value[1]),
        }
    if isinstance(value, dict):
        return {
            "model": _safe_model_identity(value.get("model")),
            "provider": _safe_provider_identity(value.get("provider")),
        }
    return None


def _safe_reasoning_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 3:
        return "<depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value if key in _REASONING_SCALAR_KEYS else {"present": value is not None}
    if isinstance(value, str):
        return value if key in _REASONING_SCALAR_KEYS and value in _REASONING_ENUMS else {"present": True}
    if isinstance(value, dict):
        return {
            str(child_key): _safe_reasoning_value(item, key=str(child_key), depth=depth + 1)
            for child_key, item in value.items()
            if (str(child_key) in _REASONING_SCALAR_KEYS or str(child_key) in _WIRE_REASONING_KEYS
                or str(child_key) in {"additionalModelRequestFields", "extra_body"})
        }
    if isinstance(value, (list, tuple)):
        return {"present": True, "items": len(value)}
    return {"present": True}


def wire_reasoning(api_kwargs: Any) -> dict[str, Any]:
    """Return only reasoning-related fields from a prepared provider request."""
    if not isinstance(api_kwargs, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _WIRE_REASONING_KEYS:
        if key in api_kwargs:
            result[key] = _safe_reasoning_value(api_kwargs[key], key=key)
    extra = api_kwargs.get("extra_body")
    if isinstance(extra, dict):
        selected = {
            str(key): _safe_reasoning_value(value, key=str(key))
            for key, value in extra.items()
            if str(key) in _WIRE_REASONING_KEYS
        }
        if selected:
            result["extra_body"] = selected
    fields = api_kwargs.get("additionalModelRequestFields")
    if isinstance(fields, dict):
        selected = {
            key: _safe_reasoning_value(fields[key], key=key)
            for key in ("thinking", "reasoningConfig", "output_config") if key in fields
        }
        if selected:
            result["additionalModelRequestFields"] = selected
    return result


def bind_sdk_request(agent: Any, api_request_id: str, api_call_count: int, retry_count: int):
    if getattr(agent, "platform", None) != "cron" or not callable(getattr(agent, "event_callback", None)):
        return None
    return _request_context.set((agent, api_request_id, api_call_count, retry_count))


def reset_sdk_request(token: Any) -> None:
    if token is not None:
        _request_context.reset(token)


def emit_sdk_request(agent: Any, *, api_kwargs: Any, api_request_id: Any = None,
                     api_call_count: Any = None, retry_count: Any = None,
                     observation: str = "sdk_call_kwargs") -> None:
    context = _request_context.get()
    if api_request_id is None and context is not None and context[0] is agent:
        _, api_request_id, api_call_count, retry_count = context
    if not api_request_id:
        api_request_id = getattr(agent, "_current_api_request_id", "")
        api_call_count = getattr(agent, "_current_api_call_count", 0) if api_call_count is None else api_call_count
        retry_count = getattr(agent, "_current_retry_count", 0) if retry_count is None else retry_count
    if getattr(agent, "platform", None) != "cron" or not callable(getattr(agent, "event_callback", None)):
        return
    try:
        extra = api_kwargs.get("extra_body") if isinstance(api_kwargs, dict) else {}
        extra = extra if isinstance(extra, dict) else {}
        model = extra.get("model", api_kwargs.get("model", api_kwargs.get("modelId"))
                          if isinstance(api_kwargs, dict) else None)
        safe_model = _safe_model_identity(model, agent)
        agent.event_callback("provider.sdk_request", {
            "api_request_id": str(api_request_id or ""), "api_call_count": int(api_call_count or 0),
            "retry_count": int(retry_count or 0), "wire_model": safe_model,
            "wire_model_observation": "observed" if safe_model else "withheld" if model else "absent",
            "wire_reasoning": wire_reasoning({**(api_kwargs if isinstance(api_kwargs, dict) else {}), **extra}),
            "wire_observation": observation,
        })
    except Exception:
        logger.debug("Cron SDK request audit callback failed", exc_info=True)


def observe_sdk_request(agent: Any, api_kwargs: dict) -> None:
    emit_sdk_request(agent, api_kwargs=api_kwargs)


def requested_reasoning(agent: Any) -> dict[str, Any] | None:
    config = getattr(agent, "reasoning_config", None)
    if not isinstance(config, dict):
        return None
    return {
        key: _safe_reasoning_value(config[key], key=key)
        for key in ("enabled", "effort")
        if key in config and isinstance(config[key], (bool, str, int, float))
    }


def route_snapshot(agent: Any, *, api_request_id: Any, api_call_count: Any, retry_count: Any,
                   api_kwargs: Any) -> dict[str, Any]:
    fallback_route = _safe_fallback_route(
        getattr(agent, "_provider_fallback_route", None)
        or getattr(agent, "_cron_initial_fallback_route", None))
    config = requested_reasoning(agent)
    wire = wire_reasoning(api_kwargs)
    effort = (
        None if config is None
        else "none" if config.get("enabled") is False
        else config.get("effort")
    )
    if effort is None:
        effort = wire.get("reasoning_effort")
        if effort is None and isinstance(wire.get("extra_body"), dict):
            effort = wire["extra_body"].get("reasoning_effort")
    return {
        "api_request_id": str(api_request_id or ""),
        "api_call_count": int(api_call_count or 0),
        "retry_count": int(retry_count or 0),
        "model": _safe_model_identity(getattr(agent, "model", "")),
        "provider": _safe_provider_identity(getattr(agent, "provider", "")),
        "requested_model": _safe_model_identity(getattr(agent, "_cron_requested_model", "")
                                or getattr(agent, "model", "")),
        "requested_provider": _safe_provider_identity(getattr(agent, "_cron_requested_provider", "")
                                   or getattr(agent, "requested_provider", "")),
        "requested_api_mode": _safe_api_mode_identity(
            getattr(agent, "_cron_requested_api_mode", "")
            or getattr(agent, "_cron_initial_requested_api_mode", "")),
        "api_mode": _safe_api_mode_identity(getattr(agent, "api_mode", "")),
        "reasoning_effort": effort,
        "requested_reasoning": config,
        "prepared_reasoning": wire,
        "fallback_active": bool(getattr(agent, "_provider_fallback_active", False)
                                or getattr(agent, "_cron_initial_provider_fallback", False)),
        "fallback_route": fallback_route,
    }


def emit_external_request(agent: Any, *, api_request_id: Any, api_call_count: Any, retry_count: Any,
                          api_kwargs: Any, observation: str) -> None:
    if getattr(agent, "platform", None) != "cron" or not callable(getattr(agent, "event_callback", None)):
        return
    try:
        agent.event_callback("provider.request", route_snapshot(
            agent, api_request_id=api_request_id, api_call_count=api_call_count,
            retry_count=retry_count, api_kwargs=api_kwargs))
        from agent.provider_request_audit import emit_sdk_request
        emit_sdk_request(agent, api_kwargs=api_kwargs, api_request_id=api_request_id,
                         api_call_count=api_call_count, retry_count=retry_count,
                         observation=observation)
    except Exception:
        logger.debug("Cron external provider audit callback failed", exc_info=True)


def emit_external_response(agent: Any, *, response: Any, api_request_id: Any, api_call_count: Any,
                           retry_count: Any) -> None:
    if getattr(agent, "platform", None) != "cron" or not callable(getattr(agent, "event_callback", None)):
        return
    try:
        agent.event_callback("provider.response", response_snapshot(
            agent, response, api_request_id=api_request_id,
            api_call_count=api_call_count, retry_count=retry_count))
    except Exception:
        logger.debug("Cron external provider response audit callback failed", exc_info=True)


def sanitize_reasoning_metadata(value: Any) -> Any:
    return _safe_reasoning_value(value, key="reasoning")


def response_snapshot(agent: Any, response: Any, *, api_request_id: Any, api_call_count: Any = None,
                      retry_count: Any = None) -> dict[str, Any]:
    model = getattr(response, "_endpoint_model", getattr(response, "model", None))
    provider = getattr(response, "provider", None)
    from agent.redact import redact_sensitive_text
    from hermes_cli.providers import get_provider

    model_present = bool(model)
    provider_present = bool(provider)
    model = _safe_model_identity(model, agent)
    if (not isinstance(provider, str) or "://" in provider
            or redact_sensitive_text(provider, force=True) != provider
            or get_provider(provider, allow_network=False) is None):
        provider = None
    return {
        "api_request_id": str(api_request_id or ""),
        "api_call_count": int(api_call_count or 0),
        "retry_count": int(retry_count or 0),
        "response_model": model,
        "response_model_observation": "observed" if model else "withheld" if model_present else "absent",
        "upstream_provider": provider,
        "upstream_provider_observation": "observed" if provider else "withheld" if provider_present else "absent",
    }