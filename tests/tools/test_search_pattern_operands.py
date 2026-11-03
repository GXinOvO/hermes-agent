"""Content regex operands must not become command-line options."""

import json

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from tools.file_tools import search_tool


@pytest.mark.parametrize("engine", ["rg-native", "rg-shell", "grep", "grep-pruned"])
@pytest.mark.parametrize("output_mode", ["content", "files_only", "count"])
def test_dash_leading_regex_is_data(tmp_path, monkeypatch, engine, output_mode):
    root = tmp_path / (".hidden-root" if engine == "grep-pruned" else "project")
    root.mkdir()
    source = root / "options.txt"
    source.write_text("--alpha=ready\n--beta=done\nordinary control\n")
    (root / "unrelated.txt").write_text("not a matching option\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))
    if engine.startswith("grep"):
        monkeypatch.setattr(ops, "_has_command", lambda command: command == "grep")
    elif engine == "rg-shell":
        monkeypatch.setattr(ops, "_native_read_enabled", lambda: False)

    control = ops.search("ordinary control", path=str(root), target="content")
    assert control.error is None
    assert control.total_count == 1
    result = ops.search("--alpha|--beta", path=str(root), target="content",
                        file_glob="*.txt", output_mode=output_mode)
    assert result.error is None, result.error
    if output_mode == "content":
        assert [(match.line_number, match.content) for match in result.matches] == [
            (1, "--alpha=ready"), (2, "--beta=done")]
    elif output_mode == "files_only":
        assert result.files == [str(source)]
    else:
        expected = {str(source): 2}
        if engine.startswith("grep"):
            expected[str(root / "unrelated.txt")] = 0
        assert result.counts == expected


def test_dash_leading_zero_match_probe_keeps_case_hint(tmp_path):
    source = tmp_path / "options.txt"
    source.write_text("--ALPHA=ready\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    hint = ops._zero_match_probe("--alpha", str(tmp_path), "*.txt")
    assert hint is not None
    assert "case-insensitive" in hint
    assert source.name in hint


def test_public_search_preserves_option_like_pattern(tmp_path):
    source = tmp_path / "options.txt"
    source.write_text("--help\n--version\nordinary\n")
    result = json.loads(search_tool("--help", path=str(source), task_id="dash-public"))
    assert "error" not in result, result
    assert result["total_count"] == 1
    assert result["matches"][0]["content"] == "--help"


def test_shell_metacharacters_in_pattern_remain_data(tmp_path, monkeypatch):
    source = tmp_path / "options.txt"
    source.write_text("--alpha\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    monkeypatch.setattr(ops, "_native_read_enabled", lambda: False)
    result = ops.search("--alpha'; touch INJECTED; #", path=str(source), target="content")
    assert result.error is None, result.error
    assert result.total_count == 0
    assert not (tmp_path / "INJECTED").exists()


def test_malformed_dash_leading_regex_still_errors(tmp_path):
    source = tmp_path / "options.txt"
    source.write_text("--alpha\n")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)), cwd=str(tmp_path))
    result = ops.search("--[", path=str(source), target="content")
    assert result.error is not None
    assert "regex parse error" in result.error
