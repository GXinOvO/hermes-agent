"""Summary-hook dispatch and cancellation rollback for context compression."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.auxiliary_client import AuxiliaryExplicitCancellation

_REASONING_SUMMARY_TRUNCATION_MARKER = "reasoning_fallback_char_limit"

if TYPE_CHECKING:
    from agent.context_compressor import _HandoffScan


def _accepts_keyword_argument(callable_obj: Any, name: str) -> bool:
    """Return whether an inspectable callable accepts ``name`` as a keyword."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return True
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


class SummaryDispatchMixin:
    @staticmethod
    def _extract_summary_content(response: Any) -> str:
        """Reject a locally clipped reasoning fallback rather than checkpointing its prefix."""
        from agent.context_compressor import extract_content_or_reasoning
        from agent.auxiliary_content import extract_visible_content

        visible = extract_visible_content(response)
        if visible:
            return visible
        content = extract_content_or_reasoning(response)
        limit = 8_000
        if len(content) > limit:
            raise RuntimeError(
                f"Context compression summary was truncated ({_REASONING_SUMMARY_TRUNCATION_MARKER}): "
                f"reasoning fallback exceeds the {limit}-character safety limit; refusing a partial checkpoint"
            )
        return content

    def _summary_route_differs_from_main(self) -> bool:
        """Use the existing deployment identity; unknown route axes cannot prove a difference."""
        from agent.backend_identity import BackendIdentity, should_skip_candidate
        from hermes_cli.providers import normalize_provider

        route = getattr(self, "_last_aux_resolved_route", None) or {}
        if route.get("main_route_attempted") == "true":
            return False
        failed = BackendIdentity.build(
            provider=normalize_provider(route.get("provider") or ""),
            model=route.get("model") or self.summary_model or "",
            base_url=route.get("base_url"),
        )
        main = BackendIdentity.build(
            provider=normalize_provider(self.provider or ""), model=self.model, base_url=self.base_url,
        )
        if not failed.model:
            return False
        if failed.model != main.model:
            return True
        if not ((failed.provider and main.provider) or (failed.base_url and main.base_url)):
            return False
        return not should_skip_candidate(main, failed)

    def _apply_summary_route(self, call_kwargs: dict, pinned: Optional[dict[str, Any]] = None) -> None:
        """Pin the summary route onto ``call_kwargs`` for both compression entry points.

        After a fallback, an omitted route is NOT "use the main model": ``call_llm`` re-resolves
        ``auxiliary.compression`` from config — the model that just failed — and a second fallback
        is refused, so every later attempt aborts (#123362). Name the main runtime explicitly.
        A stall-fallback ``pinned`` route replaces the whole route: merged over the main runtime, the
        main api_key/base_url would ride into the fallback entry's call (to its host, or instead of it).
        The main route is complete even with empty fields: an authoritative task keeps compression
        policy but never fills an unset endpoint/key/wire mode from the failed aux route (#130895).
        """
        if pinned:
            # Clear first: a keyless pin (local server) resolves its own credential, never the main one.
            for key in ("provider", "model", "base_url", "api_key", "api_mode"):
                call_kwargs.pop(key, None)
            call_kwargs.update(pinned)
            return
        if self.summary_model:
            call_kwargs["model"] = self.summary_model
            return
        if not getattr(self, "_summary_model_fallen_back", False):
            return
        from agent.auxiliary_task_config import authoritative_auxiliary_task

        call_kwargs["task"] = authoritative_auxiliary_task(call_kwargs.get("task") or "compression")
        for key, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("base_url", self.base_url),
            ("api_key", self.api_key),
            ("api_mode", getattr(self, "api_mode", "")),
        ):
            if value:
                call_kwargs[key] = value

    def _summarize_window(
        self, messages: list[dict[str, Any]], turns_to_summarize: list[dict[str, Any]], scan: _HandoffScan,
        focus_topic: Optional[str], memory_context: str, bypass_cooldown: bool,
    ) -> Optional[str]:
        """Run the summary LLM; a cancellation rolls back the handoff scan's self-heal mutation first.
        A deterministic pin (repeated stall, #112420) skips the LLM: ``None`` lets Phase 3 insert the static
        fallback summary, or abort under ``abort_on_summary_failure`` exactly like a failed summary call."""
        from agent.context_compressor import take_deterministic_summary_pin
        if take_deterministic_summary_pin():
            # A detached stale attempt must not stamp error state or mutate the shared telemetry dict
            # the fallback owns; unwind as a cancellation before any write lands.
            from agent.conversation_compression import _raise_if_stale_attempt

            _raise_if_stale_attempt(self)
            # Surfaces through the fallback summary's reason line and the host's one-shot user warning.
            self._last_summary_error = (
                "summary model stalled on every route; deterministic fallback summary inserted"
            )
            telemetry = getattr(self, "_active_compression_telemetry", None)
            if isinstance(telemetry, dict):
                telemetry["failure_class"] = "stall_deterministic_fallback"
            return None
        # Focus-topic derivation scans user turns; only pay when a summary is generated.
        summary_kwargs: dict[str, Any] = {
            "focus_topic": focus_topic or self._derive_auto_focus_topic(messages),
            "memory_context": memory_context,
        }
        if _accepts_keyword_argument(self._generate_summary, "bypass_cooldown"):
            summary_kwargs["bypass_cooldown"] = bypass_cooldown
        try:
            return self._generate_summary(turns_to_summarize, **summary_kwargs)
        except AuxiliaryExplicitCancellation:
            # Cancellation is a true no-op: restore the scan's mutation before the exception escapes.
            # Guard by THIS attempt's ownership: a detached stale attempt (a fallback already claimed
            # and is working the compressor) must not revert summary state the newer attempt advanced.
            # The caller's own generation rides a ContextVar because shared compressor attributes can
            # only name the current owner, never the caller's attempt. Working-attempt comparison (not
            # the entry generation) so a no-op claim does not suppress the owning attempt's rollback.
            from agent.conversation_compression import _caller_attempt_is_current

            if _caller_attempt_is_current(self):
                self._previous_summary = scan.previous_summary_before
                self._summary_has_user_turn = scan.has_user_turn_before
            raise
