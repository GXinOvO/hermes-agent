"""Exact delegated-child tool selection and invocation policy."""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

from tools.tool_search_catalog import BRIDGE_TOOL_NAMES
from tools.delegate_tool_scope import parent_effective_tool_defs

logger = logging.getLogger(__name__)
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", re.ASCII)


def explicit_allowed_tools(value: Any) -> Optional[list[str]]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return None


def normalize_constructor_allowed_tools(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if error := _validate_value(0, value):
        raise ValueError(error)
    return list(value)


def restore_saved_restriction(agent: Any, saved: Any) -> bool:
    if not isinstance(saved, dict) or "allowed_tools" not in saved:
        return False
    allowed = saved["allowed_tools"]
    if error := _validate_value(0, allowed):
        agent.allowed_tools, agent.tools, agent.valid_tool_names = [], [], set()
        raise ValueError(error)
    current = explicit_allowed_tools(getattr(agent, "allowed_tools", None))
    agent.allowed_tools = [name for name in allowed if current is None or name in current]
    before = agent.tools
    agent.tools = filter_tool_defs_by_allowlist(before, agent.allowed_tools)
    agent.valid_tool_names = _tool_names(agent.tools)
    return before != agent.tools



def effective_child_role(parent_agent: Any) -> str:
    from tools.delegate_tool_config import _get_max_spawn_depth, _get_orchestrator_enabled

    depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    return "orchestrator" if _get_orchestrator_enabled() and depth < _get_max_spawn_depth() else "leaf"


def role_blocked_tool_names(role: str) -> frozenset[str]:
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS
    from toolsets import resolve_toolset

    blocked = set(DELEGATE_BLOCKED_TOOLS) | set(resolve_toolset("kanban", include_registry=False))
    if role == "orchestrator":
        blocked.discard("delegate_task")
    return frozenset(blocked)


def _tool_names(definitions: Iterable[dict]) -> set[str]:
    return {tool["function"]["name"] for tool in definitions}


def _selection(agent: Any) -> tuple:
    enabled = getattr(agent, "enabled_toolsets", None)
    disabled = getattr(agent, "disabled_toolsets", None)
    return (
        list(enabled) if isinstance(enabled, (list, tuple, set, frozenset)) else None,
        list(disabled) if isinstance(disabled, (list, tuple, set, frozenset)) else None,
    )



def _validate_value(index: int, raw: Any) -> Optional[str]:
    if not isinstance(raw, list):
        return f"Task {index}: allowed_tools must be an array of exact tool-name strings."
    for position, name in enumerate(raw):
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            return f"Task {index}: allowed_tools[{position}] must be an exact tool name; wildcards, whitespace and normalization are not supported."
        from tools.delegate_tool_scope import exact_tool_name
        if not exact_tool_name(name):
            return f"Task {index} allowed_tools requires exact canonical tool names; alias {name!r} is not accepted"
        if name in BRIDGE_TOOL_NAMES:
            return f"Task {index}: allowed_tools name {name!r} is a Tool Search bridge; specify the exact underlying native/MCP tool name."
    return None


def validate_task_allowed_tools(task_list: list[dict[str, Any]], parent_agent: Any) -> Optional[str]:
    explicit = [(i, task["allowed_tools"]) for i, task in enumerate(task_list) if "allowed_tools" in task]
    for index, allowed in explicit:
        if error := _validate_value(index, allowed):
            return error
    if not any(allowed for _, allowed in explicit):
        return None
    try:
        definitions = parent_effective_tool_defs(parent_agent)
        return _validate_parent_surface(explicit, parent_agent, definitions)
    except Exception as exc:
        logger.exception("Cannot resolve parent tools for explicit child restriction")
        return f"Cannot resolve parent tool surface ({type(exc).__name__}); no child was started."


def _validate_parent_surface(explicit: list[tuple], parent_agent: Any, definitions: list[dict]) -> Optional[str]:
    from tools.registry import registry
    import model_tools

    parent_names = _tool_names(definitions)
    role = effective_child_role(parent_agent)
    blocked = role_blocked_tool_names(role)
    enabled, disabled = _selection(parent_agent)
    selected = set(model_tools._select_tool_names(enabled, disabled, quiet_mode=True))
    for index, allowed in explicit:
        for name in allowed:
            if name in blocked:
                return f"Task {index}: allowed_tools name {name!r} is blocked for child role {role!r}."
            if name not in parent_names:
                reason = "unknown" if registry.get_entry(name) is None else "disabled or outside the parent's available surface"
                return f"Task {index}: allowed_tools name {name!r} is {reason}."
            if registry.get_entry(name) is not None and name not in selected:
                return f"Task {index}: allowed_tools name {name!r} is disabled or outside the parent's enabled toolsets."
        exposed = _tool_names(filter_tool_defs_by_allowlist(definitions, allowed))
        if missing := set(allowed) - exposed:
            return f"Task {index}: allowed_tools {sorted(missing)!r} cannot be exposed with this set; required companion tools are missing."
    return None



def filter_tool_defs_by_allowlist(tools: Iterable[dict[str, Any]], allowed: Optional[Iterable[str]]) -> list[dict]:
    allowed = explicit_allowed_tools(allowed)
    if allowed is None:
        return list(tools)
    filtered = [tool for tool in tools if tool["function"]["name"] in allowed]
    import model_tools
    available = _tool_names(filtered)
    if "terminal" not in available:
        filtered = [tool for tool in filtered if tool["function"]["name"] != "browser_exec"]
    return [model_tools._rewrite_execute_code(tool, available) if tool["function"]["name"] == "execute_code" else tool
            for tool in filtered]
