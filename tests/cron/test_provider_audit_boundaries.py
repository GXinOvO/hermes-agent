import json
from types import SimpleNamespace

from cron import scheduler
from cron.provider_audit import FireAudit


def test_audit_tracks_post_middleware_and_relay_provider_request(monkeypatch, tmp_path):
    from agent import relay_llm, turn_api_call
    from agent.chat_completion_headers import _chat_completion_capturing_headers

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    audit = FireAudit({}, "job", "primary", requested_provider="custom:primary")
    sent = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: sent.append(kwargs) or SimpleNamespace(model=kwargs["model"])
    )))
    agent = SimpleNamespace(
        platform="cron", event_callback=audit.on_agent_event, provider="custom",
        requested_provider="custom:primary", model="primary", api_mode="chat_completions",
        base_url="https://example.invalid", session_id="session", reasoning_config={"enabled": True, "effort": "max"},
        _has_pending_redirect=lambda: False,
    )
    agent._interruptible_api_call = lambda kwargs: _chat_completion_capturing_headers(agent, client, kwargs)
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _agent: False)
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
                        lambda request, call, **kwargs: call({**request, "model": "middleware", "reasoning_effort": "high"}))
    monkeypatch.setattr(relay_llm, "execute",
                        lambda request, call, **kwargs: call({**request, "model": "gpt-4o", "reasoning_effort": "low"}))
    request = {"model": "primary", "reasoning_effort": "max", "messages": [{"role": "user", "content": "private"}]}
    from agent.turn_api_request import _fire_pre_api_request_hook
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    _fire_pre_api_request_hook(
        agent, request, request["messages"], [], messages=request["messages"],
        original_user_message="private", approx_tokens=1, total_chars=7,
        retry_count=0, api_call_count=1, api_request_id="request-1", api_start_time=1.0,
        effective_task_id="task", turn_id="turn",
    )
    verdict = turn_api_call.perform_api_call(
        agent, api_kwargs=request, _original_api_kwargs=request, _llm_middleware_trace=[],
        _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
        retry_count=0, api_call_count=1, api_request_id="request-1", effective_task_id="task",
        turn_id="turn", interrupted=False,
    )
    assert verdict.action == "fallthrough"
    assert sent[0]["model"] == "gpt-4o"
    assert sent[0]["reasoning_effort"] == "low"
    audit.write({}, None)
    row = json.loads((tmp_path / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert row["request_attempts"][0]["wire_model"] == sent[0]["model"]
    assert row["request_attempts"][0]["wire_reasoning"]["reasoning_effort"] == sent[0]["reasoning_effort"]
    assert "private" not in json.dumps(row)


def test_sdk_model_override_does_not_persist_url_or_key(monkeypatch, tmp_path):
    from agent.provider_request_audit import bind_sdk_request, observe_sdk_request, reset_sdk_request

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    audit = FireAudit({}, "redaction", "safe-model")
    agent = SimpleNamespace(platform="cron", event_callback=audit.on_agent_event, model="safe-model")
    token = bind_sdk_request(agent, "attempt", 1, 0)
    try:
        observe_sdk_request(agent, {"model": "https://example.invalid/?token=synthetic-secret-marker"})
    finally:
        reset_sdk_request(token)
    audit.write({}, None)
    row = (tmp_path / "cron" / "usage_audit.jsonl").read_text()
    assert "example.invalid" not in row
    assert "synthetic-secret-marker" not in row


def test_audit_snapshot_is_not_changed_by_a_later_sdk_event(monkeypatch):
    audit = FireAudit({}, "snapshot", "model")
    audit.record_request_event("request", {"api_request_id": "attempt", "api_call_count": 1, "retry_count": 0})
    audit.record_request_event("sdk_request", {
        "api_request_id": "attempt", "api_call_count": 1, "retry_count": 0, "wire_model": "model-1",
    })
    captured = []

    def write_after_later_event(record):
        audit.record_request_event("sdk_request", {
            "api_request_id": "attempt", "api_call_count": 1, "retry_count": 0, "wire_model": "model-2",
        })
        captured.append(record)

    monkeypatch.setattr(scheduler, "_write_usage_audit", write_after_later_event)
    audit.write({}, TimeoutError())
    assert [s["wire_model"] for s in captured[0]["request_attempts"][0]["sdk_attempts"]] == ["model-1"]


def test_timed_out_cron_audit_marks_live_worker_snapshot(monkeypatch, tmp_path):
    from concurrent.futures import Future
    from tests.cron.test_cron_pinned_job_fallback import _job, _run

    worker = Future()
    assert worker.set_running_or_notify_cancel()

    def timed_out(*args, worker_state, **kwargs):
        worker_state["future"] = worker
        raise TimeoutError("watchdog")

    monkeypatch.setattr(scheduler, "_run_agent_with_watchdog", timed_out)
    monkeypatch.setattr("cron.scheduler_detached_worker.defer_teardown_to_running_worker", lambda *a, **k: True)
    success, error, _, _ = _run(tmp_path, _job())
    assert not success and "TimeoutError" in error
    row = json.loads((tmp_path / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert row["worker_running_at_snapshot"] is True
    assert row["error"] == "TimeoutError"
    assert row["request_attempts"] == []
    worker.set_result(None)


def test_pre_agent_fallback_audit_preserves_requested_route_and_execution(tmp_path):
    from hermes_cli.auth import AuthError
    from tests.cron.test_cron_pinned_job_fallback import _job, _run

    success, error, _, _ = _run(tmp_path, _job(), primary_error=AuthError("No credentials stored"))
    assert (success, error) == (True, None)
    row = json.loads((tmp_path / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
    assert row["requested_model"] == "gpt-5.6-sol"
    assert row["requested_provider"] == "openai-codex"
    assert row["model"] == "z-ai/glm-5.2"
    assert row["fallback_active"] is True
    assert row["execution_id"] == "cron-owner-request"


def test_agent_constructor_fallback_is_not_reported_as_primary():
    from unittest.mock import MagicMock, patch

    from agent.provider_request_audit import route_snapshot
    from run_agent import AIAgent

    fallback = MagicMock(api_key="fixture", base_url="http://127.0.0.1:9/v1")

    def resolve(provider, model=None, **kwargs):
        if provider == "tencent-token-plan":
            return fallback, "kimi2.5"
        return None, None

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve), \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()):
        agent = AIAgent(
            provider="alibaba-coding-plan", model="qwen3.6-plus", api_key=None, base_url=None,
            quiet_mode=True, skip_context_files=True, skip_memory=True, platform="cron",
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        try:
            assert (agent.provider, agent.model, agent._fallback_activated) == (
                "tencent-token-plan", "kimi2.5", True)
            snapshot = route_snapshot(agent, api_request_id="init-fallback", api_call_count=1,
                                      retry_count=0, api_kwargs={"model": agent.model})
            assert snapshot["fallback_active"] is True
        finally:
            agent.close()
