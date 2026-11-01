"""Generated callers round-trip transformed results over both RPC transports."""

import json
import os
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.code_execution_rpc import _rpc_poll_loop, _rpc_server_loop, _serialize_rpc_result
from tools.code_execution_tool import generate_hermes_tools_module


class ShellEnvironment:
    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        from pm.shell import bash

        shell = bash()
        assert shell is not None
        result = subprocess.run(
            [shell, "-c", command], cwd=cwd, timeout=timeout,
            env=dict(os.environ), input=stdin_data or "", capture_output=True,
            text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        return {"output": result.stdout}


@contextmanager
def rpc_client(transport, tmp_path, monkeypatch, dispatch, allowed=frozenset({"read_file"})):
    """Use the production transport loop and generated client, without an LLM."""
    token = "test-result-framing"
    monkeypatch.setenv("HERMES_RPC_TOKEN", token)
    stop = threading.Event()
    calls, count = [], [0]
    listener = None
    if transport == "uds":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        monkeypatch.setenv("HERMES_RPC_SOCKET", f"tcp://127.0.0.1:{listener.getsockname()[1]}")
        worker = threading.Thread(
            target=_rpc_server_loop,
            args=(listener, "framing-test", calls, count, 10, allowed, stop, token, dispatch),
            daemon=True,
        )
    else:
        rpc_dir = tmp_path / "rpc"
        rpc_dir.mkdir()
        monkeypatch.setenv("HERMES_RPC_DIR", str(rpc_dir))
        monkeypatch.setattr("model_tools.handle_function_call", lambda name, args, **kw: dispatch(name, args))
        worker = threading.Thread(
            target=_rpc_poll_loop,
            args=(ShellEnvironment(), str(rpc_dir), "framing-test", calls, count, 10, allowed, stop, token),
            daemon=True,
        )
    namespace = {}
    exec(generate_hermes_tools_module(list(allowed), transport=transport), namespace)
    worker.start()
    try:
        yield namespace, calls, count
    finally:
        stop.set()
        connection = namespace.get("_sock")
        if connection is not None:
            connection.close()
        if listener is not None:
            listener.close()
        worker.join(timeout=10)
        assert not worker.is_alive()


@pytest.mark.parametrize("payload", [{"content": "\udcff\n雪"}, "text\udcff\nwarning"])
def test_rpc_result_is_utf8_encodable_without_losing_surrogates(payload):
    wire = _serialize_rpc_result(json.dumps(payload))
    assert json.loads(wire.encode("utf-8")) == payload


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("transport", ["uds", "file"])
def test_transformed_result_framing_preserves_payload_and_next_call(transport, tmp_path, monkeypatch):
    # Hooks may return plain multiline text; pretty JSON must also stay one wire frame.
    text = "written\n\n⚠ warning: inspect this result\n" + "z" * 70000
    responses = iter([
        text,
        json.dumps({"content": "café\nnext", "total_lines": 2}, indent=2),
        json.dumps({"error": "read refused\nreason"}),
        json.dumps({"content": "\udcff\n雪"}),
        json.dumps("text\udcff\nwarning"),
    ])
    dispatched = []

    def dispatch(name, args):
        dispatched.append((name, args))
        return next(responses)

    with rpc_client(transport, tmp_path, monkeypatch, dispatch) as (namespace, calls, count):
        read_file = namespace["read_file"]
        assert read_file("text") == text
        assert read_file("json") == {"content": "café\nnext", "total_lines": 2}
        assert read_file("error") == {"error": "read refused\nreason"}
        assert calls[-1]["error"] == "read refused\nreason"
        assert read_file("surrogate-object") == {"content": "\udcff\n雪"}
        assert read_file("surrogate-text") == "text\udcff\nwarning"
        assert count == [5]
        assert len(dispatched) == len(calls) == 5


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("transport", ["uds", "file"])
def test_security_guidance_real_write_round_trip(transport, tmp_path, monkeypatch):
    import model_tools
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManifest

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("SECURITY_GUIDANCE_BLOCK", raising=False)
    monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "security-guidance"
    manager._load_plugin(PluginManifest(name="security-guidance", source="bundled", path=str(plugin_dir)))
    assert manager.has_hook("transform_tool_result")
    handle = model_tools.handle_function_call
    task_id = f"security-result-{transport}"

    def dispatch(name, args):
        return handle(name, args, task_id=task_id)

    target = tmp_path / "written.py"
    marker = "# Inert warning probe: " + "eval" + "( is never executed.\n"
    try:
        with rpc_client(transport, tmp_path, monkeypatch, dispatch,
                        allowed=frozenset({"write_file", "read_file", "patch"})) as (namespace, calls, count):
            wrote = namespace["write_file"](str(target), marker)
            assert wrote["verified"] is True
            assert wrote["bytes_written"] == len(marker.encode())
            assert "eval_injection" in wrote["security_guidance"]
            assert target.read_text() == marker
            namespace["read_file"](str(target))
            changed = namespace["patch"](str(target), "never executed", "still never executed")
            assert changed["success"] is True
            assert target.read_text() == marker.replace("never executed", "still never executed")
            assert count == [3]
            assert len(calls) == 3
        direct = json.loads(dispatch("write_file", {"path": str(tmp_path / "direct.py"), "content": marker}))
        assert direct["verified"] is True
        assert "eval_injection" in direct["security_guidance"]
    finally:
        manager.unload()


@pytest.mark.platforms("posix")
def test_execute_code_kernel_keeps_structured_warning_and_state(tmp_path, monkeypatch):
    from hermes_cli import plugins
    from hermes_cli.plugins import PluginManifest
    from tools.code_execution_tool import execute_code
    from tools.code_kernel import shutdown_all_kernels

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("SECURITY_GUIDANCE_BLOCK", raising=False)
    monkeypatch.delenv("SECURITY_GUIDANCE_DISABLE", raising=False)
    monkeypatch.setattr("tools.code_execution_tool._load_config", lambda: {"mode": "strict", "timeout": 30})
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "security-guidance"
    manager._load_plugin(PluginManifest(name="security-guidance", source="bundled", path=str(plugin_dir)))
    target = tmp_path / "kernel-write.py"
    marker = "# Inert kernel probe: " + "eval" + "( is never executed.\n"
    code = (
        "from hermes_tools import write_file\n"
        f"written = write_file({str(target)!r}, {marker!r})\n"
        "assert written['verified'] is True\n"
        "assert 'eval_injection' in written['security_guidance']\n"
        "print('write and warning verified')\n"
    )
    shutdown_all_kernels()
    try:
        first = json.loads(execute_code(code, task_id="guidance-kernel", enabled_tools=["write_file"]))
        assert first["status"] == "success", first
        assert first["tool_calls_made"] == 1
        assert "write and warning verified" in first["output"]
        assert target.read_text() == marker
        second = json.loads(execute_code("print(written['verified'])", task_id="guidance-kernel", enabled_tools=["write_file"]))
        assert second["status"] == "success", second
        assert second["kernel"]["reused"] is True
        assert second["output"].strip() == "True"
    finally:
        shutdown_all_kernels()
        manager.unload()
