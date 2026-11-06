import json
import socket
import threading

import pytest

from tools.code_execution_rpc import _rpc_server_loop
from tools.code_execution_tool import generate_hermes_tools_module


def test_local_batch_authentication_rejects_malformed_requests_without_losing_next_call(monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    seen, log, counter = [], [], [0]

    def dispatch(name, args):
        seen.append(args)
        return json.dumps(args)

    worker = threading.Thread(target=_rpc_server_loop, args=(
        listener, "auth-batch", log, counter, 5, frozenset({"read_file"}), stop, "right", dispatch,
    ), daemon=True)
    worker.start()
    connection = socket.create_connection(listener.getsockname(), timeout=5)
    connection.settimeout(2)
    stream = connection.makefile("rwb")
    requests = [
        ["not", "an", "object"],
        {"tool": "__parallel__", "args": {"calls": []}},
        {"token": "wrong", "tool": "__parallel__", "args": {"calls": []}},
        {"token": "\ud800", "tool": "__parallel__", "args": {"calls": []}},
        {"token": "right", "tool": [], "args": {}},
        {"token": "right", "tool": "__parallel__", "args": {"calls": [
            {"tool": "read_file", "args": {"path": "ok"}}]}},
        {"token": "right", "tool": "read_file", "args": {"path": "next"}},
    ]
    results = []
    try:
        for request in requests:
            stream.write((json.dumps(request) + "\n").encode())
            stream.flush()
            results.append(json.loads(stream.readline()))
    finally:
        stop.set()
        stream.close()
        connection.close()
        listener.close()
        worker.join(5)
    assert not worker.is_alive()
    assert all("error" in result for result in results[:5])
    assert results[5:] == [[{"path": "ok"}], {"path": "next"}]
    assert seen == [{"path": "ok"}, {"path": "next"}]
    assert counter == [2]


def test_parallel_worker_limit_and_result_slots_do_not_follow_finish_order(monkeypatch):
    from tests.tools import test_code_execution_result_framing as fixtures

    arrived = threading.Barrier(5, timeout=5)
    releases = [threading.Event() for _ in range(4)]
    finished = [threading.Event() for _ in range(4)]
    guard = threading.Lock()
    active, high = 0, 0
    seen = []

    def dispatch(name, args):
        nonlocal active, high
        index = int(args["path"])
        with guard:
            active += 1
            high = max(high, active)
            seen.append(index)
        try:
            if index < 4:
                arrived.wait()
                assert releases[index].wait(10)
                finished[index].set()
            return json.dumps({"index": index})
        finally:
            with guard:
                active -= 1

    result = []
    from tempfile import TemporaryDirectory
    from pathlib import Path

    with TemporaryDirectory() as directory:
        with fixtures.rpc_client("uds", Path(directory), monkeypatch, dispatch) as (module, log, counter):
            def batch():
                result.append(module["parallel"]([
                    {"tool": "read_file", "args": {"path": str(i)}} for i in range(8)
                ]))
            caller = threading.Thread(target=batch)
            caller.start()
            try:
                arrived.wait()
                assert high == 4
                for index in reversed(range(4)):
                    releases[index].set()
                    assert finished[index].wait(5)
            finally:
                for event in releases:
                    event.set()
                caller.join(10)
            assert not caller.is_alive()
            assert result == [[{"index": i} for i in range(8)]]
            assert high == 4
            assert sorted(seen) == list(range(8))
            assert counter == [8]
            assert len(log) == 8


@pytest.mark.parametrize("shape", [
    [], [{"tool": "read_file", "args": {"path": "x"}}] * 17,
    {"tool": "read_file", "args": {"path": "x"}},
    [{"tool": "read_file", "args": "not-an-object"}],
])
def test_invalid_parallel_batch_shapes_have_no_dispatch(shape):
    from tools.code_execution_rpc import _handle_rpc_request

    seen, counter, log = [], [0], []
    result = json.loads(_handle_rpc_request(
        {"tool": "__parallel__", "args": {"calls": shape}},
        allowed_tools=frozenset({"read_file"}), tool_call_counter=counter,
        max_tool_calls=50, dispatch=lambda *a: seen.append(a), tool_call_log=log,
        call_start=0.0, where="batch", allow_parallel=True,
    ))
    assert "error" in result
    assert seen == log == []
    assert counter == [0]
