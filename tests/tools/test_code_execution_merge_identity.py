"""The fork's batch authority and upstream explicit identities share one dispatch."""

import json
import threading
from unittest.mock import patch

import pytest

from model_tools import handle_function_call
from tools.approval_context import (
    get_current_observability_context,
    reset_current_observability_context,
    set_current_observability_context,
)
from tools.code_execution_rpc import _default_dispatch, _handle_rpc_request
from tools.code_kernel import CellAuthority, shutdown_all_kernels


@pytest.mark.parametrize("route", ["kernel", "rpc"])
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("session_id,turn_id", [("sess-A", "turn-A"), ("", "")])
def test_explicit_identity_survives_conflicting_ambient_context(
    route, parallel, session_id, turn_id, tmp_path,
):
    target = tmp_path / "note.txt"
    target.write_text("hello\n")
    seen = []
    lock = threading.Lock()

    def record(tool_name, args, **kwargs):
        with lock:
            seen.append((tool_name, kwargs, get_current_observability_context()))
        return None, None

    tokens = set_current_observability_context(
        session_id="stale-session", turn_id="stale-turn", tool_call_id="outer-call",
    )
    try:
        authority = CellAuthority(
            "task-A", 2, session_id=session_id, turn_id=turn_id, conversation_id="conv-A",
        )
        dispatch = authority.dispatch if route == "kernel" else _default_dispatch(
            "task-A", session_id, turn_id, "conv-A",
        )
        counter, log = (authority.counter, authority.log) if route == "kernel" else ([0], [])
        call = {"tool": "read_file", "args": {"path": str(target)}}
        request = {"tool": "__parallel__", "args": {"calls": [call, call]}} if parallel else call
        with patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", side_effect=record):
            result = json.loads(_handle_rpc_request(
                request, allowed_tools=frozenset({"read_file"}), tool_call_counter=counter,
                max_tool_calls=2, dispatch=dispatch, tool_call_log=log,
                call_start=0.0, where="identity test", allow_parallel=True,
            ))
        results = result if parallel else [result]
        assert all("hello" in item["content"] for item in results), result
        assert counter == [len(results)]
        assert len(log) == len(seen) == len(results)
        for name, identity, scope in seen:
            assert name == "read_file"
            assert identity["session_id"] == session_id
            assert identity["turn_id"] == turn_id
            assert identity["conversation_id"] == "conv-A"
            if parallel:
                assert scope["session_id"] == session_id
                assert scope["turn_id"] == turn_id
                assert scope["tool_call_id"] == identity["tool_call_id"]
        if parallel:
            ids = [identity["tool_call_id"] for _, identity, _ in seen]
            assert len(set(ids)) == 2
            assert all(call_id.startswith("ptc-") for call_id in ids)
        assert get_current_observability_context() == {
            "session_id": "stale-session", "turn_id": "stale-turn", "tool_call_id": "outer-call",
        }
        authority.retire()
        if route == "kernel":
            before = (list(counter), list(log))
            refused = json.loads(_handle_rpc_request(
                call, allowed_tools=frozenset({"read_file"}), tool_call_counter=counter,
                max_tool_calls=2, dispatch=dispatch, tool_call_log=log,
                call_start=0.0, where="retired identity test",
            ))
            assert "No active execute_code cell" in refused["error"]
            assert (counter, log) == before
    finally:
        reset_current_observability_context(tokens)


def test_reused_kernel_parallel_cells_refresh_explicit_identity_and_positional_budget(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("hello\n")
    code = (
        "from hermes_tools import parallel\n"
        f"print(parallel([{{'tool': 'read_file', 'args': {{'path': {str(target)!r}}}}}]))"
    )
    seen = []

    def record(tool_name, args, **kwargs):
        if tool_name == "read_file":
            seen.append(kwargs)
        return None, None

    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    try:
        with patch("tools.code_execution_tool._load_config", return_value={
            "mode": "strict", "timeout": 30, "max_tool_calls": 1,
        }), patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", side_effect=record):
            results = [json.loads(handle_function_call(
                "execute_code", {"code": code}, task_id="same-kernel", session_id="sess-A",
                turn_id=turn, conversation_id="conv-A", enabled_tools=["execute_code", "read_file"],
            )) for turn in ("turn-1", "turn-2")]
        assert all(result["status"] == "success" for result in results), results
        assert all(result["tool_calls_made"] == 1 for result in results), results
        assert results[1]["kernel"]["reused"] is True
        assert [(call["session_id"], call["turn_id"], call["conversation_id"]) for call in seen] == [
            ("sess-A", "turn-1", "conv-A"), ("sess-A", "turn-2", "conv-A"),
        ]
        assert len({call["tool_call_id"] for call in seen}) == 2
    finally:
        shutdown_all_kernels()
