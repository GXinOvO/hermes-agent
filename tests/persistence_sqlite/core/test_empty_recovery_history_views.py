"""Read-only compatibility for legacy empty-response recovery pairs."""

import json
import sqlite3

import pytest

from hermes_state import SessionDB


NUDGE = (
    "You just executed tool calls but returned an empty response. Please process the tool "
    "results above and continue with the task."
)
CALLS = [{"id": "call_one", "type": "function", "function": {
    "name": "terminal", "arguments": '{"command":"pwd"}',
}}]


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    try:
        yield store
    finally:
        store.close()


def _seed(db, sid="chat", *, assistant=None, user=None):
    db.create_session(sid, source="desktop")
    db.append_messages_batch(sid, [
        {"role": "user", "content": "real ask"},
        {"role": "assistant", "content": "checking", "tool_calls": CALLS},
        {"role": "tool", "content": "/work", "tool_call_id": "call_one", "tool_name": "terminal"},
        {"role": "assistant", "content": "(empty)", "finish_reason": "stop",
         "reasoning_content": "r" * 598, **(assistant or {})},
        {"role": "user", "content": NUDGE, **(user or {})},
        {"role": "assistant", "content": "real answer"},
    ])
    return db.get_messages(sid)


def _sql_rows(db):
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute("SELECT * FROM messages ORDER BY id").fetchall()


def _seed_four_legacy_rows(db):
    db.create_session("legacy", source="desktop")
    with sqlite3.connect(db.db_path) as conn:
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [("legacy", role, content, index + 1.0) for index, (role, content) in enumerate([
                ("tool", "legacy tool result"), ("assistant", "(empty)"),
                ("user", NUDGE), ("assistant", "real answer"),
            ])],
        )


def test_model_loader_handles_four_legacy_rows(db):
    _seed_four_legacy_rows(db)
    before = _sql_rows(db)
    history = db.get_messages_as_conversation("legacy", include_row_ids=True)
    assert [m["content"] for m in history] == ["legacy tool result", "real answer"]
    assert _sql_rows(db) == before


def test_rpc_history_projection_hides_persisted_pair(db):
    from tui_gateway import server

    _seed_four_legacy_rows(db)
    raw = db.get_messages("legacy")
    shown = server._history_to_messages(raw)
    assert [(m["role"], m.get("text")) for m in shown] == [
        ("tool", None), ("assistant", "real answer"),
    ]
    assert db.get_messages("legacy") == raw


@pytest.mark.parametrize("view", ["conversation", "resume_model", "resume_display"])
def test_legacy_pair_is_absent_from_replay_and_resume_without_storage_changes(db, view):
    raw = _seed(db)
    before = _sql_rows(db)
    if view == "conversation":
        history = db.get_messages_as_conversation("chat", include_row_ids=True)
    else:
        model, display = db.get_resume_conversations("chat")
        history = model if view == "resume_model" else display
    assert [m["content"] for m in history] == ["real ask", "checking", "/work", "real answer"]
    assert [m["_row_id"] for m in history] == [m["id"] for m in raw if m["content"] not in ("(empty)", NUDGE)]
    assert db.get_messages("chat") == raw
    assert db.get_messages("chat", include_inactive=True) == raw
    assert _sql_rows(db) == before
    assert db.get_session("chat")["message_count"] == len(raw)


def test_rest_projection_hides_pair_without_dropping_physical_rows(db):
    from hermes_cli.web_routers.sessions import _project_for_display

    _seed_four_legacy_rows(db)
    raw = db.get_messages("legacy")
    before = _sql_rows(db)
    shown = _project_for_display(raw)
    assert [m.get("display_kind") for m in shown] == [None, "hidden", "hidden", None]
    assert [m["id"] for m in shown] == [m["id"] for m in raw]
    assert [m["content"] for m in shown] == [m["content"] for m in raw]
    assert _project_for_display(shown) == shown
    assert db.get_messages("legacy") == raw
    assert _sql_rows(db) == before


@pytest.mark.parametrize("order", ["oldest", "latest"])
@pytest.mark.parametrize("include_compacted", [False, True])
def test_rest_page_size_one_recognizes_both_sides_of_pair(tmp_path, monkeypatch, db, order, include_compacted):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers import sessions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", db.db_path)
    _seed_four_legacy_rows(db)
    raw = db.get_messages("legacy")
    before = _sql_rows(db)
    app = FastAPI()
    app.include_router(sessions.manage_router)
    with TestClient(app) as client:
        pages = [client.get("/api/sessions/legacy/messages", params={
            "limit": 1, "offset": offset, "order": order, "include_compacted": include_compacted,
        }) for offset in range(len(raw) + 1)]
    assert all(response.status_code == 200 for response in pages)
    payloads = [response.json() for response in pages]
    expected = raw if order == "oldest" else list(reversed(raw))
    for page, stored in zip(payloads, expected):
        assert page["pagination"]["returned"] == 1
        assert [m["id"] for m in page["messages"]] == [stored["id"]]
        assert page["messages"][0].get("display_kind") == (
            "hidden" if stored["content"] in ("(empty)", NUDGE) else None
        )
    assert payloads[-1]["messages"] == []
    assert payloads[-1]["pagination"]["returned"] == 0
    assert db.get_messages("legacy") == raw
    assert _sql_rows(db) == before


