"""Malformed native-provider sidecars must survive tool-name repair.

Reviewer finding: ``validate_tool_calls`` renames a repaired tool call and pushes that rename into
``anthropic_content_blocks`` / ``bedrock_content_blocks`` *before* it normalizes
``function.arguments``. The sidecar rewrite parsed those still-raw arguments with ``json.loads``,
so a repairable name arriving with dict / empty / truncated arguments raised ``TypeError`` /
``JSONDecodeError`` and took the whole turn down (Anthropic and Bedrock replay paths).

These tests pin the contract at the real ``validate_tool_calls`` boundary:

* the sidecar never changes the verdict — same action, result and retry state with and without
  ``provider_data``;
* signed thinking/text blocks keep their payload, signature and order (replayed verbatim);
* a repaired call's name, id and input reach the sidecar, so replay matches ``tool_calls``;
* duplicate call ids pair block-by-block, not all-onto-the-first block;
* malformed arguments are never dressed up as a plausible payload: truncated args still refuse the
  turn instead of executing with ``{}``;
* the existing partial / error / retry semantics are unchanged (3-strike partial exit, blank-name
  anti-priming error results, invalid-JSON retry ladder, closing an open tool-result tail).
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

VALID = {"read_file", "write_file", "browser_click", "terminal", "todo", "session_search"}

TRUNCATED = '{"path": "a'
# Invalid JSON that is NOT a mid-stream cut ("ends in a brace"): the retry ladder, not the
# truncation refusal, owns it.
BROKEN_JSON = '{"path": }'


def _agent(valid=VALID, *, tools=None):
    """Agent shell for ``validate_tool_calls``: only the members that function reads."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id = "anthropic", "m", "s1"
    agent.tools = tools if tools is not None else []
    agent.valid_tool_names = set(valid)
    agent.log_prefix, agent._base_url, agent.api_mode = "", "https://api.example.invalid", "anthropic_messages"
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent._vprint = lambda *a, **k: None
    agent._buffer_vprint = lambda *a, **k: None
    agent._flush_status_buffer = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None
    agent.persisted = []
    agent._persist_session = lambda messages, *a, **k: agent.persisted.append(copy.deepcopy(list(messages)))
    # The real builder pulls the stream/reasoning stack; the row shape (name + arguments read now,
    # not earlier) is what these tests observe.
    agent._build_assistant_message = lambda message, finish_reason: {
        "role": "assistant", "content": getattr(message, "content", "") or "",
        "tool_calls": [{"id": tc.id, "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                       for tc in message.tool_calls or ()],
        **({"anthropic_content_blocks": list(message.provider_data["anthropic_content_blocks"])}
           if getattr(message, "provider_data", None) else {}),
        **({"bedrock_content_blocks": list(message.provider_data["bedrock_content_blocks"])}
           if getattr(message, "provider_data", None) else {}),
    }
    return agent


def _call(name, arguments, call_id="toolu_1"):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=arguments))


def _thinking_block():
    return {"type": "thinking", "thinking": "reading a.txt", "signature": "sig-1"}


def _bedrock_thinking_block():
    return {"reasoningContent": {"reasoningText": {"text": "reading a.txt", "signature": "sig-1"},
                                 "signature": "sig-1"}}


def _message(calls, *, sidecars=True, block_inputs=None, block_ids=None):
    """One assistant turn; each call gets an ordered ``tool_use`` block in both native sidecars."""
    provider_data = None
    if sidecars:
        inputs = block_inputs or [{"path": "a.txt"}] * len(calls)
        ids = block_ids or [tc.id for tc in calls]
        provider_data = {
            "anthropic_content_blocks": [
                _thinking_block(),
                *({"type": "tool_use", "id": cid, "name": tc.function.name, "input": value}
                  for tc, cid, value in zip(calls, ids, inputs)),
            ],
            "bedrock_content_blocks": [
                _bedrock_thinking_block(),
                *({"toolUse": {"toolUseId": cid, "name": tc.function.name, "input": value}}
                  for tc, cid, value in zip(calls, ids, inputs)),
            ],
        }
    return SimpleNamespace(content="", tool_calls=list(calls), provider_data=provider_data)


