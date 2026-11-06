import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.code_execution_rpc import _rpc_server_loop
from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels


@pytest.mark.parametrize("budget", [1, 3, 2])
def test_reused_kernel_admits_batches_against_each_cells_current_budget(budget, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    code = (
        "from hermes_tools import parallel, read_file\n"
        "read_file('serial')\n"
        "print(parallel([{'tool':'read_file','args':{'path':str(i)}} for i in range(2)]))"
    )
    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "max_tool_calls": 1}):
            first = json.loads(_execute_code_handler(
                {"code": "retained = 42"}, task_id="changing-batch-budget", enabled_tools=["read_file"],
            ))
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict", "max_tool_calls": budget}), \
                patch("model_tools.handle_function_call", return_value="{}") as dispatch:
            result = json.loads(_execute_code_handler(
                {"code": "assert retained == 42\n" + code},
                task_id="changing-batch-budget", enabled_tools=["read_file"],
            ))
        assert first["status"] == "success", first
        assert result["kernel"]["reused"] is True
        assert result["tool_calls_made"] == (3 if budget == 3 else 1)
        assert dispatch.call_count == (3 if budget == 3 else 1)
        assert result["status"] == ("success" if budget == 3 else "error"), result
    finally:
        shutdown_all_kernels()


def test_executor_initialization_failure_keeps_the_same_rpc_connection_usable():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    count, log = [0], []
    worker = threading.Thread(target=_rpc_server_loop, args=(
        listener, "executor-failure", log, count, 5, frozenset({"read_file"}), stop,
        "fixture-token", lambda name, args: json.dumps({"content": args["path"]}),
    ), daemon=True)
    worker.start()
    connection = socket.create_connection(listener.getsockname(), timeout=5)
    stream = connection.makefile("rwb")
    connection.settimeout(2)
    requests = [
        {"tool": "__parallel__", "args": {"calls": [{"tool": "read_file", "args": {"path": "batch"}}]}},
        {"tool": "read_file", "args": {"path": "after-failure"}},
    ]
    results = []
    try:
        with patch("tools.code_execution_parallel.DaemonThreadPoolExecutor", side_effect=RuntimeError("pool unavailable")):
            for request in requests:
                request["token"] = "fixture-token"
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
    assert "pool unavailable" in results[0]["error"]
    assert results[1] == {"content": "after-failure"}
    assert count == [1]


def test_cancelled_batch_does_not_keep_a_real_host_process_alive(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = tmp_path / "batch_exit.py"
    script.write_text(
        "import sys, json, threading\n"
        f"sys.path[:0] = {list(dict.fromkeys([str(root)] + [p for p in sys.path if p and 'site-packages' in p]))!r}\n"
        "from tools.code_execution_parallel import execute_parallel\n"
        "stop, started, blocked = threading.Event(), threading.Event(), threading.Event()\n"
        "def dispatch(name, args):\n    started.set()\n    blocked.wait()\n    return '{}'\n"
        "def cancel():\n    assert started.wait(5)\n    stop.set()\n"
        "threading.Thread(target=cancel, daemon=True).start()\n"
        "result = execute_parallel({'calls':[{'tool':'read_file','args':{'path':'x'}}]}, "
        "allowed_tools=frozenset({'read_file'}), tool_call_counter=[0], max_tool_calls=1, "
        "dispatch=dispatch, tool_call_log=[], stop_event=stop)\n"
        "assert 'stopped' in json.loads(result)['error']\n"
        "print('main-exited', flush=True)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TZ", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(tmp_path / "home"),
               TMPDIR=str(tmp_path), HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-I", str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert "main-exited" in result.stdout
