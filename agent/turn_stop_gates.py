"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, five gates may instead append the answer as an
interim row plus a user-role nudge and continue the turn: late language-server verdicts on this
turn's edits (a durable row, see ``_late_lsp_verdicts``), verify-on-stop (#65919),
the ``pre_verify`` plugin hook after code edits, the kanban worker terminal-tool guard, and the
handoff-summary guard (a recap substituted for the answer, see ``_handoff_summary_action``).
Each keeps the candidate answer as a budget-exhaustion fallback
(``pending_verification_response``) and clears ``final_response`` so the finalizer can tell
this gate from error exits (#61631) — except the handoff-summary guard's exhausted branch, which
withholds the recap and answers with an explicit notice. Nothing here imports
``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any


def _verify_on_stop_nudge(agent) -> Optional[str]:
    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge, verify_on_stop_enabled
        )

        if verify_on_stop_enabled():
            return build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
    except Exception:
        logger.debug("verification stop-loop check failed", exc_info=True)
    return None


def _pre_verify_nudge(agent, final_response, attempt: int) -> Optional[str]:
    """After code edits a registered ``pre_verify`` hook may keep the agent going one
    more turn; no default continuation cost."""
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if _edited and has_hook("pre_verify") and attempt < max_verify_nudges():
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            return get_pre_verify_continue_message(
                session_id=getattr(agent, "session_id", None) or "",
                platform=getattr(agent, "platform", "") or "",
                model=getattr(agent, "model", "") or "", coding=coding, attempt=attempt,
                final_response=final_response, changed_paths=_edited,
            )
    except Exception:
        logger.debug("pre_verify hook check failed", exc_info=True)
    return None


def _kanban_stop_nudge(agent, messages) -> Optional[str]:
    """Workers must end with a terminal board tool (kanban_complete / kanban_block /
    kanban_request_review / kanban_request_changes); a narrated stop is recorded
    as protocol_violation, so nudge once or twice first."""
    try:
        from agent.kanban_stop import build_kanban_stop_nudge

        return build_kanban_stop_nudge(
            messages=messages, attempts=getattr(agent, "_kanban_stop_nudges", 0)
        )
    except Exception:
        logger.debug("kanban stop-loop check failed", exc_info=True)
        return None


_MAX_LSP_TURN_END_NOTES = 3


def _late_lsp_verdicts(agent) -> Optional[str]:
    """Language-server verdicts on this turn's edits that no tool result carried yet (the edit was the
    last tool call; a one-shot run has no next turn to carry them): waits briefly for checks still
    running (``agent.lsp.deferred.settle_for_agent``) and returns them as a message for the model."""
    issued = getattr(agent, "_lsp_turn_end_notes", 0)
    if not isinstance(issued, int) or issued >= _MAX_LSP_TURN_END_NOTES:
        return None
    try:
        from agent.lsp.deferred import settle_for_agent

        notes = settle_for_agent(agent)
    except Exception:
        logger.debug("late LSP verdict check failed", exc_info=True)
        return None
    if not notes:
        return None
    body = "\n\n".join(text for _path, text in notes)
    return ("[System note: the language server reported problems in files you edited this turn, after "
            "those tool results had returned. Fix them if they are real, then give your final answer.]\n\n"
            + body)


def _append_interim_answer(agent, final_msg, messages, conversation_history, flush_fail_msg: str) -> None:
    """Real content: persist and emit as interim so the user sees the attempted answer;
    only the nudge is flagged synthetic (#65919). A held turn's answer (typed hidden) is not shown
    and flushes with the turn instead, so a budget fallback can still publish it as the rewrite."""
    agent._emit_interim_assistant_message(final_msg)
    append_message(messages, final_msg)
    if final_msg.get("display_kind") == "hidden":
        return
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug(flush_fail_msg, exc_info=True)


# --- handoff-summary guard -------------------------------------------------------------
# A model can answer a turn with a context-handoff document instead of the answer: an
# ``<analysis>…</analysis><summary>…</summary>`` recap of the whole conversation carrying the
# nine recap headings below. Observed on every model route in this deployment, at any context
# size (68k and 201k alike), roughly a third of samples on long tool-heavy turns. The transport
# cannot tell it from a normal answer — ``finish_reason=stop``, no tool calls, non-empty
# content — and the compressor's own recognition matches only its ``[CONTEXT COMPACTION …]``
# prefix, so a bare document reaches the user as the answer. One nudge asks for the real answer;
# a repeat ends the turn with an explicit notice instead of the document.

