"""Read caches and guard observations whose source context was discarded."""
from __future__ import annotations

import contextlib
from typing import Any


def reset_read_context(agent: Any, task_id: str) -> None:
    """Re-arm full reads after a committed rewrite, preserving failures and turn caps."""
    if guardrails := getattr(agent, "_tool_guardrails", None):
        guardrails.reset_read_history()
    with contextlib.suppress(Exception):
        from tools.file_tools_read_tracking import reset_file_dedup
        reset_file_dedup(task_id)
    if session_id := getattr(agent, "session_id", None):
        with contextlib.suppress(Exception):
            from tools.computer_use.tool import reset_screenshot_dedup
            reset_screenshot_dedup(session_id)
    with contextlib.suppress(Exception):
        from tools.skills_tool import reset_skill_view_dedup
        reset_skill_view_dedup(task_id)
