import json
import threading
from unittest.mock import patch

import pytest

from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels


@pytest.fixture(autouse=True)
def fresh_kernels(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def test_independent_read_batch_overlaps_and_keeps_result_order():
    rendezvous = threading.Barrier(3, timeout=5)

    def dispatch(name, args, **kwargs):
        assert name == "read_file"
        rendezvous.wait()
        return json.dumps({"content": args["path"]})

    code = (
        "import json\n"
        "from hermes_tools import _call\n"
        "print(json.dumps(_call('__parallel__', {'calls': ["
        "{'tool': 'read_file', 'args': {'path': path}} "
        "for path in ['third', 'first', 'second']]})))"
    )
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "timeout": 15}), \
            patch("model_tools.handle_function_call", side_effect=dispatch):
        result = json.loads(_execute_code_handler(
            {"code": code}, task_id="readonly-batch", enabled_tools=["read_file"],
        ))

    assert result["status"] == "success", result
    assert json.loads(result["output"]) == [
        {"content": "third"}, {"content": "first"}, {"content": "second"},
    ]
    assert result["tool_calls_made"] == 3


def run_parallel(calls, *, enabled=("read_file",), budget=50, task_id="readonly-batch"):
    code = (
        "import json\nfrom hermes_tools import parallel\n"
        f"print(json.dumps(parallel({calls!r})))"
    )
    with patch("tools.code_execution_tool._load_config", return_value={
        "mode": "strict", "timeout": 15, "max_tool_calls": budget,
    }):
        return json.loads(_execute_code_handler(
            {"code": code}, task_id=task_id, enabled_tools=list(enabled),
        ))


@pytest.mark.parametrize("invalid", [
    {"tool": "terminal", "args": {"command": "do not run"}},
    {"tool": "write_file", "args": {"path": "output", "content": "x"}},
    {"tool": "patch", "args": {}},
    {"tool": "web_search", "args": {"query": "disabled"}},
    {"tool": "__parallel__", "args": {"calls": []}},
    {"tool": "read_file", "args": [], "extra": True},
])
def test_invalid_batch_rejects_before_any_tool_runs(invalid):
    with patch("model_tools.handle_function_call") as dispatch:
        result = run_parallel([
            {"tool": "read_file", "args": {"path": "valid-first"}}, invalid,
        ])
    assert result["status"] == "error", result
    assert result["tool_calls_made"] == 0
    dispatch.assert_not_called()


def test_batch_consumes_each_call_and_never_partially_exceeds_budget():
    seen = []

    def dispatch(name, args, **kwargs):
        seen.append(args["path"])
        return json.dumps({"content": args["path"]})

    calls = [{"tool": "read_file", "args": {"path": str(i)}} for i in range(3)]
    with patch("model_tools.handle_function_call", side_effect=dispatch):
        refused = run_parallel(calls, budget=2)
        accepted = run_parallel(calls[:2], budget=2)
    assert refused["status"] == "error", refused
    assert refused["tool_calls_made"] == 0
    assert accepted["tool_calls_made"] == 2
    assert json.loads(accepted["output"]) == [{"content": "0"}, {"content": "1"}]
    assert sorted(seen) == ["0", "1"]


def test_partial_tool_errors_remain_visible_and_next_write_keeps_order():
    finished = set()

    def dispatch(name, args, **kwargs):
        if name == "write_file":
            assert finished == {"ok", "broken"}
            return json.dumps({"written": True})
        finished.add(args["path"])
        if args["path"] == "broken":
            raise ValueError("read failed")
        return "plain\ntext"

    code = (
        "import json\nfrom hermes_tools import parallel, write_file\n"
        "result = parallel([{'tool': 'read_file', 'args': {'path': p}} "
        "for p in ['ok', 'broken']])\n"
        "print(json.dumps({'reads': result, 'write': write_file('out', 'x')}))"
    )
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict"}), \
            patch("model_tools.handle_function_call", side_effect=dispatch):
        result = json.loads(_execute_code_handler(
            {"code": code}, task_id="readonly-batch", enabled_tools=["read_file", "write_file"],
        ))
    assert result["status"] == "success", result
    output = json.loads(result["output"])
    assert output["reads"][0] == "plain\ntext"
    assert "read failed" in output["reads"][1]["error"]
    assert output["write"] == {"written": True}
    assert result["tool_calls_made"] == 3
    assert result["tool_errors"] == [{"tool": "read_file", "error": "read failed"}]


def test_parallel_reads_copy_current_cell_context_and_callbacks():
    import contextvars

    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.terminal_tool import _get_approval_callback, set_approval_callback

    marker = contextvars.ContextVar("parallel-test-marker")
    seen = []

    def dispatch(name, args, task_id=None, **kwargs):
        seen.append((marker.get(), task_id, _get_approval_callback()))
        return json.dumps({"content": args["path"]})

    token = set_current_session_key("parallel-cell-owner")
    try:
        with patch("model_tools.handle_function_call", side_effect=dispatch):
            for value in ["first", "second"]:
                def callback(value=value):
                    return value
                marker.set(value)
                set_approval_callback(callback)
                result = run_parallel([
                    {"tool": "read_file", "args": {"path": str(i)}} for i in range(2)
                ], task_id=value)
                assert result["status"] == "success", result
                assert all(row == (value, value, callback) for row in seen[-2:])
    finally:
        set_approval_callback(None)
        reset_current_session_key(token)


def test_parallel_reads_use_real_registered_file_tool(tmp_path):
    paths = [tmp_path / name for name in ["left.txt", "right.txt"]]
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
    result = run_parallel([
        {"tool": "read_file", "args": {"path": str(path)}} for path in paths
    ])
    assert result["status"] == "success", result
    outputs = json.loads(result["output"])
    assert "left" in outputs[0]["content"]
    assert "right" in outputs[1]["content"]
    assert result["tool_calls_made"] == 2


def test_remote_transport_cannot_use_the_local_batch_path():
    from tools.code_execution_rpc import _handle_rpc_request

    called = []
    result = json.loads(_handle_rpc_request(
        {"tool": "__parallel__", "args": {"calls": [
            {"tool": "read_file", "args": {"path": "x"}}]}},
        allowed_tools=frozenset({"read_file"}), tool_call_counter=[0], max_tool_calls=5,
        dispatch=lambda *a, **k: called.append(a), tool_call_log=[], call_start=0.0,
        where="remote sandbox",
    ))
    assert "only available over the local" in result["error"]
    assert called == []


def test_generated_module_exposes_parallel_only_for_local_transport():
    from tools.code_execution_tool import generate_hermes_tools_module

    local, remote = {}, {}
    exec(generate_hermes_tools_module(["read_file"], transport="uds"), local)
    exec(generate_hermes_tools_module(["read_file"], transport="file"), remote)
    assert callable(local["parallel"])
    assert "parallel" not in remote


def test_broken_batch_returns_an_error_instead_of_killing_the_rpc_loop():
    import tools.code_execution_parallel as module

    with patch("tools.code_execution_rpc._handle_rpc_request", side_effect=RuntimeError("boom")):
        result = json.loads(module.execute_parallel(
            {"calls": [{"tool": "read_file", "args": {"path": "x"}}]},
            allowed_tools=frozenset({"read_file"}), tool_call_counter=[0], max_tool_calls=5,
            dispatch=lambda *a, **k: "{}", tool_call_log=[], stop_event=None,
        ))
    assert "parallel batch failed" in result["error"]
