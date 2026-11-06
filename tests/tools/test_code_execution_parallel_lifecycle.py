import json
import threading
from unittest.mock import patch

import pytest

from tools.code_execution_rpc import _handle_rpc_request
from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import CellAuthority, shutdown_all_kernels


@pytest.fixture(autouse=True)
def kernel_scope(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def test_explicit_empty_grant_refuses_raw_batch_before_dispatch():
    code = "from hermes_tools import _call\nprint(_call('__parallel__', {'calls': [{'tool': 'read_file', 'args': {'path': 'x'}}]}))"
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict"}), \
            patch("model_tools.handle_function_call", return_value='{"content":"forbidden"}') as dispatch:
        result = json.loads(_execute_code_handler({"code": code}, task_id="empty-grant", enabled_tools=[]))
    assert result["tool_calls_made"] == 0, result
    dispatch.assert_not_called()
    assert "only enabled" in result["output"]


def test_reused_kernel_uses_the_new_cells_budget():
    code = "from hermes_tools import read_file\nread_file('one')\nread_file('two')\n"
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "max_tool_calls": 5}), \
            patch("model_tools.handle_function_call", return_value="{}"):
        first = json.loads(_execute_code_handler({"code": code}, task_id="budget-cell", enabled_tools=["read_file"]))
    with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "max_tool_calls": 1}), \
            patch("model_tools.handle_function_call", return_value="{}") as dispatch:
        second = json.loads(_execute_code_handler({"code": code}, task_id="budget-cell", enabled_tools=["read_file"]))
    assert first["tool_calls_made"] == 2
    assert second["kernel"]["reused"] is True
    assert second["tool_calls_made"] == 1, second
    assert dispatch.call_count == 1


def test_stopped_batch_does_not_wait_for_running_calls_or_start_queued_calls():
    stop = threading.Event()
    release = threading.Event()
    entered = threading.Barrier(5, timeout=5)
    done = threading.Event()
    seen, log, counter, result = [], [], [0], []
    lock = threading.Lock()

    def dispatch(name, args):
        with lock:
            seen.append(args["path"])
        entered.wait()
        assert release.wait(10)
        return "{}"

    def run():
        try:
            result.append(json.loads(_handle_rpc_request(
                {"tool": "__parallel__", "args": {"calls": [
                    {"tool": "read_file", "args": {"path": str(i)}} for i in range(8)]}},
                allowed_tools=frozenset({"read_file"}), tool_call_counter=counter, max_tool_calls=8,
                dispatch=dispatch, tool_call_log=log, call_start=0.0, where="test",
                stop_event=stop, allow_parallel=True,
            )))
        finally:
            done.set()

    coordinator = threading.Thread(target=run)
    coordinator.start()
    try:
        entered.wait()
        stop.set()
        assert done.wait(2), "cancelled batch waited for blocked tool I/O"
        assert counter == [4]
        assert len(seen) == 4
        assert "stopped" in result[0]["error"]
    finally:
        stop.set()
        release.set()
        coordinator.join(10)
    assert not coordinator.is_alive()


def test_retired_authority_refuses_without_budget_or_log_changes():
    authority = CellAuthority("retired")
    authority.retire()
    count, log = [0], []
    result = json.loads(_handle_rpc_request(
        {"tool": "__parallel__", "args": {"calls": [
            {"tool": "read_file", "args": {"path": "x"}}]}},
        allowed_tools=frozenset({"read_file"}), tool_call_counter=count, max_tool_calls=3,
        dispatch=authority.dispatch, tool_call_log=log, call_start=0.0, where="test",
        allow_parallel=True,
    ))
    assert count == [0], result
    assert log == []
    assert "No active execute_code cell" in str(result)


def test_batch_disconnect_is_not_replayed_but_next_call_can_reconnect(monkeypatch):
    from tools.code_execution_tool import generate_hermes_tools_module

    monkeypatch.setenv("HERMES_RPC_PERSISTENT", "1")
    monkeypatch.setenv("HERMES_RPC_TOKEN", "fixture")
    module = {}
    exec(generate_hermes_tools_module(["read_file"]), module)

    class Connection:
        def __init__(self):
            self.sent = []
            self.closed = False

        def sendall(self, request):
            self.sent.append(json.loads(request))

        def recv(self, _size) -> bytes:
            return b""

        def close(self):
            self.closed = True

    first = Connection()
    connects = []

    def connect():
        connects.append(True)
        module["_sock"] = first
        return first

    module["_connect"] = connect
    with pytest.raises(RuntimeError, match="disconnected"):
        module["parallel"]([{"tool": "read_file", "args": {"path": "x"}}])
    assert len(connects) == len(first.sent) == 1
    assert first.closed
    assert module["_sock"] is None

    class Recovered(Connection):
        def recv(self, _size):
            return b'{"content":"next"}\n'

    recovered = Recovered()
    module["_connect"] = lambda: recovered
    assert module["read_file"]("next") == {"content": "next"}
    assert recovered.sent[0]["tool"] == "read_file"


def test_queued_old_cell_reads_do_not_charge_a_new_cell():
    from tools.code_execution_parallel import execute_parallel

    old = CellAuthority("old", max_tool_calls=8)
    new = CellAuthority("new", max_tool_calls=1)
    started = threading.Barrier(5, timeout=5)
    release = threading.Event()
    finished = threading.Event()
    observed, result = [], []
    held_threads = []
    guard = threading.Lock()

    def dispatch(name, args, task_id=None):
        with guard:
            observed.append(task_id)
            if task_id == "old":
                held_threads.append(threading.current_thread())
        if task_id == "old":
            started.wait()
            assert release.wait(10)
        return "{}"

    def batch():
        try:
            result.append(json.loads(execute_parallel(
                {"calls": [{"tool": "read_file", "args": {"path": str(i)}} for i in range(8)]},
                allowed_tools=frozenset({"read_file"}), tool_call_counter=old.counter,
                max_tool_calls=old.max_tool_calls, dispatch=old.dispatch,
                tool_call_log=old.log, stop_event=None,
            )))
        finally:
            finished.set()

    with patch("model_tools.handle_function_call", side_effect=dispatch):
        coordinator = threading.Thread(target=batch)
        coordinator.start()
        try:
            started.wait()
            old.retire()
            assert finished.wait(2)
            next_result = json.loads(_handle_rpc_request(
                {"tool": "read_file", "args": {"path": "new"}},
                allowed_tools=frozenset({"read_file"}), tool_call_counter=new.counter,
                max_tool_calls=new.max_tool_calls, dispatch=new.dispatch,
                tool_call_log=new.log, call_start=0.0, where="test",
            ))
            assert next_result == {}
            assert new.counter == [1]
            assert len(new.log) == 1
        finally:
            release.set()
            coordinator.join(10)
            for worker in held_threads:
                worker.join(2)
        assert result and "stopped" in result[0]["error"]
        assert old.counter == [4]
        assert observed.count("old") == 4
        assert observed.count("new") == 1
        assert new.counter == [1]
        assert len(new.log) == 1
