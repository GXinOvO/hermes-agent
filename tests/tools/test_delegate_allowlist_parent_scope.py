from types import SimpleNamespace
from unittest.mock import patch

from tools.delegate_tool_allowlist import validate_task_allowed_tools


def _parent():
    return SimpleNamespace(
        valid_tool_names={"read_file"},
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        enabled_toolsets=["file"], disabled_toolsets=[], _delegate_depth=0,
    )


def test_explicit_allowlist_refuses_when_parent_discovery_fails():
    with patch("model_tools.get_tool_definitions", side_effect=RuntimeError("discovery unavailable")):
        error = validate_task_allowed_tools([{"goal": "Read the authorized file", "allowed_tools": ["read_file"]}], _parent())
    assert error is not None
    assert "resolve" in error.lower() or "discovery" in error.lower()


def test_parent_disabled_tool_cannot_be_regranted_from_stale_snapshot():
    parent = _parent()
    parent.disabled_toolsets = ["file"]
    error = validate_task_allowed_tools([{"goal": "Read the authorized file", "allowed_tools": ["read_file"]}], parent)
    assert error is not None
    assert "disabled" in error or "unavailable" in error


def test_explicit_zero_tools_does_not_require_parent_discovery():
    with patch("model_tools.get_tool_definitions", side_effect=RuntimeError("discovery unavailable")):
        error = validate_task_allowed_tools([{"goal": "Answer without tools", "allowed_tools": []}], _parent())
    assert error is None
