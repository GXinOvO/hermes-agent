"""Cron usage audit retains non-sensitive provider-bound request evidence."""

import json
import sys
from types import SimpleNamespace
from cron.provider_audit import FireAudit


def test_fire_audit_error_does_not_persist_exception_text():
    from hermes_constants import get_hermes_home

    audit = FireAudit({}, "error-test", "model")
    audit.write({}, RuntimeError("https://example.invalid/?token=synthetic-private-marker"))
    text = (get_hermes_home() / "cron" / "usage_audit.jsonl").read_text()
    row = json.loads(text.splitlines()[-1])
    assert row["error"] == "RuntimeError"
    assert "example.invalid" not in text
    assert "synthetic-private-marker" not in text


def test_fire_audit_records_request_route_and_wire_reasoning():
    from hermes_constants import get_hermes_home

    audit = FireAudit(
        {"deliver": "local"}, "job-audit", "gpt-6.1-terra")
    audit.record_request_event("request", {
        "api_request_id": "request-1",
        "api_call_count": 1,
        "retry_count": 0,
        "model": "gpt-6.1-terra",
        "provider": "custom",
        "requested_model": "gpt-6.1-terra",
        "requested_provider": "custom:cc-siorigin-codex",
        "requested_api_mode": "chat_completions",
        "api_mode": "chat_completions",
        "reasoning_effort": "max",
        "prepared_reasoning": {"reasoning_effort": "max"},
        "fallback_active": False,
    })
    audit.record_request_event("sdk_request", {
        "api_request_id": "request-1", "api_call_count": 1, "retry_count": 0,
        "wire_model": "gpt-6.1-terra", "wire_reasoning": {"reasoning_effort": "max"},
        "wire_observation": "sdk_call_kwargs",
    })
    audit.record_request_event("response", {
        "api_request_id": "request-1",
        "response_model": "gpt-6.1-terra",
        "upstream_provider": "cc-siorigin-codex",
    })
    audit.write({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, None)

    rows = [json.loads(line) for line in (get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()]
    row = rows[-1]
    assert row["requested_provider"] == "custom:cc-siorigin-codex"
    assert row["requested_model"] == "gpt-6.1-terra"
    assert row["provider"] == "custom"
    assert row["model"] == "gpt-6.1-terra"
    assert row["reasoning_effort"] == "max"
    assert row["wire_reasoning"] == {"reasoning_effort": "max"}
    assert row["fallback_active"] is False
    assert row["request_attempts"] == [{
        "api_request_id": "request-1",
        "api_call_count": 1,
        "retry_count": 0,
        "model": "gpt-6.1-terra",
        "provider": "custom",
        "requested_model": "gpt-6.1-terra",
        "requested_provider": "custom:cc-siorigin-codex",
        "requested_api_mode": "chat_completions",
        "api_mode": "chat_completions",
        "reasoning_effort": "max",
        "prepared_reasoning": {"reasoning_effort": "max"},
        "wire_model": "gpt-6.1-terra",
        "wire_reasoning": {"reasoning_effort": "max"},
        "wire_observation": "sdk_call_kwargs",
        "sdk_attempts": [{"wire_model": "gpt-6.1-terra", "wire_reasoning": {"reasoning_effort": "max"},
                          "wire_observation": "sdk_call_kwargs"}],
        "fallback_active": False,
        "response_model": "gpt-6.1-terra",
        "upstream_provider": "cc-siorigin-codex",
    }]


def test_response_event_binds_to_matching_call_and_retry():

    audit = FireAudit({}, "response-attempt", "model")
    for retry in (0, 1):
        audit.record_request_event("request", {
            "api_request_id": "same", "api_call_count": 3, "retry_count": retry,
            "model": "model", "provider": "custom", "api_mode": "chat_completions",
        })
    audit.record_request_event("response", {
        "api_request_id": "same", "api_call_count": 3, "retry_count": 0,
        "response_model": "gpt-4o", "upstream_provider": "Novita",
    })
    audit.record_request_event("response", {
        "api_request_id": "same", "api_call_count": 3, "retry_count": 1,
        "response_model": "gpt-4o", "upstream_provider": "OpenAI",
    })
    from hermes_constants import get_hermes_home
    audit.write({}, None)
    row = json.loads((get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert [a["upstream_provider"] for a in row["request_attempts"]] == ["Novita", "OpenAI"]


def test_delivery_target_with_credentials_is_withheld():
    from hermes_constants import get_hermes_home

    audit = FireAudit(
        {"deliver": "webhook:https://user:synthetic-token@example.invalid/?secret=private-marker"},
        "target-redaction", "model")
    audit.write({}, None)
    row = (get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1]
    assert "example.invalid" not in row
    assert "synthetic-token" not in row
    assert "private-marker" not in row
    assert json.loads(row)["deliver_target"] is None


    for target in (
        "webhook:token=secret", "webhook:auth=secret", "webhook:secret=secret",
    ):
        audit = FireAudit({"deliver": target}, "target-redaction", "model")
        audit.write({}, None)
    rows = (get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()
    for row in rows[-3:]:
        assert json.loads(row)["deliver_target"] is None


def test_pre_agent_fallback_does_not_replace_unknown_requested_route():
    from agent.provider_request_audit import route_snapshot
    from hermes_constants import get_hermes_home

    audit = FireAudit({}, "pre-fallback", "fallback-model", requested_model="primary-model",
                                 pre_agent_fallback=True)
    agent = SimpleNamespace(
        model="fallback-model", provider="fallback-provider", requested_provider="fallback-provider",
        api_mode="chat_completions", reasoning_config=None, _cron_requested_model="primary-model",
        _cron_requested_provider="", _cron_requested_api_mode="",
    )
    snapshot = route_snapshot(agent, api_request_id="request-1", api_call_count=1, retry_count=0,
                              api_kwargs={"model": "fallback-model"})
    audit.on_agent_event("provider.request", snapshot)
    audit.write({}, None)
    row = json.loads((get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert row["requested_provider"] is None
    assert row["requested_api_mode"] is None
    assert row["fallback_active"] is True
    assert row["request_attempts"][0]["fallback_active"] is True
    assert row["request_attempts"][0]["provider"] == "fallback-provider"


def test_response_identity_does_not_persist_urls_or_credentials():
    from agent.provider_request_audit import response_snapshot
    from hermes_constants import get_hermes_home

    audit = FireAudit({}, "identity-redaction", "safe-model")
    response = SimpleNamespace(
        model="https://user:synthetic-private-marker@example.invalid/v1",
        provider="https://example.invalid/?token=synthetic-private-marker",
    )
    audit.on_agent_event("provider.response", response_snapshot(None, response, api_request_id="request-1"))
    audit.write({}, None)
    row = (get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1]
    assert "synthetic-private-marker" not in row
    assert "example.invalid" not in row
    attempt = json.loads(row)["request_attempts"][0]
    assert attempt["response_model_observation"] == "withheld"
    assert attempt["upstream_provider_observation"] == "withheld"


def test_fire_audit_keeps_same_id_retry_attempts_and_initial_route():

    audit = FireAudit(
        {"deliver": "local"}, "job-audit", "primary-model",
        requested_provider="custom:primary", requested_api_mode="chat_completions")
    audit.record_request_event("request", {
        "api_request_id": "request-1", "retry_count": 0,
        "model": "primary-model", "provider": "custom",
        "requested_provider": "custom:primary", "api_mode": "chat_completions",
    })
    audit.record_request_event("response", {
        "api_request_id": "request-1", "response_model": "primary-model",
        "upstream_provider": "primary-upstream",
    })
    audit.record_request_event("request", {
        "api_request_id": "request-1", "retry_count": 1,
        "model": "fallback-model", "provider": "deepseek",
        "requested_provider": "deepseek", "api_mode": "chat_completions",
        "fallback_active": True,
    })
    audit.write({}, None)

    from hermes_constants import get_hermes_home
    row = json.loads((get_hermes_home() / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert row["requested_provider"] == "custom:primary"
    assert [item.get("retry_count", 0) for item in row["request_attempts"]] == [0, 1]
    assert [item["provider"] for item in row["request_attempts"]] == ["custom", "deepseek"]


def test_provider_audit_events_are_not_emitted_for_non_cron_agents(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", SimpleNamespace(
        _system_prompt_for_hooks=lambda _kwargs, _messages: "",
        _moa_reference_metrics_for_hook=lambda _agent: None,
    ))
    from agent.turn_api_request import _fire_pre_api_request_hook

    events = []
    agent = SimpleNamespace(
        event_callback=lambda name, payload: events.append((name, payload)), platform="gateway",
        model="m", provider="p", requested_provider="p", api_mode="chat_completions",
        reasoning_config=None, session_id="s", tools=[], _provider_fallback_active=False,
        _provider_fallback_route=None, max_tokens=None,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    _fire_pre_api_request_hook(
        agent, {"model": "m", "messages": []}, [], [], messages=[], original_user_message="",
        approx_tokens=0, total_chars=0, retry_count=0, api_call_count=1,
        api_request_id="request-4", api_start_time=1.0, effective_task_id="task", turn_id="turn",
    )
    assert events == []


def test_cron_agent_events_capture_provider_request_and_response(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", SimpleNamespace(
        _system_prompt_for_hooks=lambda _kwargs, _messages: "",
        _moa_reference_metrics_for_hook=lambda _agent: None,
    ))
    from agent.turn_api_request import _fire_pre_api_request_hook
    from agent.turn_response_intake import _fire_post_api_request_hook

    events = []
    agent = SimpleNamespace(
        event_callback=lambda name, payload: events.append((name, payload)),
        model="gpt-6.1-terra", provider="custom",
        _cron_requested_model="gpt-6.1-terra",
        _cron_requested_provider="custom:cc-siorigin-codex",
        _cron_requested_api_mode="chat_completions",
        requested_provider="custom:cc-siorigin-codex", api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "max"},
        _provider_fallback_active=False, _provider_fallback_route=None,
        session_id="cron-session", platform="cron", tools=[], max_tokens=None,
        _last_api_first_chunk_at=None,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    _fire_pre_api_request_hook(
        agent, {"model": "gpt-6.1-terra", "reasoning_effort": "max",
                "messages": [{"role": "user", "content": "secret body"}]}, [], [],
        messages=[], original_user_message="secret body", approx_tokens=1, total_chars=11,
        retry_count=0, api_call_count=1, api_request_id="request-2", api_start_time=1.0,
        effective_task_id="task", turn_id="turn",
    )
    _fire_post_api_request_hook(
        agent, SimpleNamespace(model="gpt-4o", provider="Novita"),
        SimpleNamespace(content="ok", tool_calls=[]), "stop", api_messages=[], api_call_count=1,
        api_duration=0.1, api_start_time=1.0, api_request_id="request-2",
        effective_task_id="task", turn_id="turn",
    )
    assert [name for name, _payload in events] == ["provider.request", "provider.response"]
    assert events[0][1]["prepared_reasoning"] == {"reasoning_effort": "max"}
    assert "secret body" not in json.dumps(events[0][1])
    assert events[1][1]["response_model"] == "gpt-4o"
    assert events[1][1]["upstream_provider"] == "Novita"


def test_route_snapshot_records_fallback_and_disabled_reasoning():
    from agent.provider_request_audit import route_snapshot

    snapshot = route_snapshot(SimpleNamespace(
        model="fallback-model", provider="fallback-provider",
        requested_provider="custom:primary", api_mode="chat_completions",
        reasoning_config={"enabled": False},
        _provider_fallback_active=True,
        _provider_fallback_route=("fallback-model", "fallback-provider"),
    ), api_request_id="request-3", api_call_count=2, retry_count=1,
        api_kwargs={"reasoning_effort": "none"})

    assert snapshot["fallback_active"] is True
    assert snapshot["fallback_route"] == {
        "model": "fallback-model", "provider": "fallback-provider"}

    assert snapshot["reasoning_effort"] == "none"
    assert snapshot["prepared_reasoning"] == {"reasoning_effort": "none"}


def test_wire_reasoning_does_not_persist_free_text():
    from agent.provider_request_audit import wire_reasoning

    result = wire_reasoning({
        "thinking": "secret internal reasoning text",
        "reasoning": ["secret list item"],
        "reasoningConfig": {"type": "enabled", "budget_tokens": 512},
    })

    serialized = json.dumps(result)
    assert "secret internal reasoning text" not in serialized
    assert "secret list item" not in serialized
    assert result["thinking"] == {"present": True}
    assert result["reasoningConfig"] == {"type": "enabled", "budget_tokens": 512}