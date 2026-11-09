import json
from types import SimpleNamespace
from unittest.mock import patch

from run_agent import AIAgent
from tools import delegate_tool as delegation
from tools.delegate_tool_allowlist import validate_task_allowed_tools
from tools.mcp_tool_agent import persist_agent_tool_names, refresh_agent_mcp_tools, restore_agent_tool_prefix


def _parent(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 131072\n", encoding="utf-8")
    return AIAgent(
        api_key="unit-no-network", base_url="http://127.0.0.1:9/v1", provider="custom",
        api_mode="chat_completions", model="unit-allowlist", platform="cli", quiet_mode=True,
        skip_context_files=True, skip_memory=True, skip_background_review=True,
        enabled_toolsets=["file", "terminal"], max_iterations=4,
    )


def _child(parent, allowed):
    return delegation._build_child_agent(
        task_index=0, goal="Read only the authorized fixture", context=None, toolsets=None, model=None,
        max_iterations=4, task_count=1, parent_agent=parent, allowed_tools=allowed,
    )


def test_real_child_restriction_and_native_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(delegation, "_load_config", dict)
    parent = _parent(tmp_path)
    readable = tmp_path / "input.txt"
    readable.write_text("real-handler-read-marker", encoding="utf-8")
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged", encoding="utf-8")
    assert validate_task_allowed_tools([{"goal": "Read fixture", "allowed_tools": ["read_file"]}], parent) is None
    child = _child(parent, ["read_file"])
    sibling = _child(parent, [])
    try:
        assert child.valid_tool_names == {"read_file"}
        assert sibling.tools == []
        result = child._invoke_tool("read_file", {"path": str(readable)}, "read-fixture")
        assert "real-handler-read-marker" in str(result)
        for name, args in [
            ("write_file", {"path": str(protected), "content": "bad"}),
            ("terminal", {"command": "false"}),
            ("mcp__unit__write", {}),
            ("tool_call", {"calls": [{"name": "write_file", "arguments": {"path": str(protected), "content": "bad"}}]}),
        ]:
            with patch("model_tools.registry.dispatch") as dispatch:
                blocked = child._invoke_tool(name, args, "deny-fixture")
                assert "not permitted" in str(blocked)
                dispatch.assert_not_called()
        assert protected.read_text(encoding="utf-8") == "unchanged"
        assert "write_file" in parent.valid_tool_names
        assert sibling.valid_tool_names == set()
        refresh_agent_mcp_tools(sibling)
        assert sibling.tools == []
        pins = []
        sibling._session_db = SimpleNamespace(update_session_tool_names=lambda sid, pin: pins.append(pin))
        persist_agent_tool_names(sibling)
        sibling._session_db = None
        assert pins[-1]["allowed_tools"] == []
        fresh = SimpleNamespace(tools=list(parent.tools), valid_tool_names=set(parent.valid_tool_names),
                                enabled_toolsets=["file", "terminal"], disabled_toolsets=[])
        restore_agent_tool_prefix(fresh, pins[-1])
        assert fresh.tools == []
        assert fresh.allowed_tools == []
    finally:
        child.close()
        sibling.close()
        parent.close()


def test_explicit_mcp_tool_uses_exact_native_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(delegation, "_load_config", dict)
    import model_tools
    from toolsets import TOOLSETS
    name = "mcp__unit__read"
    entered = []
    model_tools.registry.register(
        name=name, toolset="mcp-unit", schema={"name": name, "description": "Unit read", "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: entered.append(True) or "unit-mcp-read", check_fn=lambda: True,
    )
    monkeypatch.setitem(TOOLSETS, "mcp-unit", {"description": "Unit MCP", "tools": [name]})
    parent = _parent(tmp_path)
    parent.enabled_toolsets.append("mcp-unit")
    refresh_agent_mcp_tools(parent, preserve_prefix=False)
    assert validate_task_allowed_tools([{"goal": "Read MCP fixture", "allowed_tools": [name]}], parent) is None
    child = _child(parent, [name])
    try:
        assert child.valid_tool_names == {name}
        assert "tool_call" not in child.valid_tool_names
        assert "unit-mcp-read" in str(child._invoke_tool(name, {}, "mcp-fixture"))
        assert entered == [True]
    finally:
        child.close()
        parent.close()
