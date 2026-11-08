import json
import threading
from unittest.mock import patch

import pytest

from hermes_cli.plugins import PluginManager
from tools.approval_context import (
    reset_current_observability_context,
    reset_current_session_key,
    set_current_observability_context,
    set_current_session_key,
)
from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels
from tools.thread_context import propagate_context_to_thread


@pytest.fixture(autouse=True)
def kernel_scope(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def test_independent_rpc_reads_reach_distinct_pre_and_post_hook_identities(monkeypatch):
    manager = PluginManager()
    release = threading.Event()
    all_entered = threading.Event()
    guard = threading.Lock()
    pre, executed, post = [], [], []

    def pre_hook(**kwargs):
        with guard:
            pre.append(kwargs)
            if len(pre) == 3:
                all_entered.set()
        assert release.wait(10)

    def post_hook(**kwargs):
        with guard:
            post.append(kwargs)

    def execute(name, args, original, ids, **kwargs):
        with guard:
            executed.append(ids)
        return json.dumps({"content": args["path"]})

    manager._hooks["pre_tool_call"] = [pre_hook]
    manager._hooks["post_tool_call"] = [post_hook]
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", manager.invoke_hook)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: bool(manager._hooks.get(name)))
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 10)
    monkeypatch.setattr("model_tools._execute_tool", execute)
    outcome = []
    code = (
        "import json\nfrom hermes_tools import parallel\n"
        "print(json.dumps(parallel([{'tool':'read_file','args':{'path':p}} "
        "for p in ['one','two','three']])))"
    )
    owner = set_current_session_key("identity-owner")
    scope = set_current_observability_context(
        turn_id="turn-one", tool_call_id="outer-execute", session_id="session-one",
    )
    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "timeout": 20}):
            worker = threading.Thread(target=propagate_context_to_thread(
                lambda: outcome.append(json.loads(_execute_code_handler(
                    {"code": code}, task_id="identity-task", enabled_tools=["read_file"],
                )))
            ))
            worker.start()
            try:
                overlapped = all_entered.wait(5)
            finally:
                release.set()
                worker.join(15)
    finally:
        reset_current_observability_context(scope)
        reset_current_session_key(owner)

    assert not worker.is_alive()
    assert overlapped, (pre, outcome)
    assert len(outcome) == 1 and outcome[0]["status"] == "success", outcome
    assert json.loads(outcome[0]["output"]) == [
        {"content": "one"}, {"content": "two"}, {"content": "three"},
    ]
    assert outcome[0]["tool_calls_made"] == 3
    ids = {row["tool_call_id"] for row in pre}
    assert len(ids) == 3 and "" not in ids and "outer-execute" not in ids
    assert {row.tool_call_id for row in executed} == ids
    assert {row["tool_call_id"] for row in post} == ids
    assert all(row["turn_id"] == "turn-one" and row["session_id"] == "session-one" for row in pre + post)
    assert all(row.task_id == "identity-task" for row in executed)
    assert not manager._hook_running_callbacks
    assert not manager._hook_abandoned
    assert not manager._hook_timeout_suppressed_until


def test_nonpersistent_dispatch_preserves_child_identity_to_plugin_guard(monkeypatch):
    import time

    from tools.code_execution_rpc import _default_dispatch, _handle_rpc_request

    seen = []
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, args, **kwargs: seen.append(kwargs) or "{}")
    result = json.loads(_handle_rpc_request(
        {"tool": "__parallel__", "args": {"calls": [
            {"tool": "read_file", "args": {"path": "a"}},
            {"tool": "read_file", "args": {"path": "b"}},
        ]}},
        allowed_tools=frozenset({"read_file"}), tool_call_counter=[0], max_tool_calls=2,
        dispatch=_default_dispatch("fallback-task"), tool_call_log=[],
        call_start=time.monotonic(), where="test", allow_parallel=True,
    ))
    assert result == [{}, {}]
    assert len({row.get("tool_call_id") for row in seen}) == 2
    assert all(row.get("tool_call_id") and row["task_id"] == "fallback-task" for row in seen)


@pytest.mark.parametrize("policy", ["block", "approval-denied", "timeout"])
def test_child_identity_does_not_bypass_policy_or_timeout(policy, monkeypatch):
    manager = PluginManager()
    hold = threading.Event()
    entered = threading.Event()
    done = threading.Event()
    executed = []

    def guard(**kwargs):
        if kwargs["args"]["path"] != "restricted":
            return None
        if policy == "timeout":
            entered.set()
            try:
                assert hold.wait(10)
            finally:
                done.set()
            return None
        return {"action": "block" if policy == "block" else "approve", "message": "policy-refusal"}

    manager._hooks["pre_tool_call"] = [guard]
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", manager.invoke_hook)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.1 if policy == "timeout" else 5)
    monkeypatch.setattr("tools.approval.request_tool_approval", lambda *args, **kwargs: {"approved": False, "message": "denied"})
    monkeypatch.setattr("model_tools._execute_tool", lambda name, args, *a, **kw: executed.append(args["path"]) or json.dumps({"content": args["path"]}))
    code = (
        "import json\nfrom hermes_tools import parallel\n"
        "print(json.dumps(parallel([{'tool':'read_file','args':{'path':p}} "
        "for p in ['restricted','allowed']])))"
    )
    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "timeout": 15}):
            result = json.loads(_execute_code_handler({"code": code}, task_id="policy-test", enabled_tools=["read_file"]))
        outputs = json.loads(result["output"])
        assert "error" in outputs[0]
        assert "restricted" not in executed
        assert result["tool_calls_made"] == 2
        assert result["tool_errors"]
        if policy == "timeout":
            assert entered.is_set()
            assert manager._hook_abandoned
            assert manager._hook_timeout_suppressed_until
    finally:
        hold.set()
        if policy == "timeout" and entered.is_set():
            assert done.wait(5)


