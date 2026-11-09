import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import conversation_loop as loop
from tools.bot_mode_probe import capability_fingerprint


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


@pytest.mark.parametrize("allowed", [[], ["read_file"]])
def test_bot_epoch_refresh_preserves_saved_restriction(allowed):
    epoch = capability_fingerprint()
    stale_epoch = "0" * 12 if epoch != "0" * 12 else "1" * 12
    prompt = ("Stored\nCapability epoch: " + stale_epoch
              + "\n\nModel: unit-model\nProvider: custom\nPlatform: cli")
    pin = {"version": "unit", "tools": [_tool(n) for n in allowed], "allowed_tools": allowed}
    written = []
    db = SimpleNamespace(
        get_session=lambda sid: {"system_prompt": prompt, "tool_names": json.dumps(pin)},
        update_system_prompt=lambda *args: None,
        update_session_tool_names=lambda sid, value: written.append(value),
    )
    agent = SimpleNamespace(
        tools=[_tool("read_file"), _tool("terminal")], valid_tool_names={"read_file", "terminal"},
        enabled_toolsets=["file", "terminal"], disabled_toolsets=[], platform="cli", model="unit-model",
        provider="custom", allowed_tools=None, _session_db=db, session_id="restricted-bot",
        _persist_disabled=False, _build_system_prompt=lambda *args: "rebuilt", _bot_mode_protocol=False,
    )
    with patch.object(loop, "requested_epoch", return_value=0), \
         patch.object(loop, "stamp_prompt_epoch"), patch.object(loop, "record_cache_break"), \
         patch.object(loop, "stage_surface_switch_note", return_value=False), \
         patch("agent.prompt_builder.clear_skills_system_prompt_cache"):
        assert loop._stored_prompt_matches_runtime(agent, prompt)
        assert loop._bot_chat_prompt_stale(agent, prompt)
        loop._restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "continue"}])
    assert agent.allowed_tools == allowed
    assert agent.valid_tool_names == set(allowed)
    assert written[-1]["allowed_tools"] == allowed


def test_prompt_write_failure_does_not_hide_restricted_tool_pin_failure():
    db = SimpleNamespace(
        update_system_prompt=lambda session_id, prompt: (_ for _ in ()).throw(RuntimeError("prompt-failed")),
    )
    agent = SimpleNamespace(
        _session_db=db,
        session_id="restricted-session",
        _cached_system_prompt="prompt",
        allowed_tools=[],
    )
    with patch("tools.mcp_tool_agent.persist_agent_tool_names", side_effect=ValueError("pin-failed")) as persist:
        with pytest.raises(ValueError, match="pin-failed"):
            loop._persist_system_prompt(agent, "prompt write failed for %s: %s", persist_tools=True)
    persist.assert_called_once_with(agent)
