"""Post-construction context-engine tool injection."""

import logging

from hermes_constants import get_hermes_home

logger = logging.getLogger("run_agent")


def inject_context_engine_tools(agent):
    # Context engine tool schemas (lcm_*), deduped against existing names (plugins may
    # register the same schemas; duplicates 400 provider-side) and gated on enabled_toolsets
    # so `platform_toolsets: telegram: []` can't leak them.
    # Skip names that are already present — the model_tools.get_tool_definitions() quiet_mode cache returned a
    # shared list pre-#17335, so a stray mutation here would poison subsequent agent inits in the same
    # Gateway process and trip provider-side 'duplicate tool name' errors. Even with the cache fix, dedup is
    # the right defense against plugin paths that may register the same schemas via ctx.register_tool().
    # Mirrors the memory tools dedup above. Respect the platform's enabled_toolsets configuration (#5544):
    # context engine tools follow the same gating pattern as memory provider tools — without the gate,
    # `platform_toolsets: telegram: []` would still leak lcm_* tools into the tool surface and incur the
    # same local-model latency penalty.
    from tools.delegate_tool_allowlist import explicit_allowed_tools
    allowed_tools = explicit_allowed_tools(getattr(agent, "allowed_tools", None))
    agent._context_engine_tool_names = set()
    if (
        agent.context_compressor
        and agent.tools is not None
        and (agent.enabled_toolsets is None or "context_engine" in agent.enabled_toolsets)
    ):
        _existing_tool_names = {
            t.get("function", {}).get("name") for t in agent.tools if isinstance(t, dict)
        }
        from agent.memory_manager_tools import normalize_tool_schema
        for _raw_schema in agent.context_compressor.get_tool_schemas():
            _schema = normalize_tool_schema(_raw_schema)
            if _schema is None:
                # A nameless tool makes strict providers 400 and disables the whole toolset.
                logger.warning(
                    # Skip it. See #47707.
                    "Context engine returned a tool schema with no resolvable "
                    "name; skipping to avoid poisoning the request (%r)",
                    _raw_schema,
                )
                continue
            _tname = _schema["name"]
            if allowed_tools is not None and _tname not in set(allowed_tools):
                continue
            if _tname in _existing_tool_names:
                continue  # already registered via plugin/cache path
            agent.tools.append({"type": "function", "function": _schema})
            for _names in (agent.valid_tool_names, agent._context_engine_tool_names, _existing_tool_names):
                _names.add(_tname)

    if agent.context_compressor:
        try:
            agent.context_compressor.on_session_start(
                agent.session_id, hermes_home=str(get_hermes_home()),
                platform=agent.platform or "cli", model=agent.model,
                context_length=getattr(agent.context_compressor, "context_length", 0),
                conversation_id=getattr(agent, "_gateway_session_key", None),
            )
        except Exception as _ce_err:
            logger.debug("Context engine on_session_start: %s", _ce_err, exc_info=True)