def test_reused_kernel_assigns_new_ids_and_keeps_current_turn_without_scope_leak(monkeypatch):
    from tools.approval_context import get_current_observability_context
    from tools.code_execution_rpc import _parallel_call_id

    seen = []
    def dispatch(name, args, **kwargs):
        seen.append((kwargs, get_current_observability_context()))
        return "{}"

    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    owner = set_current_session_key("same-owner")
    code = "from hermes_tools import parallel\nparallel([{'tool':'read_file','args':{'path':'x'}}])"
    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict"}):
            for turn in ["first-turn", "second-turn"]:
                token = set_current_observability_context(turn_id=turn, tool_call_id="outer-call", session_id="same-session")
                try:
                    result = json.loads(_execute_code_handler({"code": code}, task_id=turn, enabled_tools=["read_file"]))
                    assert result["status"] == "success", result
                    assert get_current_observability_context()["tool_call_id"] == "outer-call"
                    assert _parallel_call_id.get() == ""
                finally:
                    reset_current_observability_context(token)
    finally:
        reset_current_session_key(owner)
    assert len({row[0]["tool_call_id"] for row in seen}) == 2
    for (kwargs, scope), turn in zip(seen, ["first-turn", "second-turn"]):
        assert kwargs["turn_id"] == scope["turn_id"] == turn
        assert kwargs["tool_call_id"] == scope["tool_call_id"]
        assert kwargs["session_id"] == scope["session_id"] == "same-session"


def test_repeated_operation_identity_still_fails_closed(monkeypatch):
    from tools.code_kernel import CellAuthority

    manager = PluginManager()
    entered, release = threading.Event(), threading.Event()
    starts, executed, result = [], [], []

    def pre_hook(**kwargs):
        starts.append(kwargs["tool_call_id"])
        entered.set()
        assert release.wait(10)

    manager._hooks["pre_tool_call"] = [pre_hook]
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", manager.invoke_hook)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 10)
    monkeypatch.setattr("model_tools._execute_tool", lambda *a, **kw: executed.append(1) or "{}")
    authority = CellAuthority("duplicate-task")
    worker = threading.Thread(target=lambda: result.append(
        authority.ctx.copy().run(authority._invoke, "read_file", {"path": "same"}, "same-operation")
    ))
    worker.start()
    try:
        assert entered.wait(5)
        duplicate = json.loads(authority.ctx.copy().run(
            authority._invoke, "read_file", {"path": "same"}, "same-operation",
        ))
        assert "error" in duplicate
        assert starts == ["same-operation"]
        assert executed == []
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert len(executed) == 1 and result == ["{}"]


def test_registered_file_reads_pass_overlapping_policy_and_transform_hooks(tmp_path, monkeypatch):
    manager = PluginManager()
    arrived = threading.Barrier(3, timeout=5)
    pre, transformed = [], []
    guard = threading.Lock()

    def pre_hook(**kwargs):
        with guard:
            pre.append(kwargs["tool_call_id"])
        arrived.wait()

    def transform(**kwargs):
        with guard:
            transformed.append(kwargs["tool_call_id"])

    manager._hooks["pre_tool_call"] = [pre_hook]
    manager._hooks["transform_tool_result"] = [transform]
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", manager.invoke_hook)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: bool(manager._hooks.get(name)))
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 10)
    paths = [tmp_path / f"{i}.txt" for i in range(3)]
    for index, path in enumerate(paths):
        path.write_text(f"registered-content-{index}", encoding="utf-8")
    code = (
        "import json\nfrom hermes_tools import parallel\n"
        "print(json.dumps(parallel([{'tool':'read_file','args':{'path':p}} "
        f"for p in {[str(path) for path in paths]!r}])))"
    )
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "timeout": 20}):
        result = json.loads(_execute_code_handler({"code": code}, task_id="registered-batch", enabled_tools=["read_file"]))
    assert result["status"] == "success", result
    outputs = json.loads(result["output"])
    assert all(f"registered-content-{index}" in output["content"] for index, output in enumerate(outputs))
    assert len(set(pre)) == 3 and set(transformed) == set(pre)
    assert result["tool_calls_made"] == 3
