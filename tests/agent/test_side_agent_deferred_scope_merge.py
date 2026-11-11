"""A side agent (gateway ``side_agent=True``: the btw/refine turn) never carries
``manage_connections`` — ``toolsets.agent_tool_drops`` strips it from the session surface.
The deferred-tool bridge must agree with that surface everywhere it is consulted: the bare-name
route (``turn_tool_validation`` → ``_route_deferred_call``), the ``tool_call`` unwrap, the
search/describe catalog and the listing embedded in the ``tool_search`` schema.

Regression for the upstream sync that added bare-name deferred routing: the bridge scope was
computed from pre-drop ``get_tool_definitions``, so a dropped tool stayed routable, advertised
and dispatchable from a side agent. Authorized tools stay reachable, and a NORMAL agent keeps
its full catalog — this is a scoped fix, not a blanket rejection.
"""

import json
from types import SimpleNamespace

import pytest

import model_tools
from agent import tool_executor
from agent.tool_executor import (
    _ToolCallRef,
    _parse_tool_call,
    _resolve_sequential_dispatch,
    _run_agent_tool_execution_middleware,
    _unwrap_tool_search_call,
)
from agent.turn_tool_validation import _route_deferred_call, validate_tool_calls
from tools import tool_search
from toolsets import agent_tool_drops, agent_tool_surface

DROPPED = "manage_connections"
AUTHORIZED_DEFERRED = "todo_list"
BRIDGE = "tool_call"


@pytest.fixture
def connectors_granted(monkeypatch):
    """Expose ``manage_connections`` (its check_fn is the portal gate) for this test module."""
    from tools.connectors.gateway import config as gateway_config
    from tools.registry import invalidate_check_fn_cache

    monkeypatch.setattr(gateway_config, "connectors_available", lambda *args, **kwargs: True)
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    yield
    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()


def _raw_defs():
    """Pre-assembly catalog: every tool the toolset selection grants (no bridge yet)."""
    return model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True) or []


def _agent(*, side_agent):
    agent = SimpleNamespace(
        side_agent=side_agent,
        platform="cli",
        allowed_tools=None,
        enabled_toolsets=None,
        disabled_toolsets=None,
        session_id="side-session",
        quiet_mode=True,
        log_prefix="",
        _context_engine_tool_names=set(),
        _memory_manager=None,
        _current_turn_id="",
        _current_api_request_id="",
    )
    # The session's real surface, built the way ``agent_init`` builds it.
    agent.tools = agent_tool_surface(agent, model_tools.get_tool_definitions(quiet_mode=True) or [])
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    agent._vprint = lambda *args, **kwargs: None
    agent._repair_tool_call = lambda name: None
    agent._uniquify_tool_call_ids = lambda calls, taken=None: None
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    return agent