# Two, not one: a first nudge demonstrably works (the model resumed 30 tool calls after one and
# then recapped again at the end of the same turn), so a second keeps a long turn answerable.
_HANDOFF_MAX_NUDGES = 2
_SUMMARY_REQUEST_WORDS = (
    "总结", "摘要", "汇总", "回顾", "梳理", "交接", "复盘",
    "summarize", "summarise", "summary", "recap", "handoff", "what did we do",
)
_HANDOFF_SUMMARY_NUDGE = (
    "[System note: your last reply was a context-handoff document — it recapped the "
    "conversation's history and listed next steps instead of answering. The user did not ask for "
    "a handoff. Answer their latest request directly and only that; do not restate the "
    "conversation, do not list completed actions, files, or pending tasks.]"
)
_HANDOFF_SUMMARY_NOTICE = (
    "⚠️ 本轮没有回答你的问题：模型输出的是整段会话的交接摘要（<analysis>/<summary>），已拦截，"
    "没有把它当作答案显示。\n\n"
    "这是模型侧的偶发行为，不是你的操作问题。请重发这条请求；如果连续复现，"
    "可以在本轮减少上下文，或临时换用其他模型。"
)


def _handoff_guard_enabled(config: dict[str, Any] | None = None) -> bool:
    """``agent.handoff_summary_guard`` — default ON: an unexplained recap is not an answer.

    Read through the same read-only loader the verify-on-stop gate uses, so a mid-session
    ``config set`` takes effect without rebuilding the agent.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            return True
    if not isinstance(config, dict):
        return True
    agent_cfg = config.get("agent")
    if not isinstance(agent_cfg, dict):
        return True
    return agent_cfg.get("handoff_summary_guard", True) is not False


def _looks_like_handoff_document(text: Any) -> bool:
    """True when a visible answer carries the handoff-document signature.

    The shape test is shared with ``session_search``'s transcript elision
    (``agent/handoff_summary.py``) so the two never drift apart. A user who asked for a recap is
    exempted by the caller.
    """
    from agent.handoff_summary import looks_like_handoff

    return looks_like_handoff(text)


def _last_real_user_text(messages: list[dict[str, Any]]) -> str:
    """The newest user-authored message, skipping nudges this module appends."""
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if any(message.get(flag) for flag in (
            "_verification_stop_synthetic", "_pre_verify_synthetic",
            "_kanban_stop_synthetic", "_handoff_summary_synthetic",
        )):
            continue
        content = message.get("content")
        return content if isinstance(content, str) else ""
    return ""


def _user_asked_for_summary(messages: list[dict[str, Any]]) -> bool:
    """A requested recap is a legitimate answer: never gate those."""
    text = _last_real_user_text(messages).lower()
    return any(word in text for word in _SUMMARY_REQUEST_WORDS)


def _handoff_summary_action(
    agent: Any, final_response: Any, messages: list[dict[str, Any]],
) -> Optional[tuple[str, str]]:
    """``("nudge", nudge)`` while attempts remain, else ``("notice", notice)``, else None.

    The attempt budget is per TURN, not per session: a fresh user ask gets its own nudge even
    when an earlier turn already used one. ``_current_turn_id`` is the turn boundary the loop
    already maintains; when it is absent the budget degrades to per-session, which stays safe.
    """
    if not _handoff_guard_enabled() or not _looks_like_handoff_document(final_response):
        return None
    if _user_asked_for_summary(messages):
        logger.debug("handoff-summary guard skipped: the user asked for a recap")
        return None
    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    if turn_id and str(getattr(agent, "_handoff_summary_turn_id", "") or "") != turn_id:
        agent._handoff_summary_turn_id = turn_id
        agent._handoff_summary_nudges = 0
    attempts = int(getattr(agent, "_handoff_summary_nudges", 0) or 0)
    return ("nudge", _HANDOFF_SUMMARY_NUDGE) if attempts < _HANDOFF_MAX_NUDGES else ("notice", _HANDOFF_SUMMARY_NOTICE)


def _withhold_answer(agent: Any, final_msg: dict[str, Any], conversation_history: Any) -> None:
    """Keep the rejected document OUT of the conversation history.

    Persisting it -- even as a hidden row -- re-seeds the pattern: the next turn's request would
    carry a worked example of "write a session handoff", and models copy what they see (measured:
    affected sessions average ~1.75 recaps, climbing 1 -> 2 -> 3 turn by turn). The text survives
    in ``pending_verification_response`` for budget fallback and in the wire capture for forensics,
    so withholding it from history costs no evidence.
    """
    try:
        agent._flush_messages_to_session_db([], conversation_history)
    except Exception:
        logger.debug("handoff-summary withhold flush failed", exc_info=True)


def apply_stop_gates(
    agent: Any, final_msg: dict[str, Any], *, final_response: Any, messages: list[dict[str, Any]],
    conversation_history: Any, pending_verification_response: Any,
    pending_verification_response_previewed: Any,
) -> StopGateVerdict:
    """Run verify-on-stop → pre_verify hook → kanban stop guard, in that order. Nudges
    are user-role rows appended only after the assistant answer row, so role alternation
    holds. Hook lookups are imported lazily from their origin modules (tests patch them
    there)."""

    def _continue(nudge: str, flag: str) -> StopGateVerdict:
        """Append the synthetic nudge row and hand the turn back to the loop."""
        append_message(messages, {"role": "user", "content": nudge, flag: True})
        agent._session_messages = messages
        # Keep the answer only as a budget-exhaustion fallback; clear ``final_response`` so
        # the finalizer can tell this gate from error exits. Mark previewed only if the
        # candidate is reused (#61631).
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            # Prefix semantics on purpose: the streamed prefix IS on screen here. The gateway
            # interim verdict uses _interim_content_fully_streamed instead (#88954).
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    _handoff_action = _handoff_summary_action(agent, final_response, messages)
    if _handoff_action is not None:
        _kind, _text = _handoff_action
        if _kind == "nudge":
            agent._handoff_summary_nudges = int(getattr(agent, "_handoff_summary_nudges", 0) or 0) + 1
            final_msg["finish_reason"] = "handoff_summary_required"
            _withhold_answer(agent, final_msg, conversation_history)
            append_message(messages, {"role": "user", "content": _text, "_handoff_summary_synthetic": True})
            agent._session_messages = messages
            logger.warning(
                "handoff-summary guard: rejected a conversation recap (%d chars) and asked for the "
                "real answer (attempt %d)",
                len(final_response or ""), agent._handoff_summary_nudges,
            )
            return StopGateVerdict(
                continue_turn=True, final_response=None,
                pending_verification_response=final_response,
                pending_verification_response_previewed=agent._interim_content_was_streamed(final_response or ""),
            )
        final_msg["content"] = _text
        if final_msg.get("api_content") is not None:
            final_msg["api_content"] = _text
        logger.warning(
            "handoff-summary guard: the recap came back after a nudge (%d chars); replaced it with "
            "an explicit notice instead of treating it as the answer",
            len(final_response or ""),
        )
        return StopGateVerdict(
            continue_turn=False, final_response=_text,
            pending_verification_response=pending_verification_response,
            pending_verification_response_previewed=pending_verification_response_previewed,
        )

    _lsp_note = _late_lsp_verdicts(agent)
    if _lsp_note:
        agent._lsp_turn_end_notes = getattr(agent, "_lsp_turn_end_notes", 0) + 1  # int: checked above
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "late-LSP interim flush failed"
        )
        # A durable row (no scaffolding flag): it is never stripped, so no sent byte changes later.
        append_message(messages, {"role": "user", "content": _lsp_note})
        agent._session_messages = messages
        logger.debug("late LSP verdicts delivered before the final answer (%d)", agent._lsp_turn_end_notes)
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(final_response or ""),
        )

    _verify_nudge = _verify_on_stop_nudge(agent)
    if _verify_nudge:
        agent._verification_stop_nudges = getattr(agent, "_verification_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "verify-on-stop interim flush failed"
        )
        verdict = _continue(_verify_nudge, "_verification_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("verification stop-loop nudge issued (attempt %d)", agent._verification_stop_nudges)
        return verdict

    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    _verify_nudge2 = _pre_verify_nudge(agent, final_response, _attempt)
    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_verify interim flush failed"
        )
        verdict = _continue(_verify_nudge2, "_pre_verify_synthetic")
        logger.debug("pre_verify nudge issued (attempt %d)", agent._pre_verify_nudges)
        return verdict

    _kanban_nudge = _kanban_stop_nudge(agent, messages)
    if _kanban_nudge:
        agent._kanban_stop_nudges = getattr(agent, "_kanban_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "kanban_terminal_required"
        final_msg["_kanban_stop_synthetic"] = True
        append_message(messages, final_msg)
        verdict = _continue(_kanban_nudge, "_kanban_stop_synthetic")
        logger.info(
            "kanban stop-loop nudge issued (attempt %d) task=%s",
            agent._kanban_stop_nudges,
            os.environ.get("HERMES_KANBAN_TASK", ""),
        )
        agent._emit_diagnostic_status(
            "⚠️ Kanban worker tried to exit without a terminal board call "
            "(kanban_complete/kanban_request_review/kanban_block) — nudging to finish"
        )
        return verdict
    return StopGateVerdict(
        continue_turn=False, final_response=final_response,
        pending_verification_response=pending_verification_response,
        pending_verification_response_previewed=pending_verification_response_previewed,
    )
