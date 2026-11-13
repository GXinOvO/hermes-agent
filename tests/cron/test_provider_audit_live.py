import json
import os
from pathlib import Path

import pytest

from tests.fakes.fake_llm_provider import Error, FakeLLMServer, Text, write_hermes_home
from cron.provider_audit import FireAudit


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_real_agent_audit_matches_loopback_sdk_request(monkeypatch, tmp_path, stream):
    from cron import scheduler
    from run_agent import AIAgent

    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    with FakeLLMServer(aux=lambda _request: Text("loopback done")) as peer:
        write_hermes_home(home, peer.base_url, extra_config=(
            "updates:\n  check: false\nmodels_dev:\n  url: http://127.0.0.1:9/api.json\n"
            "skills:\n  creation_nudge_interval: 0\nmemory:\n  nudge_interval: 0\n"
        ))
        audit = FireAudit({}, "audit-live", "fake-model", requested_provider="custom")
        agent = AIAgent(
            api_key="loopback-only", base_url=peer.base_url, provider="custom", model="fake-model",
            platform="cron", reasoning_config={"enabled": True, "effort": "max"},
            event_callback=audit.on_agent_event, quiet_mode=True, skip_context_files=True,
            skip_memory=True, skip_background_review=True, enabled_toolsets=[], save_trajectories=False,
        )
        agent._disable_streaming = not stream
        try:
            result = agent.run_conversation("audit-loopback-private-marker", task_id="audit-test")
        finally:
            agent.close()
        assert result.get("completed") is True, result.get("error")
        audit.write(result, None)
        record = json.loads((tmp_path / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
        matching = [r["body"] for r in peer.requests if "audit-loopback-private-marker" in json.dumps(r["body"])]
        assert len(matching) == 1
        sent = matching[0]
        assert bool(sent.get("stream")) is stream
        assert len(record["request_attempts"]) == 1
        attempt = record["request_attempts"][0]
        assert attempt["wire_observation"] == "sdk_call_kwargs"
        assert attempt["wire_model"] == sent["model"]
        assert sent["reasoning_effort"] == "max"
        assert attempt["wire_reasoning"]["reasoning_effort"] == "max"
        assert "audit-loopback-private-marker" not in json.dumps(record)
        assert "loopback-only" not in json.dumps(record)


def test_real_agent_audit_records_reasoning_rejection_retry(monkeypatch, tmp_path):
    from cron import scheduler
    from run_agent import AIAgent

    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    seen = []

    def respond(request):
        if "audit-retry-private-marker" not in json.dumps(request["body"]):
            return Text("auxiliary")
        seen.append(request)
        return Error(400, "unsupported reasoning_effort") if len(seen) == 1 else Text("retry succeeded")

    with FakeLLMServer(respond, aux=respond) as peer:
        write_hermes_home(home, peer.base_url, extra_config=(
            "updates:\n  check: false\nmodels_dev:\n  url: http://127.0.0.1:9/api.json\n"
            "skills:\n  creation_nudge_interval: 0\nmemory:\n  nudge_interval: 0\n"
        ))
        audit = FireAudit({}, "audit-retry", "fake-model", requested_provider="custom")
        agent = AIAgent(
            api_key="loopback-only", base_url=peer.base_url, provider="custom", model="fake-model",
            platform="cron", reasoning_config={"enabled": True, "effort": "max"},
            event_callback=audit.on_agent_event, quiet_mode=True, skip_context_files=True,
            skip_memory=True, skip_background_review=True, enabled_toolsets=[], save_trajectories=False,
        )
        try:
            result = agent.run_conversation("audit-retry-private-marker", task_id="audit-retry-test")
        finally:
            agent.close()
        assert result.get("completed") is True, result.get("error")
        audit.write(result, None)
        record = json.loads((tmp_path / "cron" / "usage_audit.jsonl").read_text().splitlines()[-1])
        requests = [r["body"] for r in peer.requests if "audit-retry-private-marker" in json.dumps(r["body"])]
        assert len(requests) == 2
        assert requests[0]["reasoning_effort"] == "max"
        assert "reasoning_effort" not in requests[1]
        attempts = record["request_attempts"]
        assert len(attempts) == 2
        assert attempts[0]["api_request_id"] == attempts[1]["api_request_id"]
        assert [attempt["retry_count"] for attempt in attempts] == [0, 1]
        assert [len(attempt["sdk_attempts"]) for attempt in attempts] == [1, 1]
        assert "response_model" not in attempts[0]
        assert attempts[1]["response_model"] == "fake-model"
        assert "audit-retry-private-marker" not in json.dumps(record)


def test_request_reasoning_metadata_does_not_persist_untrusted_free_text(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.provider_request_audit import route_snapshot
    from cron import scheduler

    sentinel = "secret-free-text-must-not-enter-audit"
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    agent = SimpleNamespace(
        model="fake-model", provider="custom", requested_provider="custom", api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": sentinel}, _provider_fallback_active=False,
        _provider_fallback_route=None,
    )
    request = {"reasoning_effort": sentinel, "thinking": {"type": sentinel, "effort": sentinel}}
    snapshot = route_snapshot(agent, api_request_id="attempt", api_call_count=1, retry_count=0, api_kwargs=request)
    audit = FireAudit({}, "redaction", "fake-model")
    audit.on_agent_event("provider.request", snapshot)
    audit.write({}, None)
    record = (tmp_path / "cron" / "usage_audit.jsonl").read_text()
    assert sentinel not in record
