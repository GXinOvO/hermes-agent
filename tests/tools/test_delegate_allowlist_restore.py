from types import SimpleNamespace

import pytest

from tools.mcp_tool_agent import persist_agent_tool_names, restore_agent_tool_prefix


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_tool_pin_preserves_explicit_zero_for_fresh_receiver():
    captured = []
    db = SimpleNamespace(update_session_tool_names=lambda session, pin: captured.append(pin))
    original = SimpleNamespace(tools=[], valid_tool_names=set(), allowed_tools=[],
                               _session_db=db, session_id="restricted-session")
    persist_agent_tool_names(original)
    assert captured[0]["allowed_tools"] == []
    fresh = SimpleNamespace(tools=[_tool("read_file")], valid_tool_names={"read_file"},
                            enabled_toolsets=["file"], disabled_toolsets=[], session_id="restricted-session")
    restore_agent_tool_prefix(fresh, captured[0])
    assert fresh.tools == []
    assert fresh.valid_tool_names == set()
    assert fresh.allowed_tools == []


def test_malformed_saved_restriction_leaves_no_tools_even_if_caller_recovers():
    fresh = SimpleNamespace(tools=[_tool("terminal")], valid_tool_names={"terminal"})
    with pytest.raises(ValueError):
        restore_agent_tool_prefix(fresh, {"version": "old", "tools": [], "allowed_tools": "terminal"})
    assert fresh.tools == []
    assert fresh.allowed_tools == []
    assert fresh.valid_tool_names == set()


def test_restore_intersects_new_restriction_with_saved_restriction():
    fresh = SimpleNamespace(tools=[_tool("read_file"), _tool("write_file")],
                            valid_tool_names={"read_file", "write_file"},
                            enabled_toolsets=["file"], disabled_toolsets=[],
                            allowed_tools=["write_file"])
    saved = {"version": "old-code", "tools": [_tool("read_file"), _tool("write_file")],
             "allowed_tools": ["read_file"]}
    restore_agent_tool_prefix(fresh, saved)
    assert fresh.tools == []
    assert fresh.allowed_tools == []
