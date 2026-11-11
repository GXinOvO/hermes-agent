"""The exact ``allowed_tools`` allowlist stays fail-closed across tool-name repair, while a
session without the restriction keeps the auto-repair path (and its deferred-tool routing).

Fork contract: a restricted child must never have a call it did not name silently renamed onto a
permitted neighbour — the model gets the unknown-name error instead. Upstream's deferred-tool
routing is allowlist-scoped on its own (the ``tool_call`` bridge is not assembled under
``allowed_tools``), so both behaviours coexist.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent


def _validate(name, *, allowed_tools=None, valid_tool_names=("read_file",),
              bridge=True):
    """Run the real ``validate_tool_calls`` on a stub agent with an optional restriction."""
    from agent.turn_tool_validation import validate_tool_calls

    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "nous", "m", "s1", []
    agent.valid_tool_names = set(valid_tool_names) | ({"tool_call"} if bridge else set())
    agent.log_prefix, agent._base_url, agent.api_mode = "", "https://api.example.invalid/v1", "chat_completions"
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent.enabled_toolsets, agent.disabled_toolsets = None, None
    agent._vprint = lambda *a, **k: None
    agent._buffer_vprint = lambda *a, **k: None
    if allowed_tools is not None:
        agent.allowed_tools = allowed_tools
    tc = SimpleNamespace(id="call_1", type="function",
                         function=SimpleNamespace(name=name, arguments=json.dumps({"path": "x"})))
    messages = []
    message = SimpleNamespace(content="", tool_calls=[tc])
    verdict = validate_tool_calls(agent, message, "tool_calls", messages=messages,
                                  conversation_history=[], api_call_count=1, effective_task_id="t1")
    errors = [m["content"] for m in messages if m.get("role") == "tool"]
    return verdict, tc, errors


@pytest.mark.parametrize("name", ["Read_File", "read-file", "read_file_tool"])
def test_restricted_session_never_repairs_a_name_onto_a_permitted_tool(name):
    """``allowed_tools`` is exact: a repairable-looking variant stays unknown and errors."""
    allowed = ["read_file"]
    verdict, tc, errors = _validate(name, allowed_tools=allowed, bridge=False)

    assert tc.function.name == name  # not renamed onto the permitted neighbour
    assert verdict.action == "continue"
    assert errors and errors[0].startswith(f"Tool '{name}' does not exist"), errors


@pytest.mark.parametrize("name, repaired_to", [
    ("Read_File", "read_file"), ("read-file", "read_file"), ("read_file_tool", "read_file"),
])
def test_unrestricted_session_still_repairs_the_same_variants(name, repaired_to):
    """Unchanged upstream/stock behaviour where no allowlist was requested."""
    verdict, tc, errors = _validate(name)

    assert verdict.action == "ok" and errors == []
    assert tc.function.name == repaired_to


def test_restricted_session_rejects_a_deferred_tool_name_instead_of_routing_it():
    """A deferred tool named by its real name is not rerouted for a restricted child: the bridge
    is never assembled there, so the call fails closed with the original name intact."""
    verdict, tc, errors = _validate("process_manage", allowed_tools=["read_file"],
                                    valid_tool_names=("read_file",), bridge=False)

    assert tc.function.name == "process_manage"
    assert verdict.action == "continue"
    assert errors and errors[0].startswith("Tool 'process_manage' does not exist"), errors


def test_restricted_session_never_routes_an_alias_even_if_the_bridge_is_present():
    with patch("agent.turn_tool_validation._route_deferred_call") as route:
        verdict, tc, errors = _validate("process", allowed_tools=["process_manage", "tool_call"],
                                        valid_tool_names=("tool_call",), bridge=True)
    route.assert_not_called()
    assert verdict.action == "continue" and tc.function.name == "process"
    assert errors and errors[0].startswith("Tool 'process' does not exist")


def test_restricted_session_permits_a_name_the_allowlist_actually_holds():
    """The guard only rules out renames; an exactly-named permitted tool still validates."""
    verdict, tc, errors = _validate("read_file", allowed_tools=["read_file"], bridge=False)

    assert verdict.action == "ok" and errors == []
    assert tc.function.name == "read_file"