@pytest.mark.parametrize("assistant,user", [
    ({"tool_calls": CALLS}, {}),
    ({"tool_call_id": "real-tool-result"}, {}),
    ({}, {"platform_message_id": "human-1"}),
    ({}, {"message_id": "human-2"}),
    ({}, {"display_metadata": {"typed_ask": NUDGE}}),
    ({}, {"display_kind": "steer"}),
    ({"api_content": "real provider payload"}, {}),
    ({"codex_message_items": [{"type": "message", "content": "real reply"}]}, {}),
    ({"codex_reasoning_items": [{"type": "compaction", "encrypted_content": "checkpoint"}]}, {}),
    ({"content": " (empty)"}, {}),
    ({}, {"content": NUDGE + " "}),
    ({"content": "(Empty)"}, {}),
    ({}, {"content": NUDGE + "!"}),
])
def test_known_provenance_payloads_and_nonexact_text_survive(db, assistant, user):
    from hermes_cli.web_routers.sessions import _project_for_display
    from hermes_state_recovery import without_legacy_empty_recovery_pairs

    raw = _seed(db, assistant=assistant, user=user)
    before = _sql_rows(db)
    assert without_legacy_empty_recovery_pairs(raw) == raw
    shown = _project_for_display(raw)
    assert [m.get("display_kind") for m in shown] == [m.get("display_kind") for m in raw]
    replay = db.get_messages_as_conversation("chat", include_row_ids=True)
    assert [m["_row_id"] for m in replay] == [m["id"] for m in raw]
    assert replay[3].get("tool_calls") == assistant.get("tool_calls")
    assert db.get_messages("chat") == raw
    assert _sql_rows(db) == before


@pytest.mark.parametrize("limit", [4, 5, 6])
def test_rest_around_hides_pair_at_window_edge(tmp_path, monkeypatch, db, limit):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers import sessions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", db.db_path)
    raw = _seed(db)
    before = _sql_rows(db)
    app = FastAPI()
    app.include_router(sessions.manage_router)
    with TestClient(app) as client:
        response = client.get("/api/sessions/chat/messages/around", params={"row_id": raw[0]["id"], "limit": limit})
    assert response.status_code == 200
    page = response.json()
    assert [row["id"] for row in page["messages"]] == [row["id"] for row in raw[:limit]]
    assert page["messages"][3]["display_kind"] == "hidden"
    assert page["pagination"]["returned"] == limit
    assert _sql_rows(db) == before


@pytest.mark.parametrize("view", ["conversation", "resume_model", "resume_display", "rpc", "rest"])
def test_native_checkpoint_survives_recovery_pair_projection(db, view):
    from hermes_cli.web_routers.sessions import _project_for_display
    from tui_gateway import server

    checkpoint = [{"type": "compaction", "encrypted_content": "checkpoint"}]
    raw = _seed(db, assistant={"codex_reasoning_items": checkpoint})
    before = _sql_rows(db)
    assert json.loads(raw[3]["codex_reasoning_items"]) == checkpoint
    if view == "conversation":
        history = db.get_messages_as_conversation("chat", include_row_ids=True)
    elif view.startswith("resume_"):
        model, display = db.get_resume_conversations("chat")
        history = model if view == "resume_model" else display
    elif view == "rpc":
        history = server._history_to_messages(raw)
    else:
        history = _project_for_display(raw)
    carrier = next(m for m in history if m.get("content", m.get("text")) == "(empty)")
    items = carrier["codex_reasoning_items"]
    assert (json.loads(items) if isinstance(items, str) else items) == checkpoint
    assert carrier.get("display_kind") != "hidden"
    assert any(m.get("content", m.get("text")) == NUDGE for m in history)
    assert db.get_messages("chat") == raw
    assert _sql_rows(db) == before


def test_orphan_nudge_and_ordinary_tools_survive(db):
    from hermes_cli.web_routers.sessions import _project_for_display
    from hermes_state_recovery import without_legacy_empty_recovery_pairs

    db.create_session("chat", source="desktop")
    db.append_messages_batch("chat", [
        {"role": "user", "content": NUDGE},
        {"role": "assistant", "content": "checking", "tool_calls": CALLS},
        {"role": "tool", "content": "result", "tool_call_id": "call_one"},
        {"role": "assistant", "content": "(empty)"},
    ])
    raw = db.get_messages("chat")
    assert without_legacy_empty_recovery_pairs(raw) == raw
    assert [m.get("display_kind") for m in _project_for_display(raw)] == [None] * len(raw)
    assert len(db.get_messages_as_conversation("chat")) == len(raw)


def test_shared_filter_is_idempotent_without_mutating_input(db):
    from hermes_state_recovery import without_legacy_empty_recovery_pairs

    _seed_four_legacy_rows(db)
    raw = db.get_messages("legacy")
    filtered = without_legacy_empty_recovery_pairs(raw)
    assert without_legacy_empty_recovery_pairs(filtered) == filtered
    assert [m["content"] for m in filtered] == ["legacy tool result", "real answer"]
    assert db.get_messages("legacy") == raw
