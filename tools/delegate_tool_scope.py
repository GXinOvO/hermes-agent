from typing import Any


_INLINE_ALIASES = {
    "invoke_tool": "tool_call",
    "delegate": "delegate_task",
    "delegate_tasks": "delegate_task",
    "spawn_subagent": "delegate_task",
    "setup_mcp": "manage_connections",
}


def canonical_tool_name(name: str) -> str:
    from model_tools import _LEGACY_TOOL_ALIASES
    return _INLINE_ALIASES.get(name) or _LEGACY_TOOL_ALIASES.get(name) or name


def exact_tool_name(name: str) -> bool:
    return canonical_tool_name(name) == name


def tool_name_allowed(agent: Any, name: str) -> bool:
    from tools.delegate_tool_allowlist import explicit_allowed_tools
    allowed = explicit_allowed_tools(getattr(agent, "allowed_tools", None))
    return allowed is None or (exact_tool_name(name) and name in allowed)


def parent_effective_tool_defs(parent_agent: Any) -> list[dict]:
    if parent_agent is None:
        return []
    from model_tools import get_tool_definitions
    from tools.mcp_tool_agent import _reinject_authorized_dynamic_tools, _reinject_post_build_tools

    try:
        discovered = get_tool_definitions(
            enabled_toolsets=getattr(parent_agent, "enabled_toolsets", None),
            disabled_toolsets=getattr(parent_agent, "disabled_toolsets", None),
            quiet_mode=True, skip_tool_search_assembly=True,
        )
        if not isinstance(discovered, list):
            raise ValueError("parent tool discovery did not return a tool list")
        discovered = list(discovered)
        names = {entry.get("function", {}).get("name") for entry in discovered}
        _reinject_authorized_dynamic_tools(parent_agent, discovered, names)
        _reinject_post_build_tools(parent_agent, discovered, names)
    except Exception as exc:
        raise ValueError("Cannot resolve parent tool availability for allowed_tools") from exc
    from tools.delegate_tool_allowlist import explicit_allowed_tools
    allowed = explicit_allowed_tools(getattr(parent_agent, "allowed_tools", None))
    from toolsets import agent_tool_drops
    drops = agent_tool_drops(parent_agent)
    return [entry for entry in discovered
            if exact_tool_name(entry.get("function", {}).get("name", ""))
            and entry.get("function", {}).get("name") not in drops
            and (allowed is None or entry.get("function", {}).get("name") in allowed)]


def validate_restricted_route(agent: Any, runtime: dict | None = None) -> None:
    from tools.delegate_tool_allowlist import explicit_allowed_tools
    if explicit_allowed_tools(getattr(agent, "allowed_tools", None)) is None:
        return
    runtime = runtime or {}
    provider = str(runtime.get("provider", getattr(agent, "provider", "")) or "")
    base_url = str(runtime.get("base_url", getattr(agent, "base_url", "")) or "")
    api_mode = runtime.get("api_mode", getattr(agent, "api_mode", None))
    command = runtime.get("acp_command", runtime.get("command", getattr(agent, "acp_command", None)))
    from hermes_cli.runtime_provider_backends import _is_external_process_provider
    if (command or provider in {"acp", "codex-app-server"}
            or api_mode == "codex_app_server" or base_url.startswith(("acp://", "acp+tcp://"))
            or _is_external_process_provider(provider)):
        raise ValueError("allowed_tools cannot be enforced on ACP or external-agent routes; select a native API route")
