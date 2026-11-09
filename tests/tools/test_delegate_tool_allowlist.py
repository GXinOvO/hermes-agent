"""Focused runtime tests for explicit delegated-child tool allowlists."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import model_tools
from toolsets import agent_tool_surface
from tools.delegate_tool_allowlist import filter_tool_defs_by_allowlist


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_explicit_allowlist_surface_keeps_only_exact_underlying_names():
    agent = SimpleNamespace(
        platform="subagent",
        side_agent=True,
        connection_callback=None,
        allowed_tools=["mcp__server__read"],
        _oneshot_session=False,
        _delegation_inline=False,
    )
    surface = agent_tool_surface(agent, [_tool("mcp__server__read"), _tool("mcp__server__write")])
    assert [entry["function"]["name"] for entry in surface] == ["mcp__server__read"]


def test_explicit_empty_allowlist_filters_every_tool_definition():
    assert filter_tool_defs_by_allowlist([_tool("terminal"), _tool("read_file")], []) == []


def test_dispatch_rejects_forbidden_name_before_registry_handler():
    with patch.object(model_tools.registry, "dispatch") as dispatch:
        result = model_tools.handle_function_call(
            "terminal",
            {"command": "touch protected-target"},
            allowed_tools=[],
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    payload = json.loads(result)
    assert "not permitted" in str(payload["error"])
    dispatch.assert_not_called()


def test_dispatch_does_not_normalize_for_allowlist_matching():
    with patch.object(model_tools.registry, "dispatch") as dispatch:
        result = model_tools.handle_function_call(
            "process",
            {"action": "list"},
            allowed_tools=["process_manage"],
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    payload = json.loads(result)
    assert "not permitted" in str(payload["error"])
    dispatch.assert_not_called()