"""Invariants for the handoff-summary guard in ``agent/turn_stop_gates.py``.

A model can answer a turn with a conversation recap (``<analysis>…</analysis><summary>…</summary>``
plus recap headings) instead of the user's answer. The guard must (a) recognise only that shape,
(b) ask for the real answer once, (c) never show the recap in place of an answer, and (d) leave a
recap the user explicitly asked for alone.
"""

from __future__ import annotations

import pytest

from agent import turn_stop_gates as gates


def _recap(chars: int = 2400) -> str:
    body = (
        "<analysis>\nLet me chronologically analyze this conversation.\n\n"
        "**Turn 1 (user)**: asked what a session was about.\n\n</analysis>\n<summary>\n"
        "1. Primary Request and Intent:\n   Explain the session.\n\n"
        "2. Key Technical Concepts:\n   turn loop.\n\n"
        "3. Files and Code Sections:\n   agent/turn_stop_gates.py\n\n"
        "6. All user messages:\n   - what is this session\n\n"
        "7. Pending Tasks:\n   none\n\n"
        "8. Current Work:\n   investigating\n\n"
        "9. Optional Next Step:\n   commit and push both branches.\n</summary>"
    )
    return body + ("\n" + "x" * max(0, chars - len(body)))


class _Agent:
    """Minimal agent surface ``apply_stop_gates`` touches for this gate."""

    session_id = "test-session"

    def __init__(self) -> None:
        self._session_messages: list = []
        self.flushes = 0

    def _interim_content_was_streamed(self, _text: str) -> bool:
        return False

    def _flush_messages_to_session_db(self, _messages, _history) -> None:
        self.flushes += 1


@pytest.fixture()
def only_handoff_gate(monkeypatch):
    """Silence the sibling gates so a passing assertion names this gate and nothing else."""
    monkeypatch.setattr(gates, "_late_lsp_verdicts", lambda _agent: None)
    monkeypatch.setattr(gates, "_verify_on_stop_nudge", lambda _agent: None)
    monkeypatch.setattr(gates, "_pre_verify_nudge", lambda _agent, _resp, _attempt: None)
    monkeypatch.setattr(gates, "_kanban_stop_nudge", lambda _agent, _messages: None)
    monkeypatch.setattr(gates, "_handoff_guard_enabled", lambda config=None: True)


def _gate(agent, text, messages):
    final_msg = {"role": "assistant", "content": text}
    return gates.apply_stop_gates(
        agent, final_msg, final_response=text, messages=messages,
        conversation_history=None, pending_verification_response=None,
        pending_verification_response_previewed=False,
    )


def test_recognises_a_recap_and_not_a_long_answer():
    assert gates._looks_like_handoff_document(_recap()) is True
    # A long, ordinary answer mentioning the words is not a document.
    ordinary = ("The tag <analysis> is a wrapper. " * 80) + "Here is the answer you asked for."
    assert gates._looks_like_handoff_document(ordinary) is False
    # Below the size floor: a short reply cannot be a recap.
    assert gates._looks_like_handoff_document("<analysis>x</analysis><summary>ok</summary>") is False
    assert gates._looks_like_handoff_document(None) is False


def test_recap_is_nudged_then_replaced_by_a_notice(only_handoff_gate):
    agent = _Agent()
    messages: list = [{"role": "user", "content": "修一下这个 bug"}]

    first = _gate(agent, _recap(), messages)
    assert first.continue_turn is True
    assert first.final_response is None
    # The candidate is kept for budget fallback, never shown as the answer.
    assert first.pending_verification_response
    assert agent._handoff_summary_nudges == 1
    assert messages[-1]["role"] == "user" and messages[-1]["_handoff_summary_synthetic"] is True
    # The document must NOT enter history: a persisted copy re-seeds the pattern next turn.
    assert not any(isinstance(m.get("content"), str) and "<analysis>" in m["content"] for m in messages)

    # A second nudge is granted: a long turn can recap again after useful work.
    agent._session_messages = messages
    assert _gate(agent, _recap(), messages).continue_turn is True
    assert agent._handoff_summary_nudges == 2

    # Budget exhausted -> the recap is withheld and an explicit notice answers.
    agent._session_messages = messages
    second = _gate(agent, _recap(), messages)
    assert second.continue_turn is False
    assert second.final_response == gates._HANDOFF_SUMMARY_NOTICE
    # The recap body never reaches the user, even though the notice names the tags.
    assert "Let me chronologically analyze" not in second.final_response


def test_a_recap_the_user_asked_for_is_not_gated(only_handoff_gate):
    agent = _Agent()
    messages = [{"role": "user", "content": "帮我总结一下这个会话做了什么"}]
    verdict = _gate(agent, _recap(), messages)
    assert verdict.continue_turn is False
    assert verdict.final_response.startswith("<analysis>")


def test_the_nudge_budget_is_per_turn_not_per_session(only_handoff_gate):
    """A later turn in the same session gets its own nudge: one budget per user ask."""
    agent = _Agent()
    messages: list = [{"role": "user", "content": "第一轮的问题"}]

    agent._current_turn_id = "turn-A"
    assert _gate(agent, _recap(), messages).continue_turn is True
    assert _gate(agent, _recap(), messages).continue_turn is True
    assert _gate(agent, _recap(), messages).continue_turn is False  # budget spent in turn A

    # A new turn resets it, so this turn is nudged again instead of jumping to the notice.
    agent._current_turn_id = "turn-B"
    messages.append({"role": "user", "content": "第二轮的问题"})
    assert _gate(agent, _recap(), messages).continue_turn is True
    assert agent._handoff_summary_nudges == 1


def test_config_key_turns_the_guard_off_through_the_loader(tmp_path, monkeypatch):
    """The declared ``agent.handoff_summary_guard`` is what the runtime reader honours."""
    home = tmp_path / "home"
    home.mkdir()
    # Written as text: the test environment carries no PyYAML, and the reader only needs a file.
    (home / "config.yaml").write_text(
        "agent:\n  handoff_summary_guard: false\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Real read through the loader the runtime uses (no patched reader).
    assert gates._handoff_guard_enabled() is False
    assert gates._handoff_summary_action(_Agent(), _recap(), [{"role": "user", "content": "hi"}]) is None

    # Default stays ON when the key is absent.
    assert gates._handoff_guard_enabled({}) is True
    assert gates._handoff_guard_enabled({"agent": {}}) is True
