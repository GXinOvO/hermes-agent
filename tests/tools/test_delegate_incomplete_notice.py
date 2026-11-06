import threading
from types import SimpleNamespace

from tools import async_delegation as ad
from tools import delegate_tool_dispatch as dispatch
from tools.delegate_tool_child_run import _build_result_entry, _SchemaOutcome
from tools.process_registry import ProcessRegistry
from tools.process_registry_notifications import async_delegation_display_text, format_process_notification


def test_budget_exhaustion_warns_parent_before_the_sibling_finishes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = ProcessRegistry()
    registry._completions_restored = True
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    parent = SimpleNamespace(_interrupt_requested=False)
    child = SimpleNamespace(model="test-model")
    entry = _build_result_entry(
        child, {"final_response": "partial verified evidence", "completed": False, "api_calls": 3},
        0, 1, _SchemaOutcome(None, None, [], 0),
    )
    assert entry["status"] == "completed"
    assert entry["exit_reason"] == "max_iterations" and entry["truncated"] is True
    sibling_started = threading.Event()
    release_sibling = threading.Event()
    record = {
        "delegation_id": "deleg-budget", "status": "running", "is_batch": True,
        "session_key": "parent", "origin_ui_session_id": "ui", "goal": "first task",
        "goals": ["first task", "slow sibling"], "dispatched_at": 1,
    }
    monkeypatch.setattr(ad, "_records", {record["delegation_id"]: record})
    tasks = [{"goal": "first task"}, {"goal": "slow sibling"}]
    batch = dispatch._Batch(
        task_list=tasks, children=[(i, task, child) for i, task in enumerate(tasks)],
        parent_agent=parent, creds={}, context=None, top_role="leaf", max_children=2,
        live_deleg_id=None, live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="ui", origin_owner_transport=None, origin_owner_session_record=None,
        origin_session_history_delivery=False, overall_start=1, unit_id=record["delegation_id"],
    )
    monkeypatch.setattr(dispatch, "_record_finished_child", lambda *args: None)
    monkeypatch.setattr(dispatch, "_report_child_done", lambda *args: None)

    def child_entry(index, task, child):
        if index == 1:
            sibling_started.set()
            assert release_sibling.wait(10)
            return {"task_index": 1, "status": "completed", "summary": "sibling done"}
        assert sibling_started.wait(5)
        return dict(entry)

    batch.run_child = child_entry
    results, errors = [], []

    def run():
        try:
            dispatch._run_children_parallel(batch, results, honor_parent_interrupt=False)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert sibling_started.wait(5)
        notice = registry.completion_queue.get(timeout=3)
        assert worker.is_alive()
        assert ad._records[record["delegation_id"]]["status"] == "running"
        assert notice["task_failure_notice"] is True
        assert notice["results"][0]["summary"] == "partial verified evidence"
        text = format_process_notification(notice)
        assert text is not None
        assert "TASK INCOMPLETE" in text
        assert "max_iterations" in text
        assert "partial verified evidence" in text
        assert "Incomplete" in async_delegation_display_text(notice)
        assert ad.claim_event_delivery(notice, "tui-poller") == ""
    finally:
        release_sibling.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert sorted(r["task_index"] for r in results) == [0, 1]