def _validate(agent, message, *, finish_reason="tool_calls", messages=None):
    from agent.turn_tool_validation import validate_tool_calls

    return validate_tool_calls(agent, message, finish_reason,
                               messages=messages if messages is not None else [],
                               conversation_history=[], api_call_count=1, effective_task_id="t1")


def _stored(message):
    """The row this turn persists: live ``tool_calls`` plus the live sidecars."""
    provider_data = getattr(message, "provider_data", None) or {}
    return {"role": "assistant", "content": "", **provider_data, "tool_calls": [
        {"id": tc.id, "type": "function",
         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
        for tc in message.tool_calls]}


def _payload(arguments):
    """The ``input`` a repaired call must replay with, mirroring ``validate_tool_calls``."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments or not str(arguments).strip():
        return {}
    return json.loads(arguments)


def _replayed_tool_uses(message):
    from agent.anthropic_message_convert import _convert_assistant_message

    return [b for b in _convert_assistant_message(_stored(message))["content"] if b["type"] == "tool_use"]


def _replayed_bedrock_tool_uses(message):
    from agent.bedrock_adapter import _assistant_blocks

    return [b["toolUse"] for b in _assistant_blocks(_stored(message), "") if "toolUse" in b]


# --------------------------------------------------------------------------- sidecar never decides
@pytest.mark.parametrize("arguments", [
    {"path": "a.txt"},       # dict arguments (json-mode providers hand these through untouched)
    "",                      # empty arguments (documented model quirk)
    "   ",                   # whitespace-only
    '{"path": "a.txt"}',     # well-formed control
])
def test_repairable_name_with_odd_arguments_keeps_the_verdict(arguments):
    """Repair must not depend on argument shape: the verdict is identical with and without sidecars."""
    bare_agent, bare = _agent(), _message([_call("read_fiel", arguments)], sidecars=False)
    bare_verdict = _validate(bare_agent, bare)

    agent, message = _agent(), _message([_call("read_fiel", arguments)])
    verdict = _validate(agent, message)

    assert (verdict.action, verdict.mixed_invalid_batch) == (bare_verdict.action, bare_verdict.mixed_invalid_batch)
    assert verdict.action == "ok"
    assert message.tool_calls[0].function.name == bare.tool_calls[0].function.name == "read_file"
    assert message.tool_calls[0].function.arguments == bare.tool_calls[0].function.arguments
    assert json.loads(message.tool_calls[0].function.arguments) == _payload(arguments)


@pytest.mark.parametrize("arguments", ["", "   ", None, {"path": "a.txt"}, '{"path": "a.txt"}'])
def test_repaired_call_replays_with_its_name_id_and_input(arguments):
    """Both sidecars carry the repaired name, the (uniquified) id and the arguments' payload."""
    agent, message = _agent(), _message([_call("read_fiel", arguments)])
    assert _validate(agent, message).action == "ok"
    expected = _payload(arguments)

    thinking = message.provider_data["anthropic_content_blocks"][0]
    bedrock_thinking = message.provider_data["bedrock_content_blocks"][0]
    blocks = message.provider_data["anthropic_content_blocks"]
    bedrock_blocks = message.provider_data["bedrock_content_blocks"]
    assert blocks[0] is thinking and bedrock_blocks[0] is bedrock_thinking  # untouched, in place
    assert blocks[0] == _thinking_block() and bedrock_blocks[0] == _bedrock_thinking_block()

    assert [b["id"] for b in blocks[1:]] == [message.tool_calls[0].id]
    assert [b["name"] for b in blocks[1:]] == ["read_file"]
    assert [b["input"] for b in blocks[1:]] == [expected]
    assert [b["toolUse"]["toolUseId"] for b in bedrock_blocks[1:]] == [message.tool_calls[0].id]
    assert [b["toolUse"]["name"] for b in bedrock_blocks[1:]] == ["read_file"]
    assert [b["toolUse"]["input"] for b in bedrock_blocks[1:]] == [expected]

    # The real replay consumers (signed-thinking sidecar first) see the corrected turn.
    assert [(b["name"], b["input"]) for b in _replayed_tool_uses(message)] == [("read_file", expected)]
    assert [(b["name"], b["input"]) for b in _replayed_bedrock_tool_uses(message)] == \
           [("read_file", expected)]
    # ...and the signed thinking block survives that replay, in order, ahead of the tool_use.
    from agent.anthropic_message_convert import _convert_assistant_message

    replayed = _convert_assistant_message(_stored(message))["content"]
    assert replayed[0]["type"] == "thinking" and replayed[0]["signature"] == "sig-1"
    assert [b["type"] for b in replayed].index("tool_use") > 0


def test_blank_arguments_replay_the_same_empty_object_the_call_executes_with():
    """``""``/``None`` become ``{}`` for the call; the sidecar must not keep a divergent payload."""
    for arguments in ("", "   ", None):
        agent, message = _agent(), _message([_call("read_fiel", arguments)])
        assert _validate(agent, message).action == "ok"
        assert message.tool_calls[0].function.arguments == "{}"
        assert message.provider_data["anthropic_content_blocks"][1]["input"] == {}
        assert message.provider_data["bedrock_content_blocks"][1]["toolUse"]["input"] == {}


# --------------------------------------------------------------------------- truncated args refuse
def test_truncated_arguments_with_repairable_name_still_refuse_the_turn():
    """Cut-off args keep their own semantics: partial exit, nothing executed, no invented payload."""
    bare_agent, bare = _agent(), _message([_call("read_fiel", TRUNCATED)], sidecars=False)
    bare_verdict = _validate(bare_agent, bare)

    agent, message = _agent(), _message([_call("read_fiel", TRUNCATED)])
    verdict = _validate(agent, message)

    assert bare_verdict.action == "return" and verdict.action == "return"
    assert verdict.result["partial"] is True and verdict.result["completed"] is False
    assert verdict.result["error"] == bare_verdict.result["error"]
    assert verdict.result["messages"] == bare_verdict.result["messages"] == []
    # No plausible-looking payload was fabricated from the truncated text.
    assert message.tool_calls[0].function.arguments == TRUNCATED
    assert message.provider_data["anthropic_content_blocks"][1]["input"] == {"path": "a.txt"}
    assert message.provider_data["bedrock_content_blocks"][1]["toolUse"]["input"] == {"path": "a.txt"}


def test_truncated_partial_exit_still_closes_an_open_tool_result_tail():
    """Regression guard for the #48879 exit contract on this path (tool → user is illegal)."""
    agent = _agent()
    messages = [{"role": "tool", "tool_call_id": "toolu_0", "content": "done"}]
    message = _message([_call("read_fiel", TRUNCATED)])

    verdict = _validate(agent, message, messages=messages)

    assert verdict.action == "return" and verdict.result["partial"] is True
    assert messages[-1]["role"] == "assistant"
    assert agent.persisted and agent.persisted[-1] == messages


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls"])
def test_truncation_copy_matches_the_non_sidecar_path(finish_reason):
    bare_agent, bare = _agent(), _message([_call("read_fiel", TRUNCATED)], sidecars=False)
    agent, message = _agent(), _message([_call("read_fiel", TRUNCATED)])
    assert _validate(bare_agent, bare, finish_reason=finish_reason).result["error"] == \
           _validate(agent, message, finish_reason=finish_reason).result["error"]


# --------------------------------------------------------------------------- repeats & mixed batches
def test_repeated_validation_of_the_same_malformed_turn_is_stable():
    """A retry loop re-validates the same live message: no crash, no drift, one repair."""
    agent, message = _agent(), _message([_call("read_fiel", {"path": "a.txt"})])
    first = _validate(agent, message)
    snapshot = copy.deepcopy(message.provider_data)
    second = _validate(agent, message)

    assert first.action == second.action == "ok"
    assert message.tool_calls[0].function.name == "read_file"
    assert message.provider_data == snapshot
    assert agent._invalid_tool_retries == agent._invalid_json_retries == 0


def test_repeated_truncated_turn_refuses_every_time():
    agent, message = _agent(), _message([_call("read_fiel", TRUNCATED)])
    assert [_validate(agent, message).action for _ in range(3)] == ["return"] * 3
    assert message.provider_data["anthropic_content_blocks"][1]["name"] == "read_file"
    assert message.provider_data["anthropic_content_blocks"][1]["input"] == {"path": "a.txt"}


def test_mixed_batch_repairs_the_malformed_call_and_leaves_the_valid_block_alone():
    agent = _agent()
    message = _message([_call("read_fiel", {"path": "a.txt"}, "toolu_1"),
                        _call("terminal", '{"command": "ls"}', "toolu_2")],
                       block_inputs=[{"path": "a.txt"}, {"command": "ls"}])
    valid_block = message.provider_data["anthropic_content_blocks"][2]
    valid_bedrock_block = message.provider_data["bedrock_content_blocks"][2]

    verdict = _validate(agent, message)

    assert verdict.action == "ok" and verdict.mixed_invalid_batch is False
    assert [tc.function.name for tc in message.tool_calls] == ["read_file", "terminal"]
    blocks = message.provider_data["anthropic_content_blocks"]
    assert (blocks[1]["id"], blocks[1]["name"], blocks[1]["input"]) == ("toolu_1", "read_file", {"path": "a.txt"})
    assert blocks[2] is valid_block and message.provider_data["bedrock_content_blocks"][2] is valid_bedrock_block
    assert [b["name"] for b in _replayed_tool_uses(message)] == ["read_file", "terminal"]


def test_duplicate_call_ids_pair_each_block_with_its_own_call():
    """Two calls sharing one id: each sidecar block takes its own call's name/input/id."""
    agent = _agent()
    message = _message([_call("read_fiel", {"path": "a.txt"}, "toolu_1"),
                        _call("BrowserClick", {"selector": "#go"}, "toolu_1")],
                       block_inputs=[{"path": "a.txt"}, {"selector": "#go"}])

    assert _validate(agent, message).action == "ok"

    ids = [tc.id for tc in message.tool_calls]
    assert len(set(ids)) == 2 and ids[0] == "toolu_1"
    blocks = message.provider_data["anthropic_content_blocks"]
    assert [(b["id"], b["name"], b["input"]) for b in blocks[1:]] == [
        (ids[0], "read_file", {"path": "a.txt"}), (ids[1], "browser_click", {"selector": "#go"})]
    bedrock = [b["toolUse"] for b in message.provider_data["bedrock_content_blocks"][1:]]
    assert [(b["toolUseId"], b["name"], b["input"]) for b in bedrock] == [
        (ids[0], "read_file", {"path": "a.txt"}), (ids[1], "browser_click", {"selector": "#go"})]
    assert [b["id"] for b in _replayed_tool_uses(message)] == ids
    assert [b["name"] for b in _replayed_bedrock_tool_uses(message)] == ["read_file", "browser_click"]


@pytest.mark.parametrize("names", [
    ("terminal", "read_fiel"),
    ("read_fiel", "terminal"),
    ("terminal", "read_file"),
])
def test_duplicate_call_ids_preserve_each_mixed_native_block(names):
    inputs = [{"command": "first"}, {"path": "second"}]
    message = _message([_call(names[0], inputs[0], "toolu_1"),
                        _call(names[1], inputs[1], "toolu_1")], block_inputs=inputs)

    assert _validate(_agent(), message).action == "ok"

    expected = [(tc.id, tc.function.name, json.loads(tc.function.arguments)) for tc in message.tool_calls]
    anthropic = message.provider_data["anthropic_content_blocks"][1:]
    bedrock = [block["toolUse"] for block in message.provider_data["bedrock_content_blocks"][1:]]
    assert [(b["id"], b["name"], b["input"]) for b in anthropic] == expected
    assert [(b["toolUseId"], b["name"], b["input"]) for b in bedrock] == expected


# --------------------------------------------------------------------------- existing error semantics
def test_unrepairable_name_with_malformed_arguments_keeps_the_error_result_path():
    """No repair → no sidecar write; the 3-strike error/partial ladder is unchanged."""
    for arguments in ({"path": "a.txt"}, "", TRUNCATED):
        agent, message = _agent(), _message([_call("totally_unknown_tool", arguments)])
        before = copy.deepcopy(message.provider_data)

        verdict = _validate(agent, message)

        assert verdict.action == "continue" and agent._invalid_tool_retries == 1
        assert message.provider_data == before
        # Messages are created inside the call; re-run with a list to inspect the error rows.
        agent, message = _agent(), _message([_call("totally_unknown_tool", arguments)])
        messages = []
        _validate(agent, message, messages=messages)
        results = [m for m in messages if m["role"] == "tool"]
        assert len(results) == 1 and results[0]["tool_call_id"] == "toolu_1"
        assert "totally_unknown_tool" in results[0]["content"]


def test_three_strike_partial_exit_survives_malformed_sidecars():
    agent, message = _agent(), _message([_call("totally_unknown_tool", TRUNCATED)])
    for _ in range(2):
        assert _validate(agent, message).action == "continue"
    verdict = _validate(agent, message)
    assert verdict.action == "return" and verdict.result["partial"] is True
    assert "totally_unknown_tool" in verdict.result["error"]


def test_invalid_json_retry_ladder_is_unchanged_by_sidecars():
    """Well-named call + broken args: retry twice, then recovery tool results (never a crash)."""
    for sidecars in (False, True):
        agent, message = _agent(), _message([_call("read_file", BROKEN_JSON)], sidecars=sidecars)
        assert _validate(agent, message).action == "continue"
        assert _validate(agent, message).action == "continue"
        messages = []
        verdict = _validate(agent, message, messages=messages)
        assert verdict.action == "continue"
        results = [m for m in messages if m["role"] == "tool"]
        assert len(results) == 1 and "Invalid JSON" in results[0]["content"]


def test_routed_bridge_call_keeps_its_envelope_in_both_sidecars(monkeypatch):
    """A deferred-tool call routed through ``tool_call`` rewrites name + envelope in the sidecar too."""
    monkeypatch.setattr("agent.tool_executor._tool_search_scoped_names",
                        lambda agent: frozenset({"session_search"}))
    agent = _agent(valid=(VALID - {"session_search"}) | {"tool_call"}, tools=[])
    message = _message([_call("session_search", '{"query": "x"}')], block_inputs=[{"query": "x"}])

    assert _validate(agent, message).action == "ok"

    assert message.tool_calls[0].function.name == "tool_call"
    envelope = json.loads(message.tool_calls[0].function.arguments)
    assert envelope == {"calls": [{"name": "session_search", "arguments": {"query": "x"}}]}
    assert message.provider_data["anthropic_content_blocks"][1]["name"] == "tool_call"
    assert message.provider_data["anthropic_content_blocks"][1]["input"] == envelope
    assert message.provider_data["bedrock_content_blocks"][1]["toolUse"]["input"] == envelope
    assert message.provider_data["anthropic_content_blocks"][0]["signature"] == "sig-1"
