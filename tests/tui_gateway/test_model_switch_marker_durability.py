"""A model pivot may join later turns only after its original row is durable."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import _merge_consecutive_users
from agent.context_compressor import _DB_PERSISTED_MARKER
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def marker_session(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("chat", "desktop")
    agent = SimpleNamespace(_session_db=db, session_id="chat", model="new-model", provider="test-provider")
    session = {"session_key": "chat", "_sid": "marker-test", "agent": agent, "history": [],
               "history_lock": threading.RLock(), "history_version": 0}
    session["test_notices"] = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload: session["test_notices"].append((event, sid, payload)))
    try:
        yield session, db
    finally:
        db.close()


def _block_marker_writes(db):
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER reject_model_marker BEFORE INSERT ON messages "
        "WHEN NEW.display_kind = 'model_switch' "
        "BEGIN SELECT RAISE(ABORT, 'marker storage unavailable'); END"))


def _pivot(session, model="new-model"):
    return server._append_model_switch_marker(session, model=model, provider="test-provider")


def _try_pivot(session):
    _pivot(session)
    return "\n".join(payload["message"] for event, _sid, payload in session["test_notices"] if event == "error")


def _merge_and_archive(session, db, text):
    human = {"role": "user", "content": text}
    db.append_messages_batch(session["agent"].session_id, [human])
    human[_DB_PERSISTED_MARKER] = True
    original = copy.deepcopy(human)
    live, repairs = _merge_consecutive_users([*session["history"], human])
    db.archive_and_compact(session["agent"].session_id, live)
    rows = db.get_messages(session["agent"].session_id, include_inactive=True)
    # Independent store-side oracle: no reconstruction of a missing marker from composite text.
    composites = [row for row in rows if row.get("absorbed_message_uids")]
    for row in composites:
        members = [row["message_uid"], *json.loads(row["absorbed_message_uids"])]
        for uid in members:
            assert any(prior["id"] < row["id"] and prior["message_uid"] == uid
                       and not prior.get("absorbed_message_uids") for prior in rows), (
                f"composite {row['id']} has no prior original for {uid}")
    return original, live, repairs, rows


def test_failed_marker_cannot_become_a_missing_prior_merge_member(marker_session):
    session, db = marker_session
    _block_marker_writes(db)

    error = _try_pivot(session)
    _original, live, repairs, rows = _merge_and_archive(session, db, "keep this human request")

    assert repairs == 0 and [row["content"] for row in live] == ["keep this human request"]
    assert all(row.get("display_kind") != "model_switch" for row in rows)
    assert session["history"] == [] and session["history_version"] == 0
    assert session["agent"].model == "new-model"
    assert "switched" in error.lower() and "retry" in error.lower()

    # The existing selection entry can retry the already-selected model after storage recovers.
    db._execute_write(lambda conn: conn.execute("DROP TRIGGER reject_model_marker"))
    _pivot(session)
    marker = copy.deepcopy(session["history"][-1])
    human, merged, repairs, rows = _merge_and_archive(session, db, "next request")
    assert repairs == 1
    assert merged[0]["message_uid"] == marker["message_uid"]
    assert merged[0]["_absorbed_message_uids"] == [human["message_uid"]]
    assert merged[0]["content"] == marker["content"] + "\n\n" + human["content"]
    assert any(row["message_uid"] == marker["message_uid"]
               and row["content"] == marker["content"] for row in rows)


def test_marker_time_and_uid_survive_native_merge_archive_and_reopen(marker_session, monkeypatch):
    session, db = marker_session
    # Stamp via the native metadata helper, independently of SessionDB's default write clock.
    monkeypatch.setattr("agent.message_metadata.wall_time", lambda: 1700000000.25)
    _pivot(session)
    marker = copy.deepcopy(session["history"][-1])
    stored = db.get_messages("chat")[0]
    assert marker.get("timestamp") == stored["timestamp"] == 1700000000.25
    assert marker["message_uid"] == stored["message_uid"]
    assert marker["_row_id"] == stored["id"] and marker[_DB_PERSISTED_MARKER] is True

    human, merged, repairs, rows = _merge_and_archive(session, db, "真实人工输入")
    assert repairs == 1
    assert merged[0]["timestamp"] == marker["timestamp"]
    assert merged[0]["message_uid"] == marker["message_uid"]
    assert merged[0]["_absorbed_message_uids"] == [human["message_uid"]]
    assert any(row["message_uid"] == human["message_uid"]
               and row["timestamp"] == human["timestamp"] and row["content"] == human["content"] for row in rows)
    with SessionDB(db_path=db.db_path) as reader:
        restored = reader.get_messages_as_conversation("chat", repair_alternation=False)
    assert restored[0]["timestamp"] == marker["timestamp"]
    assert restored[0]["content"] == marker["content"] + "\n\n" + human["content"]
    assert restored[0]["message_uid"] == marker["message_uid"]


@pytest.mark.parametrize("failure_stage", ["insert", "counter-update"])
def test_failed_replacement_preserves_the_previous_marker(marker_session, failure_stage):
    session, db = marker_session
    _pivot(session, "previous-model")
    prior_history = copy.deepcopy(session["history"])
    prior_rows = db.get_messages("chat", include_inactive=True)
    version = session["history_version"]
    if failure_stage == "insert":
        _block_marker_writes(db)
    else:
        # The INSERT has run and stamped its tentative row id, but the transaction still must roll back.
        db._execute_write(lambda conn: conn.execute(
            "CREATE TRIGGER reject_marker_counter BEFORE UPDATE OF message_count ON sessions "
            "WHEN NEW.message_count > OLD.message_count "
            "BEGIN SELECT RAISE(ABORT, 'marker counter commit unavailable'); END"))

    error = _try_pivot(session)

    assert session["history"] == prior_history
    assert session["history_version"] == version
    assert db.get_messages("chat", include_inactive=True) == prior_rows
    assert "switched" in error.lower() and "retry" in error.lower()
    assert session["agent"].model == "new-model"


def test_no_store_refuses_a_live_marker_and_can_retry(marker_session, monkeypatch):
    session, db = marker_session
    session["agent"]._session_db = None
    monkeypatch.setattr(server, "_get_db", lambda: None)

    error = _try_pivot(session)

    assert session["history"] == [] and session["history_version"] == 0
    assert "switched" in error.lower() and "retry" in error.lower()
    session["agent"]._session_db = db
    _pivot(session)
    assert len(session["history"]) == len(db.get_messages("chat")) == 1


@pytest.mark.parametrize("once", [False, True])
def test_storage_failure_preserves_the_applied_switch_and_retry_entry(marker_session, monkeypatch, once):
    session, db = marker_session
    agent = session["agent"]
    agent.model = "old-model"
    agent.base_url, agent.api_key, agent.api_mode = "", "", "chat_completions"
    agent.reasoning_config = None
    agent._primary_runtime = None

    def swap(**kwargs):
        agent.model, agent.provider = kwargs["new_model"], kwargs["new_provider"]

    agent.switch_model = swap
    result = SimpleNamespace(success=True, new_model="new-model", target_provider="test-provider",
                             base_url="", api_key="", api_mode="chat_completions", warning_message="",
                             runtime_capabilities=None, model_info=None)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kwargs: result)
    for name in ("_restart_slash_worker", "_persist_live_session_runtime", "_persist_live_session_system_prompt",
                 "_emit_session_info", "_merge_preflight_warning"):
        monkeypatch.setattr(server, name, lambda *args, **kwargs: None)
    _block_marker_writes(db)
    raw = "new-model --provider test-provider " + ("--once" if once else "--session")

    switched = server._apply_model_switch("marker-test", session, raw, confirm_expensive_model=True, count_switch=False)

    assert switched["value"] == agent.model == "new-model"
    assert session["history"] == []
    if once:
        assert session["one_turn_model_restore"]["model"] == "old-model"
    else:
        assert session["model_override"]["model"] == "new-model"
    errors = [payload["message"] for event, sid, payload in session["test_notices"]
              if event == "error" and sid == "marker-test"]
    assert len(errors) == 1 and "Model switched" in errors[0] and "retry" in errors[0]

    db._execute_write(lambda conn: conn.execute("DROP TRIGGER reject_model_marker"))
    # Recover through the actual selector after storage returns.
    server._apply_model_switch("marker-test", session, "new-model --provider test-provider --session",
                               confirm_expensive_model=True, count_switch=False)
    assert len(session["history"]) == len(db.get_messages("chat")) == 1


def test_failed_notice_resolves_session_outside_history_lock(marker_session, monkeypatch):
    session, db = marker_session
    _block_marker_writes(db)
    lock = threading.Lock()
    session["history_lock"] = lock

    def resolve_sid(current):
        assert current is session
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        assert acquired, "notice resolver would acquire sessions lock while holding history lock"
        return "marker-test"

    monkeypatch.setattr(server, "_lifecycle_own_sid", resolve_sid)
    assert "retry" in _try_pivot(session)
    assert session["history"] == []


def test_publication_holds_history_lock_until_native_commit(marker_session, monkeypatch):
    session, db = marker_session
    acquired = threading.Event()
    release = threading.Event()
    original_execute = db._execute_write
    failures = []

    def hold_commit(callback, **kwargs):
        acquired.set()
        assert release.wait(5), "test did not release the native commit"
        return original_execute(callback, **kwargs)

    def write_pivot():
        try:
            _pivot(session)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(db, "_execute_write", hold_commit)
    worker = threading.Thread(target=write_pivot)
    worker.start()
    try:
        assert acquired.wait(5), "writer never reached storage"
        escaped = session["history_lock"].acquire(blocking=False)
        if escaped:
            session["history_lock"].release()
        assert not escaped, "live marker escaped before durable commit"
        assert session["history"] == []
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert len(session["history"]) == len(db.get_messages("chat")) == 1
