import copy
import threading
import time
import uuid
from typing import Optional


def _sanitize_event_payload(payload):
    allowed = {
        "api_request_id", "api_call_count", "retry_count", "model", "provider",
        "requested_model", "requested_provider", "requested_api_mode", "api_mode",
        "reasoning_effort", "requested_reasoning", "prepared_reasoning", "wire_model", "wire_reasoning",
        "wire_observation", "wire_model_observation", "fallback_active", "fallback_route", "response_model", "upstream_provider",
        "response_model_observation", "upstream_provider_observation",
    }
    from agent.provider_request_audit import (
        _safe_api_mode_identity, _safe_fallback_route, _safe_model_identity,
        _safe_provider_identity, _safe_reasoning_value, sanitize_reasoning_metadata)
    values = {key: value for key, value in payload.items() if key in allowed}
    for key in ("api_call_count", "retry_count"):
        if key in values and (type(values[key]) is not int or values[key] < 0):
            values.pop(key)
    for key, choices in (
        ("wire_observation", {"sdk_call_kwargs", "codex_app_server_turn"}),
        ("wire_model_observation", {"observed", "withheld", "absent"}),
        ("response_model_observation", {"observed", "withheld", "absent"}),
        ("upstream_provider_observation", {"observed", "withheld", "absent"}),
    ):
        if key in values and (not isinstance(values[key], str) or values[key] not in choices):
            values[key] = None
    if "fallback_active" in values and type(values["fallback_active"]) is not bool:
        values["fallback_active"] = None
    if "reasoning_effort" in values:
        values["reasoning_effort"] = _safe_reasoning_value(values["reasoning_effort"], key="reasoning_effort")
    for key in ("model", "requested_model", "response_model", "wire_model"):
        if key in values:
            values[key] = _safe_model_identity(values[key])
    for key in ("provider", "requested_provider", "upstream_provider"):
        if key in values:
            values[key] = _safe_provider_identity(values[key])
    if "fallback_route" in values:
        values["fallback_route"] = _safe_fallback_route(values["fallback_route"])
    for key in ("requested_api_mode", "api_mode"):
        if key in values:
            values[key] = _safe_api_mode_identity(values[key])
    for key in ("requested_reasoning", "prepared_reasoning", "wire_reasoning"):
        if key in values:
            values[key] = sanitize_reasoning_metadata(values[key])
    return values


