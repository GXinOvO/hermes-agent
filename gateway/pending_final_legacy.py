"""Conservative migration of unproven crash-left markers; never grants send authority."""
from contextlib import nullcontext
from typing import Optional
import asyncio
import logging
import time

logger = logging.getLogger("gateway.run")


async def ledger_crash_left_replies(self, max_age_seconds: int) -> int:
    """Settle every marked turn whose final reply was persisted and clear its marker, so
    auto-resume does not regenerate it: a reply live delivery would have suppressed is owed
    nothing, any other goes to the delivery ledger for the boot sweep. Without the ledger a
    presentable reply stays marked and resumes."""
    from gateway.delivery_ledger import compute_obligation_id, ledger_enabled, record_crash_left_reply
    ledger_on = await asyncio.to_thread(ledger_enabled)
    cutoff = time.time() - max_age_seconds  # older markers are cleared, never acted on
    with self.session_store._lock:
        self.session_store._ensure_loaded_locked()
        marked = [
            (e.session_key, e.session_id, e.active_turn_token, e.active_turn_started_at, e.origin,
             e.transport_profile)
            for e in self.session_store._entries.values()
            if e.active_turn_token and e.active_turn_started_at and e.origin and not e.suspended
        ]
    ledgered = 0
    for key, session_id, token, started_at, origin, profile in marked:
        from gateway.pending_final import settled_for_session
        if await asyncio.to_thread(settled_for_session, self, session_id, token):
            await self.async_session_store.clear_turn_active(key, token)
            await self.async_session_store.clear_resume_pending(key)
            ledgered += 1
            continue
        started = started_at.timestamp()  # aware UTC marker; a pre-upgrade naive one reads as local
        if started < cutoff:
            continue
        decision = await self.async_session_store.get_session_metadata(key, "room_silence_decision", {})
        text = self._crash_left_reply(await self.async_session_store.load_transcript(session_id),
                                      started, origin, silence_decision=decision, active_token=token)
        if text is None or (text and not ledger_on):
            continue  # no final reply to deliver: the turn resumes
        if text:
            await asyncio.to_thread(
                record_crash_left_reply,
                obligation_id=compute_obligation_id(key, f"crash:{token}", text), session_key=key,
                platform=str(getattr(origin.platform, "value", origin.platform)), chat_id=origin.chat_id,
                thread_id=origin.thread_id, content=text, since=started, adapter_profile=profile)
        if await self.async_session_store.clear_turn_active(key, token) and text:
            ledgered += 1
    return ledgered


def crash_left_reply(
    self, history: list, started: float, origin, *, silence_decision=None, active_token=None,
) -> Optional[str]:
    """What a crash-left turn owes, judged as live delivery would have: ``None`` when it never
    persisted a final reply after *started*; ``""`` when nothing would have been presented (a
    silence marker on a machinery turn or on a turn the adapter reported as not addressed to the
    bot, a muted diagnostic wake); else the text to send, with any other bare silence marker
    replaced by the same notice the live path sends."""
    from gateway.platforms.base import _strip_media_directives
    from gateway.response_filters import (
        is_intentional_silence_response, is_machinery_display_kind, silence_allowed,
        strip_trailing_silence_marker,
    )
    from gateway.run import _sanitize_gateway_final_response
    from gateway.run_turn import _unexpected_silence_reply
    from gateway.warning_notifications import diagnostic_turn_muted
    from hermes_cli.timefmt import coerce_epoch
    visible = [m for m in history if m.get("role") not in ("session_meta", "system")]
    last = visible[-1] if visible else {}
    if (last.get("role") != "assistant" or last.get("tool_calls") or not isinstance(last.get("content"), str)
            or (coerce_epoch(last.get("timestamp")) or 0) < started):
        return None
    prompt = next((m for m in reversed(visible) if m.get("role") == "user"), {})
    if is_machinery_display_kind(prompt.get("display_kind")):
        try:  # the owning profile's display policy, as the adapter reads it at delivery
            scope = self._media_delivery_scope_for_source(origin)
        except Exception:
            logger.debug("Crash-left reply: no routed scope for %s", origin.chat_id, exc_info=True)
            scope = nullcontext()
        with scope:
            if diagnostic_turn_muted(prompt.get("display_metadata"), origin.platform):
                return ""
    if is_intentional_silence_response(last["content"]):
        decision = silence_decision or {}
        proven_silent = (active_token is not None and decision.get("token") == active_token
                         and decision.get("timestamp") == last.get("timestamp")
                         and decision.get("silent") is True)
        silent_ok = silence_allowed(
            prompt.get("display_kind"), (prompt.get("display_metadata") or {}).get("reply_expected"),
            allow_silence=proven_silent, agent_result={})
        return "" if silent_ok else _unexpected_silence_reply()
    reply = strip_trailing_silence_marker(last["content"])
    return _strip_media_directives(_sanitize_gateway_final_response(origin.platform, reply)).strip() or None

