"""Standalone sidecar sealing precedes events and resumes by stored identity."""
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace


import pytest

from hermes_state import SessionDB
from hermes_state_background import results
from tui_gateway import server


@pytest.fixture
def profile_session(tmp_path, monkeypatch):
    launch, secondary = tmp_path / "launch", tmp_path / "secondary"
    launch.mkdir()
    secondary.mkdir()
    (secondary / "config.yaml").write_text("terminal:\n  backend: local\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_db", None)
    monkeypatch.setattr(server, "_db_error", None)
    db = SessionDB(secondary / "state.db")
    db.create_session("stored-parent", source="desktop")
    db.create_session("foreign", source="desktop")
    agent = SimpleNamespace(_session_db=db, session_id="stored-parent", model="test", provider="test")
    session = {"agent": agent, "session_key": "stale-key", "profile_home": str(secondary),
               "cwd": str(tmp_path), "history": [], "history_lock": threading.RLock(), "running": False}
    try:
        yield db, session, launch
    finally:
        db.close()
        from hermes_state_registry import close_all
        close_all()


def _join(task_id):
    for thread in list(threading.enumerate()):
        if thread.name == "side-agent-" + task_id:
            thread.join(timeout=10)
            assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["background.complete", "btw.complete", "preview.restart.complete"])
def test_sidecar_seals_before_emit_and_cold_resume_reads_same_result(profile_session, monkeypatch, kind):
    db, session, launch = profile_session
    task_id = "standalone-task"
    events, calls = [], []
    def body():
        from hermes_constants import get_hermes_home
        assert get_hermes_home().resolve() == Path(db.db_path).parent.resolve()
        calls.append("execute")
        return "sealed standalone answer"

    def emit(event, live_sid, payload):
        events.append((event, live_sid, payload, results(db, "stored-parent")))

    monkeypatch.setattr(server, "_emit", emit)
    history = deepcopy(session["history"])
    response = server._spawn_side_agent("req", session, task_id, "live-ui", kind, body)
    assert response["result"]["task_id"] == task_id
    _join(task_id)
    assert len(events) == 1
    saved = {"event_type": kind, "payload": {"task_id": task_id, "text": "sealed standalone answer"}}
    assert events[0] == (kind, "live-ui", saved["payload"], [saved])
    assert session["history"] == history
    assert db.get_messages("stored-parent") == []
    assert not (launch / "state.db").exists()
    path = db.db_path
    db.close()
    reopened = SessionDB(path)
    sid = None
    try:
        monkeypatch.setattr(server, "_get_db", lambda: reopened)
        monkeypatch.setattr(server, "_profile_home", lambda _p: Path(path).parent)
        monkeypatch.setattr(server, "_profile_configured_cwd", lambda _: session["cwd"])
        monkeypatch.setattr(server, "_default_session_cwd", lambda: session["cwd"])
        monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
        monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *a, **kw: None)
        monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
        monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
        monkeypatch.setattr(server, "_fallback_session_info", lambda _s: {})
        monkeypatch.setattr(server, "_pending_approval_request_payload", lambda _k: None)
        monkeypatch.setattr(server, "_open_requests", lambda _sid: [])
        for deferred in (False, True):
            response = server.handle_request({"id": "read", "method": "session.resume", "params": {
                "session_id": "stored-parent", "profile": "test", "source": "desktop",
                "defer_history": deferred, "omit_messages": True}})
            assert "error" not in response, response
            sid = response["result"]["session_id"]
            assert response["result"]["background_results"] == [saved]
            assert response["result"]["messages"] == []
            live = server._sessions[sid]
            assert server._live_session_payload(sid, live, omit_messages=True)["background_results"] == [saved]
            server._sessions.pop(sid, None)
            sid = None
        assert results(reopened, "foreign") == []
        assert calls == ["execute"]
        assert reopened.get_messages("stored-parent") == []
    finally:
        if sid:
            server._sessions.pop(sid, None)
        reopened.close()


@pytest.mark.parametrize("existing", ["running", "completed", "unknown", "input", "target", "profile"])
def test_sidecar_identity_conflict_never_starts_second_body(profile_session, monkeypatch, existing):
    db, session, _launch = profile_session
    entered, release = threading.Event(), threading.Event()
    calls = []
    def body():
        calls.append("execute")
        entered.set()
        assert release.wait(timeout=10)
        return "one answer"

    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    task_id = "same-task"
    first = server._spawn_side_agent("req", session, task_id, "live-ui", "background.complete", body,
                                     inputs={"text": "inspect"})
    assert "error" not in first, first
    assert entered.wait(timeout=10)
    try:
        if existing == "completed":
            release.set()
            _join(task_id)
        inputs = {"text": "changed" if existing == "input" else "inspect"}
        if existing == "target":
            session["agent"].session_id = "foreign"
        if existing == "profile":
            session["profile_home"] = str(Path(db.db_path).parent / "other")
        if existing == "unknown":
            with monkeypatch.context() as dead_owner:
                dead_owner.setattr("hermes_state_background._owner_live", lambda _: False)
                reply = server._spawn_side_agent("req", session, task_id, "live-ui", "background.complete", body, inputs=inputs)
        else:
            reply = server._spawn_side_agent("req", session, task_id, "live-ui", "background.complete", body, inputs=inputs)
        assert "error" in reply if existing in {"input", "target", "profile"} else "result" in reply
    finally:
        release.set()
        _join(task_id)
    assert calls == ["execute"]
