import json
from types import SimpleNamespace

import pytest

from tools.delegate_tool_child_run import _build_tool_trace
from tools.environments.base_output import _BoundedOutputCollector, _finalize_wait_result
from tools.terminal_tool_result import finalize_foreground_result


@pytest.mark.parametrize("failure,output,suffix,truncated", [
    ("open", "short output", "", False),
    ("open", "literal [OUTPUT TRUNCATED] from command", "", False),
    ("directory", "x" * 2_000, "", True),
    ("open", "x" * 2_000, "", True),
    ("no-path", "x" * 2_000, "", True),
    ("open", "x" * 990, "\n[Command interrupted]", True),
], ids=["short", "literal-marker", "directory-fails", "open-fails", "no-path", "status-suffix"])
def test_collector_truncation_survives_without_a_spill(
    monkeypatch, tmp_path, failure, output, suffix, truncated,
):
    def denied(*args, **kwargs):
        raise OSError("fixture spill failure")

    if failure != "no-path":
        target = "ensure_spill_dir" if failure == "directory" else "open_exclusive"
        monkeypatch.setattr("tools.spill_safety." + target, denied)
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 1_000)
    path = None if failure == "no-path" else tmp_path / "spill" / "output.log"
    collector = _BoundedOutputCollector(1_000, spill_path=path)
    collector.append(output)
    raw = _finalize_wait_result(collector, collector.render(suffix=suffix), 130 if suffix else 0)
    assert "full_output_path" not in raw
    assert (raw["output"] != output + suffix) is truncated
    assert len(raw["output"]) <= 1_000
    envelope = finalize_foreground_result(
        command="fixture-check", result=raw, env=SimpleNamespace(cwd="."), env_type="local",
        effective_task_id="fixture", task_id="fixture", session_id="fixture", session_key="fixture",
        workdir=".", command_cwd=None, approval_note=None,
    )
    trace = _build_tool_trace([
        {"role": "assistant", "tool_calls": [{"id": "check", "type": "function", "function": {
            "name": "terminal", "arguments": "{}",
        }}]},
        {"role": "tool", "tool_call_id": "check", "content": envelope},
    ])
    assert bool(raw.get("truncated")) is truncated
    assert bool(json.loads(envelope).get("truncated")) is truncated
    assert trace[0]["execution"]["output_truncated"] is truncated


def test_terminal_tool_reports_truncation_when_spill_creation_fails(monkeypatch):
    from tools.terminal_tool import terminal_tool

    def denied(*args, **kwargs):
        raise OSError("fixture spill failure")

    monkeypatch.setattr("tools.spill_safety.open_exclusive", denied)
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 1_000)
    envelope = terminal_tool("python3 -c \"print('x' * 2_000)\"", task_id="failed-spill")
    result = json.loads(envelope)
    assert result["exit_code"] == 0
    assert "full_output_path" not in result
    assert result.get("truncated") is True
    trace = _build_tool_trace([
        {"role": "assistant", "tool_calls": [{"id": "check", "type": "function", "function": {
            "name": "terminal", "arguments": "{}",
        }}]},
        {"role": "tool", "tool_call_id": "check", "content": envelope},
    ])
    assert trace[0]["execution"]["output_truncated"] is True
