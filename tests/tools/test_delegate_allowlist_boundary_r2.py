from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import model_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.inline_tool_executors import resolve_invoke_tool_executor
from tools.delegate_tool_allowlist import normalize_constructor_allowed_tools, parent_effective_tool_defs, validate_task_allowed_tools
from tools.registry import ToolRegistry
from toolsets import TOOLSETS


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def _parent(name, enabled):
    return SimpleNamespace(tools=[_tool(name)], valid_tool_names={name}, enabled_toolsets=enabled,
                           disabled_toolsets=[], _delegate_depth=0, allowed_tools=None)


@pytest.mark.parametrize("alias", list(model_tools._LEGACY_TOOL_ALIASES) + ["invoke_tool", "delegate", "delegate_tasks", "spawn_subagent"])
def test_exact_constructor_rejects_legacy_aliases(alias):
    with pytest.raises(ValueError, match="exact|canonical|alias"):
        normalize_constructor_allowed_tools([alias])


def test_final_registry_gate_rechecks_after_alias_mapping():
    with patch.object(model_tools.registry, "dispatch") as dispatch:
        result = model_tools.handle_function_call("process", {}, allowed_tools=["process"])
    assert "not permitted" in result
    dispatch.assert_not_called()


@pytest.mark.parametrize("alias", ["invoke_tool", "delegate_tasks", "spawn_subagent"])
def test_inline_alias_cannot_enter_an_unlisted_handler(alias):
    agent = SimpleNamespace(allowed_tools=[alias])
    with patch("agent.inline_tool_executors.resolve_invoke_tool_executor") as resolve:
        result = invoke_tool(agent, alias, {}, "unit")
    assert "not permitted" in result
    resolve.assert_not_called()
    assert resolve_invoke_tool_executor(agent, alias) is None


@pytest.mark.parametrize("enabled", [[], ["file"]])
def test_parent_snapshot_does_not_authorize_an_unselected_toolset(enabled, monkeypatch):
    name = "mcp__scope_r2__selected"
    registry = ToolRegistry()
    registry.register(name=name, toolset="mcp-scope-r2", schema=_tool(name)["function"],
                      handler=lambda args: "unused", check_fn=lambda: True)
    monkeypatch.setitem(TOOLSETS, "mcp-scope-r2", {"tools": [name]})
    with patch.object(model_tools, "registry", registry):
        error = validate_task_allowed_tools([{"allowed_tools": [name]}], _parent(name, enabled))
    assert error is not None
    assert "unknown" in error or "unavailable" in error


@pytest.mark.parametrize("check", [lambda: False, lambda: None, lambda: Mock(), None])
def test_discovery_miss_cannot_be_reauthorized_from_snapshot(check, monkeypatch):
    name = "mcp__scope_r2__missing"
    registry = ToolRegistry()
    registry.register(name=name, toolset="mcp-scope-r2", schema=_tool(name)["function"],
                      handler=lambda args: "unused", check_fn=check)
    monkeypatch.setitem(TOOLSETS, "mcp-scope-r2", {"tools": [name]})
    with patch.object(model_tools, "registry", registry), patch.object(model_tools, "get_tool_definitions", return_value=[]):
        assert validate_task_allowed_tools([{"allowed_tools": [name]}], _parent(name, ["mcp-scope-r2"])) is not None


def test_selected_deferred_tool_remains_authorizable(monkeypatch):
    name = "mcp__scope_r2__positive"
    registry = ToolRegistry()
    registry.register(name=name, toolset="mcp-scope-r2", schema=_tool(name)["function"],
                      handler=lambda args: "unused", check_fn=lambda: True)
    monkeypatch.setitem(TOOLSETS, "mcp-scope-r2", {"tools": [name]})
    with patch.object(model_tools, "registry", registry):
        assert validate_task_allowed_tools([{"allowed_tools": [name]}], _parent(name, ["mcp-scope-r2"])) is None


def test_context_provider_tool_uses_current_authoritative_schema():
    name = "context_unit_r2"
    schema = {**_tool(name)["function"], "description": "current provider schema"}
    parent = _parent(name, ["context_engine"])
    parent.context_compressor = SimpleNamespace(get_tool_schemas=lambda: [schema])
    with patch.object(model_tools, "get_tool_definitions", return_value=[]):
        result = parent_effective_tool_defs(parent)
    assert result == [{"type": "function", "function": schema}]
