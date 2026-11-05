"""Cron session ending reason: execution outcomes precede transcript shape."""
from __future__ import annotations

import logging
from typing import Any
from agent.tool_guardrails import ToolGuardrailDecision

logger = logging.getLogger('cron.scheduler')


def cron_session_end_reason(session_db: Any, agent: Any, session_id: str, job_id: str) -> str:
    """A visible framework stop is not a successful answer; preserve shape fallbacks."""
    decision = getattr(agent, '_tool_guardrail_halt_decision', None)
    if isinstance(decision, ToolGuardrailDecision) and decision.should_halt:
        return 'cron_guardrail_halt'
    # Only positively recognized bad shapes downgrade. Probe failures and unknown
    # vocabularies retain the established best-effort metadata behavior.
    try:
        lifecycle = session_db.session_lifecycle_statuses([session_id]).get(session_id)
        if lifecycle in ('interrupted', 'error', 'empty'):
            logger.warning("Job '%s': session ended without a final assistant message "
                           '(lifecycle=%s) — booking run as cron_incomplete_no_output', job_id, lifecycle)
            return 'cron_incomplete_no_output'
    except (Exception, KeyboardInterrupt) as exc:
        logger.debug("Job '%s': session lifecycle classification failed: %s", job_id, exc, exc_info=True)
    return 'cron_complete'
