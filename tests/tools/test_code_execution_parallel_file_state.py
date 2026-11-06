import json
from unittest.mock import patch

from tools.code_execution_tool import _execute_code_handler
from tools.code_kernel import shutdown_all_kernels


def test_parallel_paged_reads_preserve_the_native_write_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    path = tmp_path / "paged.txt"
    path.write_text("".join(f"row {i}\n" for i in range(2500)))
    replacement = "complete\n"
    calls = [{"tool": "read_file", "args": {"path": str(path), "offset": offset, "limit": 1250}}
             for offset in [1, 1251]]
    code = (
        "from hermes_tools import parallel, write_file\n"
        f"reads = parallel({calls!r})\n"
        "assert all('content' in result for result in reads), reads\n"
        f"print(write_file({str(path)!r}, {replacement!r}))"
    )
    try:
        with patch("tools.code_execution_tool._load_config", return_value={"mode": "strict"}):
            result = json.loads(_execute_code_handler(
                {"code": code}, task_id="paged-parallel", enabled_tools=["read_file", "write_file"],
            ))
        assert result["status"] == "success", result
        assert "tool_errors" not in result, result
        assert path.read_text() == replacement
        assert result["tool_calls_made"] == 3
    finally:
        shutdown_all_kernels()
