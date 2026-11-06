import json
from types import SimpleNamespace

import pytest

from tools.delegate_tool_child_run import _build_tool_trace
from tools.terminal_tool_result import finalize_foreground_result


@pytest.mark.parametrize("output,truncated", [
    ("short output", False),
    ("x" * 60_000, True),
    ("literal [OUTPUT TRUNCATED] from command", False),
], ids=["short", "oversized", "literal-marker"])
def test_finalized_terminal_envelope_preserves_truncation_in_trace(monkeypatch, output, truncated):
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 50_000)
    envelope = finalize_foreground_result(
        command="fixture-check", result={"output": output, "returncode": 0},
        env=SimpleNamespace(cwd="."), env_type="managed_modal", effective_task_id="fixture",
        task_id="fixture", session_id="fixture", session_key="fixture", workdir=".",
        command_cwd=None, approval_note=None,
    )
    trace = _build_tool_trace([
        {"role": "assistant", "tool_calls": [{"id": "check", "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "fixture-check"}),
        }}]},
        {"role": "tool", "tool_call_id": "check", "content": envelope},
    ])
    returned = json.loads(envelope)
    assert (returned["output"] != output) is truncated
    assert trace[0]["execution"] == {
        "outcome": "succeeded", "exit_code": 0, "output_truncated": truncated,
    }
