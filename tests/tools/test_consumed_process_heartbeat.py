"""A queued heartbeat must not repeat a terminal result already read via wait/log."""

import threading
from types import SimpleNamespace

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


def _queued_finished_process(registry, exit_code):
    process = ProcessSession(
        id="proc_heartbeat_receipt", command="bounded job", session_key="owner",
        task_id="owner", started_at=1.0, notify_on_complete=True,
    )
    registry._running[process.id] = process
    process.append_output("stage complete\n")
    registry._emit_heartbeat(process, now=61.0)
    registry._finish_exited(process, exit_code)
    return process


@pytest.mark.parametrize("exit_code", [0, 7])
def test_consumed_result_drops_queued_heartbeat_but_preserves_unread_result(exit_code):
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = _queued_finished_process(registry, exit_code)

    # Reading terminal output is the native consumption boundary; poll alone is not.
    result = registry.read_log(process.id)
    assert result["output"] == "stage complete"
    assert process.exit_code == exit_code
    assert registry.is_completion_consumed(process.id)
    assert registry.drain_notifications(session_key="owner", skip_poll_observed=False) == []

    unread = ProcessRegistry()
    unread._completions_restored = True
    other = _queued_finished_process(unread, exit_code)
    pending = unread.drain_notifications(session_key="owner", skip_poll_observed=False)
    assert [event["type"] for event, _ in pending] == ["heartbeat", "completion"]
    assert pending[-1][0]["session_id"] == other.id
    assert pending[-1][0]["exit_code"] == exit_code
    assert not unread.is_completion_consumed(other.id)


def test_foreign_owner_cannot_drop_consumed_process_heartbeat():
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = _queued_finished_process(registry, 0)
    registry.read_log(process.id)
    assert registry.drain_notifications(session_key="foreign", skip_poll_observed=False) == []
    assert registry.completion_queue.qsize() == 2
    assert registry.drain_notifications(session_key="owner", skip_poll_observed=False) == []


@pytest.mark.parametrize("state", ["running", "unknown", "poll", "partial-log", "reader-finalizing"])
def test_unconsumed_heartbeat_remains_deliverable(state):
    registry = ProcessRegistry()
    registry._completions_restored = True
    process = ProcessSession(
        id="proc_unconsumed", command="bounded job", session_key="owner", started_at=1.0,
    )
    registry._running[process.id] = process
    process.append_output("earlier\nlatest\n")
    registry._emit_heartbeat(process, now=61.0)
    if state == "unknown":
        registry._running.clear()
    elif state != "running":
        process.mark_exited(0)
        if state == "poll":
            registry.poll(process.id)
        elif state == "partial-log":
            registry.read_log(process.id, offset=0, limit=1)
        else:
            process._reader_finish_requested.set()
            process._reader_thread = SimpleNamespace(is_alive=lambda: True)
            registry.read_log(process.id)
    assert not registry.is_completion_consumed(process.id)
    notifications = registry.drain_notifications(session_key="owner", skip_poll_observed=False)
    assert [event["type"] for event, _ in notifications] == ["heartbeat"]


@pytest.mark.platforms("linux")
def test_real_process_wait_consumes_pending_heartbeat(tmp_path, monkeypatch):
    import shlex
    import sys

    registry = ProcessRegistry()
    registry._completions_restored = True
    publish = registry._publish_finished
    enqueued = threading.Event()

    def publish_after_heartbeat(process, was_running):
        registry._emit_heartbeat(process, now=process.started_at + 61)
        publish(process, was_running)
        enqueued.set()

    monkeypatch.setattr(registry, "_publish_finished", publish_after_heartbeat)
    process = registry.spawn_local(
        shlex.join([sys.executable, "-c", "print('real terminal output')"]), cwd=str(tmp_path),
    )
    assert enqueued.wait(10)
    result = registry.wait(process.id, timeout=10)
    assert result["exit_code"] == 0 and "real terminal output" in result["output"]
    assert registry.is_completion_consumed(process.id)
    assert registry.drain_notifications(skip_poll_observed=False) == []