def _call(name, arguments, call_id="call-1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _describe(agent):
    return next(tool["function"] for tool in agent.tools if tool["function"]["name"] == "tool_search")


# --- the bare-name route the merge added ------------------------------------------------


def test_side_agent_bare_dropped_name_is_not_routed_into_the_bridge(connectors_granted):
    agent = _agent(side_agent=True)
    assert DROPPED not in agent.valid_tool_names

    call = _call(DROPPED, json.dumps({"action": "status"}))

    assert _route_deferred_call(agent, call) is False
    assert call.function.name == DROPPED  # left for the invalid-name path, never bridged


def test_side_agent_bare_authorized_tool_still_routes(connectors_granted):
    agent = _agent(side_agent=True)
    call = _call(AUTHORIZED_DEFERRED, json.dumps({}))

    assert _route_deferred_call(agent, call) is True
    assert call.function.name == BRIDGE


def test_normal_agent_bare_dropped_name_still_routes(connectors_granted):
    call = _call(DROPPED, json.dumps({"action": "status"}))

    assert _route_deferred_call(_agent(side_agent=False), call) is True
    assert call.function.name == BRIDGE


# --- the model-written bridge call ------------------------------------------------------


def test_side_agent_bridge_call_to_dropped_name_is_blocked(connectors_granted):
    agent = _agent(side_agent=True)

    name, _, block = _unwrap_tool_search_call(
        agent, BRIDGE, {"calls": [{"name": DROPPED, "arguments": {"action": "status"}}]})

    assert name == BRIDGE  # stays the bridge: never unwrapped to the dropped tool
    assert block is not None
    assert "not available" in block.lower()
    assert "use tool_search to find" not in block.lower()


def test_side_agent_bridge_call_to_authorized_tool_is_not_blocked(connectors_granted):
    agent = _agent(side_agent=True)

    name, _, block = _unwrap_tool_search_call(
        agent, BRIDGE, {"calls": [{"name": AUTHORIZED_DEFERRED, "arguments": {}}]})

    assert block is None
    assert name == AUTHORIZED_DEFERRED


def test_normal_agent_bridge_call_to_the_tool_is_not_blocked(connectors_granted):
    agent = _agent(side_agent=False)

    name, _, block = _unwrap_tool_search_call(
        agent, BRIDGE, {"calls": [{"name": DROPPED, "arguments": {"action": "status"}}]})

    assert block is None
    assert name == DROPPED


# --- the dispatch gate: the unauthorized call never reaches a handler --------------------


def _dispatch_gate(agent, parsed, name=None, args=None):
    """Run the real policy/dispatch gate for a parsed call; returns (handler_args, state)."""
    executed = []
    state = _run_agent_tool_execution_middleware(
        agent,
        function_name=name or parsed.name,
        function_args=parsed.args if args is None else args,
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=executed.append,
        scope_block=parsed.scope_block,
    )
    return executed, state


def test_side_agent_bare_dropped_name_fails_validation_instead_of_routing(connectors_granted):
    agent = _agent(side_agent=True)
    agent._invalid_tool_retries = 0
    agent._invalid_json_retries = 0
    agent._buffer_vprint = lambda *args, **kwargs: None
    agent._flush_status_buffer = lambda: None
    agent._build_assistant_message = lambda message, finish_reason: {"role": "assistant", "content": None}
    call = _call(DROPPED, json.dumps({"action": "status"}))
    assistant = SimpleNamespace(tool_calls=[call], provider_data=None, content=None)
    messages = []

    verdict = validate_tool_calls(
        agent, assistant, "tool_calls", messages=messages,
        conversation_history=[], api_call_count=1, effective_task_id="task-1")

    assert verdict.action == "continue"  # unknown-tool retry path, the pre-merge refusal
    assert call.function.name == DROPPED
    assert "does not exist" in messages[-1]["content"]


def test_normal_agent_bare_dropped_name_routes_and_dispatches(connectors_granted):
    agent = _agent(side_agent=False)
    agent._invalid_tool_retries = 0
    agent._invalid_json_retries = 0
    call = _call(DROPPED, json.dumps({"action": "status"}))
    assistant = SimpleNamespace(tool_calls=[call], provider_data=None, content=None)

    verdict = validate_tool_calls(
        agent, assistant, "tool_calls", messages=[], conversation_history=[],
        api_call_count=1, effective_task_id="task-1")

    assert verdict.action == "ok"
    assert call.function.name == BRIDGE  # routed through the bridge, as any deferred tool


def test_side_agent_bridge_call_never_reaches_dispatch(connectors_granted):
    agent = _agent(side_agent=True)
    parsed = _parse_tool_call(
        agent, _call(BRIDGE, json.dumps({"calls": [{"name": DROPPED, "arguments": {"action": "status"}}]})))

    assert parsed.scope_block is not None
    executed, state = _dispatch_gate(agent, parsed)

    assert executed == []
    assert state.blocked is True
    assert "not available" in json.loads(state.result)["error"].lower()


def test_side_agent_authorized_call_reaches_dispatch(connectors_granted):
    agent = _agent(side_agent=True)
    parsed = _parse_tool_call(agent, _call(BRIDGE, json.dumps({"calls": [{"name": AUTHORIZED_DEFERRED, "arguments": {}}]})))

    assert parsed.scope_block is None
    assert parsed.name == AUTHORIZED_DEFERRED


# --- the catalog the model reads -------------------------------------------------------


def _listed_names(description: str) -> set[str]:
    """Deferred-tool names the ``tool_search`` description advertises as existing."""
    return {line[2:].split(":", 1)[0].strip()
            for line in description.splitlines() if line.startswith("- ") and ":" in line}


def test_side_agent_listing_is_the_session_listing_minus_its_drops(connectors_granted):
    normal = _listed_names(_describe(_agent(side_agent=False))["description"])
    side_agent = _agent(side_agent=True)
    description = _describe(side_agent)["description"]

    assert DROPPED in normal  # premise: this fixture's config defers the tool for a full session
    assert _listed_names(description) == normal - set(agent_tool_drops(side_agent))
    assert f"`{DROPPED}`" not in description  # the connectors hint goes with the tool


def test_side_agent_search_catalog_hides_the_dropped_tool(connectors_granted):
    described = json.loads(tool_search.dispatch_tool_describe({"names": [DROPPED]}, current_tool_defs=_raw_defs()))
    assert DROPPED in described["tools"]  # top-level session: reachable

    with tool_search.scoped_hidden_tool_names(frozenset({DROPPED})):
        hidden = json.loads(tool_search.dispatch_tool_describe({"names": [DROPPED]}, current_tool_defs=_raw_defs()))

    assert DROPPED not in hidden["tools"]
    assert "not available" in hidden["errors"][DROPPED].lower()


def test_side_agent_bridge_dispatch_carries_the_hidden_scope(connectors_granted, monkeypatch):
    agent = _agent(side_agent=True)
    agent._tool_guardrails = SimpleNamespace(before_call=lambda name, args: SimpleNamespace(allows_execution=True))
    agent.tool_progress_callback = None
    seen = {}

    def _record(function_name, function_args, task_id=None, **kwargs):
        seen["hidden"] = tool_search.hidden_tool_names()
        return "{}"

    monkeypatch.setattr(model_tools, "handle_function_call", _record)
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *args, **kwargs: None)
    dispatch = _resolve_sequential_dispatch(
        agent, _ToolCallRef("tool_search", {"queries": ["connect an account"]}, "task-1", "call-1", []), [])

    _run_agent_tool_execution_middleware(
        agent, function_name="tool_search", function_args={"queries": ["connect an account"]},
        effective_task_id="task-1", tool_call_id="call-1", execute=dispatch.execute)

    assert dispatch is not None
    assert DROPPED in seen["hidden"]
