"""Consumed terminal output must not wake Desktop/TUI through an old heartbeat."""

import threading

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_notifications import format_process_notification
from tui_gateway import server


@pytest.mark.parametrize("exit_code", [0, 7])
def test_consumed_heartbeat_does_not_claim_a_turn_or_hide_other_events(monkeypatch, exit_code):
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = ProcessSession(
        id="proc_tui_consumed", command="bounded job", started_at=1.0,
        session_key="owner", task_id="owner", notify_on_complete=True,
    )
    registry._running[process.id] = process
    process.append_output("finished stage\n")
    registry._emit_heartbeat(process, now=61.0)
    heartbeat = registry.completion_queue.get_nowait()
    registry._finish_exited(process, exit_code)
    assert registry.read_log(process.id)["output"] == "finished stage"
    assert process.exit_code == exit_code
    assert registry.is_completion_consumed(process.id)

    session = {"history_lock": threading.RLock(), "running": False, "session_key": "owner"}
    dispatched, painted = [], []
    monkeypatch.setattr(server, "_emit", lambda *a, **k: painted.append(a))
    monkeypatch.setattr(server, "_background_notifications_off", lambda _s: False)
    monkeypatch.setattr(server, "_notif_dispatch_event", lambda *a: dispatched.append(a))

    assert server._notif_handle_event(
        "ui", session, heartbeat, set(), registry, format_process_notification, None, owned=True,
    )
    assert not session["running"]
    assert dispatched == painted == []

    # A watch or delegated result is not interchangeable with a process completion.
    for event in (
        {**heartbeat, "type": "watch_match", "pattern": "finished"},
        {"type": "async_delegation", "delegation_id": "deleg-keep", "results": []},
    ):
        session["running"] = False
        assert server._notif_handle_event(
            "ui", session, event, set(), registry, lambda _e: "actionable result", None, owned=True,
        )
        assert session["running"]
    assert len(dispatched) == 2
