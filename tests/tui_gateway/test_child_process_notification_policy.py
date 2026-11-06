import queue
import threading
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

from tools.process_registry_notifications import format_process_notification
from tui_gateway import server


@pytest.fixture
def surface(monkeypatch, tmp_path):
    starts, frames = [], []
    session = {
        "session_key": "parent-session",
        "profile_home": str(tmp_path),
        "history_lock": threading.RLock(),
        "running": False,
        "history": [],
        "agent": None,
    }
    registry = SimpleNamespace(
        completion_queue=queue.Queue(),
        is_completion_consumed=lambda _sid: False,
    )

    def submit(rid, sid, current, text, **kwargs):
        starts.append((text, kwargs))
        current["running"] = False
        return True

    monkeypatch.setattr(server, "_sessions", {"ui": session})
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: frames.append(args))
    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", lambda *args: "claim")
    monkeypatch.setattr("tools.async_delegation.complete_event_delivery", lambda *args: None)
    return session, registry, starts, frames


def event(kind="completion", **extra):
    return {
        "type": kind,
        "session_id": "proc_child",
        "origin_ui_session_id": "ui",
        "session_key": "parent-session",
        "owner_task_id": "sa-child",
        "task_id": "parent-session",
        "command": "build target",
        "exit_code": 0,
        "output": "build output",
        "pattern": "ready",
        "seq": 1,
        "elapsed": 60,
        "interval": 60,
        **extra,
    }


def route(surface, events):
    session, registry, _, _ = surface
    with server._session_profile_runtime_scope(session):
        server._notif_handle_ready(
            "ui", session, events, set(), registry,
            format_process_notification, None,
        )


@pytest.mark.parametrize("kind,exit_code", [
    ("completion", 0), ("completion", 2), ("heartbeat", 0), ("watch_match", 0),
])
def test_child_process_events_do_not_wake_parent_by_default(surface, kind, exit_code):
    session, registry, starts, frames = surface
    route(surface, [event(kind, exit_code=exit_code)])
    assert starts == []
    assert len(frames) == 1
    assert session["running"] is False
    assert registry.completion_queue.empty()


def test_legacy_child_task_id_is_filtered(surface):
    route(surface, [event(owner_task_id="", task_id="sa-child")])
    assert surface[2] == []
    assert len(surface[3]) == 1


@pytest.mark.parametrize("kind", ["completion", "heartbeat", "watch_match"])
def test_parent_process_events_still_wake_parent(surface, kind):
    route(surface, [event(kind, owner_task_id="parent-session")])
    assert len(surface[2]) == 1
    assert "build output" in surface[2][0][0]


def test_final_delegation_result_still_wakes_parent(surface):
    route(surface, [event("async_delegation", delegation_id="deleg-result", results=[])])
    assert len(surface[2]) == 1
    assert surface[2][0][1]["display_kind"] == "async_delegation_complete"


def test_explicit_child_notification_opt_in_is_preserved(surface, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "delegation": {"surface_child_process_notifications": True},
    }))
    route(surface, [event()])
    assert len(surface[2]) == 1


def test_child_filter_uses_owning_profile_configuration(surface, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "delegation": {"surface_child_process_notifications": True},
    }))
    owner_home = tmp_path / "owner-profile"
    owner_home.mkdir()
    (owner_home / "config.yaml").write_text(yaml.safe_dump({
        "delegation": {"surface_child_process_notifications": False},
    }))
    surface[0]["profile_home"] = str(owner_home)
    route(surface, [event()])
    assert surface[2] == []
    assert len(surface[3]) == 1


def test_foreign_child_notification_is_requeued_before_filtering(surface, monkeypatch):
    foreign = {"session_key": "foreign-session"}
    monkeypatch.setitem(server._sessions, "other-ui", foreign)
    notification = event(origin_ui_session_id="other-ui", session_key="foreign-session")
    route(surface, [notification])
    assert surface[1].completion_queue.get_nowait() == notification
    assert surface[2] == []
    assert surface[3] == []


def test_mixed_snapshot_preserves_parent_result_without_child_noise(surface):
    route(surface, [event(), event(session_id="proc_parent", owner_task_id="parent-session", output="parent result")])
    assert len(surface[2]) == 1
    assert "parent result" in surface[2][0][0]
    assert "proc_child" not in surface[2][0][0]


def test_live_poller_drops_child_event_before_parent_delivery(surface, monkeypatch):
    session, registry, starts, frames = surface
    registry.restore_completions = lambda: 0
    registry.completion_queue.put(event())
    registry.completion_queue.put(event(session_id="proc_parent", owner_task_id="parent-session", output="parent result"))
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr("tools.async_delegation.maybe_sweep_orphaned_completions", lambda _q: None)
    for name in ("_maybe_fire_tui_loop_tick", "_maybe_fire_tui_heartbeat_tick", "_notif_poll_kanban"):
        monkeypatch.setattr(server, name, lambda *args: None)
    stop = threading.Event()

    def submit(rid, sid, current, text, **kwargs):
        starts.append((text, kwargs))
        current["running"] = False
        stop.set()
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    worker = threading.Thread(target=server._notification_poller_loop, args=(stop, "ui", session), daemon=True)
    worker.start()
    try:
        worker.join(10)
        assert not worker.is_alive()
        assert len(starts) == 1
        assert "parent result" in starts[0][0]
        assert "proc_child" not in starts[0][0]
        assert len(frames) == 2
        assert registry.completion_queue.empty()
    finally:
        stop.set()
        worker.join(2)