class FireAudit:
    """One usage_audit.jsonl line per fire with provider-attempt metadata."""

    def __init__(self, job: dict, job_id: str, model: str, *, requested_provider: str = "",
                 requested_api_mode: str = "", requested_model: str = "", execution_id: str = "",
                 pre_agent_fallback: bool = False):
        from agent.provider_request_audit import (
            _safe_api_mode_identity, _safe_model_identity, _safe_provider_identity)
        self.job, self.job_id, self.model = job, job_id, model
        self.requested_model = _safe_model_identity(requested_model or model)
        self.requested_provider = _safe_provider_identity(requested_provider)
        self.requested_api_mode = _safe_api_mode_identity(requested_api_mode)
        self.execution_id = execution_id or None
        self.pre_agent_fallback = pre_agent_fallback
        self.fire_id = uuid.uuid4().hex
        self.t_start = time.monotonic()
        self._request_attempts: list[dict] = []
        self._request_lock = threading.Lock()

    def record_request_event(self, event: str, payload: dict) -> None:
        """Merge a provider request/response event without retaining request content."""
        if event not in {"request", "sdk_request", "response"} or not isinstance(payload, dict):
            return
        from agent.provider_request_audit import _safe_route_identifier, _safe_model_identity, _safe_provider_identity
        request_id = _safe_route_identifier(payload.get("api_request_id"), max_length=256)
        if not request_id:
            return
        values = _sanitize_event_payload(payload)
        values["api_request_id"] = request_id
        with self._request_lock:
            if event == "request":
                if not self.pre_agent_fallback:
                    if not self.requested_model:
                        self.requested_model = str(values.get("requested_model") or values.get("model") or "")
                    if not self.requested_provider:
                        self.requested_provider = str(values.get("requested_provider") or values.get("provider") or "")
                    if not self.requested_api_mode:
                        self.requested_api_mode = str(values.get("requested_api_mode") or values.get("api_mode") or "")
                values["model"] = _safe_model_identity(values.get("model"))
                values["provider"] = _safe_provider_identity(values.get("provider"))
                values["requested_model"] = self.requested_model
                values["requested_provider"] = self.requested_provider
                values["requested_api_mode"] = self.requested_api_mode
                if self.pre_agent_fallback:
                    values["fallback_active"] = True
                self._request_attempts.append(values)
                return
            if event == "sdk_request":
                for row in reversed(self._request_attempts):
                    if (row.get("api_request_id") == request_id
                            and row.get("api_call_count") == values.get("api_call_count")
                            and row.get("retry_count") == values.get("retry_count")):
                        sdk = {k: values[k] for k in ("wire_model", "wire_model_observation", "wire_reasoning", "wire_observation") if k in values}
                        row.setdefault("sdk_attempts", []).append(sdk)
                        row.update(sdk)
                        return
                self._request_attempts.append(values)
                return
            candidates = [
                row for row in self._request_attempts
                if row.get("api_request_id") == request_id
                and "response_model" not in row
                and "upstream_provider" not in row
                and ("api_call_count" not in values or row.get("api_call_count") == values["api_call_count"])
                and ("retry_count" not in values or row.get("retry_count") == values["retry_count"])
            ]
            if len(candidates) == 1:
                candidates[0].update(values)
                return
            self._request_attempts.append(values)

    def on_agent_event(self, event: str, payload: dict) -> None:
        if event == "provider.request":
            self.record_request_event("request", payload)
        elif event == "provider.sdk_request":
            self.record_request_event("sdk_request", payload)
        elif event == "provider.response":
            self.record_request_event("response", payload)

    def write(self, result: dict, error: Optional[BaseException], *, worker_future=None) -> None:
        from agent.provider_request_audit import (
            _safe_api_mode_identity, _safe_delivery_target, _safe_model_identity, _safe_provider_identity,
            _safe_reasoning_value, sanitize_reasoning_metadata)
        with self._request_lock:
            attempts = copy.deepcopy(self._request_attempts)
            requested_model = self.requested_model
            requested_provider = self.requested_provider
            requested_api_mode = self.requested_api_mode
            worker_running = None if worker_future is None else not worker_future.done()
        last = attempts[-1] if attempts else {}
        from cron.scheduler import _write_usage_audit, _utcnow_iso_ms
        _write_usage_audit({
            "ts": _utcnow_iso_ms(),
            "job_id": self.job_id,
            "fire_id": self.fire_id,
            "prompt_tokens": result.get("prompt_tokens"),
            "completion_tokens": result.get("completion_tokens"),
            "total_tokens": result.get("total_tokens"),
            "response_silent": bool(result.get("response_silent")),
            "deliver_target": _safe_delivery_target(self.job.get("deliver")),
            "model": _safe_model_identity(last.get("model") or self.model),
            "provider": _safe_provider_identity(last.get("provider")),
            "requested_model": _safe_model_identity(requested_model),
            "requested_provider": _safe_provider_identity(requested_provider),
            "response_model": _safe_model_identity(last.get("response_model")),
            "upstream_provider": _safe_provider_identity(last.get("upstream_provider")),
            "requested_api_mode": _safe_api_mode_identity(requested_api_mode),
            "api_mode": _safe_api_mode_identity(last.get("api_mode")),
            "reasoning_effort": _safe_reasoning_value(last.get("reasoning_effort"), key="reasoning_effort"),
            "requested_reasoning": sanitize_reasoning_metadata(last.get("requested_reasoning")),
            "wire_model": _safe_model_identity(last.get("wire_model")),
            "wire_model_observation": last.get("wire_model_observation"),
            "wire_reasoning": sanitize_reasoning_metadata(last.get("wire_reasoning")),
            "wire_observation": last.get("wire_observation"),
            "fallback_active": (True if self.pre_agent_fallback else
                                any(bool(item.get("fallback_active")) for item in attempts) if attempts else None),
            "fallback_route": last.get("fallback_route"),
            "request_attempts": attempts,
            "worker_running_at_snapshot": worker_running,
            "execution_id": self.execution_id,
            "duration_ms": int((time.monotonic() - self.t_start) * 1000),
            "error": type(error).__name__ if error is not None else None})
