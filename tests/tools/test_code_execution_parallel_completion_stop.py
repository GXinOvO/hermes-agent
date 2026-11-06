import json
import threading
from concurrent.futures import wait
from unittest.mock import patch

import pytest

from tools.code_execution_rpc import _handle_rpc_request
from tools.code_kernel import CellAuthority


@pytest.mark.parametrize("cancel_source", [None, "transport", "cell"])
def test_last_result_does_not_override_a_concurrent_stop(cancel_source, monkeypatch):
    authority = CellAuthority("completion-stop")
    stop = threading.Event()

    def finish_then_stop(futures, **kwargs):
        done, pending = wait(futures, timeout=5)
        assert not pending
        if cancel_source == "transport":
            stop.set()
        elif cancel_source == "cell":
            authority.retire()
        return done, pending

    monkeypatch.setattr("tools.code_execution_parallel.wait", finish_then_stop)
    with patch("model_tools.handle_function_call", return_value='{"content":"done"}') as dispatch:
        result = json.loads(_handle_rpc_request(
            {"tool": "__parallel__", "args": {"calls": [
                {"tool": "read_file", "args": {"path": "fixture"}},
            ]}},
            allowed_tools=frozenset({"read_file"}), tool_call_counter=authority.counter,
            max_tool_calls=1, dispatch=authority.dispatch, tool_call_log=authority.log,
            call_start=0.0, where="completion-stop", stop_event=stop, allow_parallel=True,
        ))
    dispatch.assert_called_once()
    assert authority.counter == [1]
    assert len(authority.log) == 1
    if cancel_source is None:
        assert result == [{"content": "done"}]
    else:
        assert isinstance(result, dict), result
        assert "stopped" in result.get("error", ""), result
