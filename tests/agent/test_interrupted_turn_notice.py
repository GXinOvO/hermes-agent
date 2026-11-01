"""Interrupted turns retain cancellation meaning in core and gateway transcripts.

Reproduces a real explicit Stop recorded as the ordinary resend-request boundary.
Uses real SessionDB persistence, without a provider or active gateway.
"""
from types import SimpleNamespace
from typing import Any

import pytest

from agent.conversation_loop import _close_durable_failed_turn
from agent.turn_failure_copy import (
    FAILED_TURN_DISPLAY_KIND,
    FAILED_TURN_NOTICE,
    PARTIAL_FAILED_TURN_NOTICE,
    untyped_failed_turn_display_kind,
)
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from hermes_state import SessionDB


def _messages(tool_ran):
    rows: list[dict[str, Any]] = [{"role": "user", "content": "apply the scoped change"}]
    if tool_ran:
        rows.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call1", "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call1", "content": "side effect already completed"},
        ])
    return rows


@pytest.mark.parametrize("tool_ran", [False, True])
def test_interrupted_boundary_is_distinct_and_durable(tmp_path, tool_ran):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    sid = "interrupted-turn"
    db.create_session(session_id=sid, source="local", model="fixture-model")
    rows = _messages(tool_ran)
    for row in rows:
        db.append_message(sid, row["role"], row.get("content"),
                          tool_calls=row.get("tool_calls"), tool_call_id=row.get("tool_call_id"))

    def flush(messages):
        row = messages[-1]
        db.append_message(sid, row["role"], row["content"],
                          display_kind=row.get("display_kind"),
                          display_metadata=row.get("display_metadata"))

    agent = SimpleNamespace(_session_db=db, session_id=sid, provider="custom", model="fixture-model",
                            _flush_messages_to_session_db=flush)
    result = {"completed": False, "interrupted": True, "messages": rows,
              "current_turn_user_idx": 0, "history_offset": 0}
    _close_durable_failed_turn(agent, result)
    notice = rows[-1]["content"]
    assert "interrupted" in notice.lower(), notice
    assert "send it again" not in notice.lower(), notice
    if tool_ran:
        assert "verify" in notice.lower() and "actions" in notice.lower(), notice
    assert rows[-1]["display_kind"] == FAILED_TURN_DISPLAY_KIND
    assert untyped_failed_turn_display_kind("assistant", notice) == FAILED_TURN_DISPLAY_KIND
    before = db.message_count()
    _close_durable_failed_turn(agent, result)
    assert db.message_count() == before
    db.close()

    # Reload is where the original interruption's live status was lost.
    reopened = SessionDB(path)
    persisted = reopened.get_messages(sid)
    assert persisted[-1]["content"] == notice
    assert persisted[-1]["display_kind"] == FAILED_TURN_DISPLAY_KIND
    reopened.append_message(sid, "user", "do something different")
    tail = reopened.get_messages(sid)[-3:]
    assert [row["role"] for row in tail] == ["tool" if tool_ran else "user", "assistant", "user"]
    assert tail[-1]["content"] == "do something different"
    reopened.close()


@pytest.mark.parametrize("tool_ran", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_gateway_notice_distinguishes_interruptions_without_changing_failures(tool_ran, interrupted):
    runner = object.__new__(GatewayTurnPersistenceMixin)
    result = {"messages": _messages(tool_ran), "history_offset": 0, "interrupted": interrupted}
    notice = runner._hmwa_failed_turn_notice(result)
    if interrupted:
        assert "interrupted" in notice.lower(), notice
        assert "send it again" not in notice.lower(), notice
        if tool_ran:
            assert "actions" in notice.lower() and "verify" in notice.lower(), notice
    else:
        assert notice == (PARTIAL_FAILED_TURN_NOTICE if tool_ran else FAILED_TURN_NOTICE)