def test_post_turn_keeps_child_status_without_a_model_turn(surface, monkeypatch):
    from tools.process_registry import ProcessRegistry, ProcessSession

    session, _, starts, frames = surface
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = ProcessSession(
        id="proc_child_post_turn", command="build target", started_at=1,
        session_key="parent-session", owner_task_id="sa-child", notify_on_complete=True,
    )
    registry._running[process.id] = process
    with server._session_profile_runtime_scope(session):
        registry._finish_exited(process, 2)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *args: False)

    server._run_post_turn_followups("request", "ui", session, {}, None)

    assert starts == []
    assert len(frames) == 1
    assert "Failed" in frames[0][2]["text"]
    assert session["running"] is False
    assert registry.completion_queue.empty()


@pytest.mark.parametrize("reason", ["lost", "failed_start"])
@pytest.mark.parametrize("path", ["poller", "post_turn"])
def test_child_process_infrastructure_failure_wakes_parent(surface, monkeypatch, reason, path):
    from tools.process_registry import ProcessRegistry, ProcessSession

    session, _, starts, frames = surface
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = ProcessSession(
        id="proc_child_infrastructure", command="build target", started_at=1,
        session_key="parent-session", owner_task_id="sa-child", notify_on_complete=True,
    )
    registry._running[process.id] = process
    with server._session_profile_runtime_scope(session):
        registry._finish_exited(process, None, reason, "backend_lost" if reason == "lost" else reason)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)

    if path == "post_turn":
        monkeypatch.setattr(server, "_drain_queued_prompt", lambda *args: False)
        server._run_post_turn_followups("request", "ui", session, {}, None)
    else:
        route((session, registry, starts, frames), [registry.completion_queue.get_nowait()])

    assert len(starts) == 1
    assert "proc_child_infrastructure" in starts[0][0]
    assert len(frames) == 1
    assert registry.completion_queue.empty()


@pytest.mark.parametrize("status", ["error", "timeout", "failed"])
def test_terminal_child_failure_is_delivered_before_siblings_and_final_result(surface, monkeypatch, status):
    from tools import async_delegation as ad

    session, registry, starts, _ = surface
    record = {
        "delegation_id": "deleg-failed-child", "status": "running", "is_batch": True,
        "session_key": "parent-session", "origin_ui_session_id": "ui",
        "goals": ["blocked task", "ongoing sibling"], "goal": "blocked task",
        "dispatched_at": 1,
    }
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    entry = {"task_index": 0, "status": status, "error": "terminal child failure"}
    with server._session_profile_runtime_scope(session):
        ad.push_task_failure_notice(record["delegation_id"], entry, n_tasks=2)
    notice = registry.completion_queue.get_nowait()
    route(surface, [notice])
    assert len(starts) == 1
    assert "terminal child failure" in starts[0][0]
    assert ad._records[record["delegation_id"]]["status"] == "running"
    assert notice["task_failure_notice"] is True

    with server._session_profile_runtime_scope(session):
        ad._push_completion_event(record, {"results": [entry, {"task_index": 1, "status": "completed", "summary": "done"}]}, "completed")
    final = registry.completion_queue.get_nowait()
    route(surface, [final])
    assert len(starts) == 2
    assert "done" in starts[1][0]
    assert not final.get("task_failure_notice")


def test_explicit_process_notification_off_keeps_status_without_waking(surface, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "display": {"background_process_notifications": "off"},
    }))
    route(surface, [event(completion_reason="lost", exit_code=None)])
    assert surface[2] == []
    assert len(surface[3]) == 1


def test_single_failed_command_remains_available_to_its_child(surface):
    from tools.process_registry import ProcessRegistry, ProcessSession

    session, _, starts, frames = surface
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = ProcessSession(
        id="proc_child_recoverable", command="build target", started_at=1,
        session_key="parent-session", owner_task_id="sa-child", notify_on_complete=True,
    )
    process.append_output("recoverable compiler diagnostic")
    registry._running[process.id] = process
    with server._session_profile_runtime_scope(session):
        registry._finish_exited(process, 2)
    route((session, registry, starts, frames), [registry.completion_queue.get_nowait()])
    assert starts == []
    assert len(frames) == 1
    assert registry.poll(process.id)["exit_code"] == 2
    assert registry.read_log(process.id)["output"] == "recoverable compiler diagnostic"
