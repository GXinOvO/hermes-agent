"""Read-only compatibility for legacy empty-response recovery scaffolding."""

from __future__ import annotations

from collections.abc import Mapping


EMPTY_TOOL_RESPONSE_NUDGE = (
    "You just executed tool calls but returned an empty response. Please process the tool "
    "results above and continue with the task."
)
_PRESERVED_FIELDS = (
    "tool_calls", "tool_call_id", "tool_name", "function_call", "display_kind", "display_metadata",
    "platform_message_id", "message_id", "api_content", "_compressed_summary", "codex_message_items",
    "effect_disposition", "_db_persisted", "codex_reasoning_items",
)


def is_legacy_empty_recovery_pair(assistant, user) -> bool:
    """Exact old signature, with no known provenance or other semantic payload.

    Untyped historical text cannot establish authorship: this is a compatibility
    heuristic. Reasoning on the empty assistant is allowed; known user provenance,
    tools and typed rows always survive.
    """
    if not isinstance(assistant, Mapping) or not isinstance(user, Mapping):
        return False
    if (assistant.get("role"), assistant.get("content"), user.get("role"), user.get("content")) != (
        "assistant", "(empty)", "user", EMPTY_TOOL_RESPONSE_NUDGE,
    ):
        return False
    if assistant.get("session_id") != user.get("session_id"):
        return False
    return not any(message.get(key) for message in (assistant, user) for key in _PRESERVED_FIELDS)


def empty_recovery_pair_indexes(messages) -> set[int]:
    """Indexes of adjacent legacy pairs; inputs and durable identities stay untouched."""
    return {index for right in range(1, len(messages))
            if is_legacy_empty_recovery_pair(messages[right - 1], messages[right])
            for index in (right - 1, right)}


def without_legacy_empty_recovery_pairs(rows):
    """Filter fetched SQLite rows before content sanitization, or in-memory dicts."""
    # Decoded replay carries _db_persisted: its original bytes were already checked,
    # and sanitize/strip must never turn a near-match into a second-pass match.
    rows = list(rows)
    messages = [dict(row) if hasattr(row, "keys") else row for row in rows]
    hidden = empty_recovery_pair_indexes(messages)
    return [row for index, row in enumerate(rows) if index not in hidden]


def project_legacy_empty_recovery_pairs(messages):
    """Keep physical rows/counts, marking only the display copies of legacy pairs."""
    hidden = empty_recovery_pair_indexes(messages)
    return [{**message, "display_kind": "hidden"} if index in hidden else message
            for index, message in enumerate(messages)]


def recovery_compatible_message_page(db, session_id, *, limit, offset=0, latest=False,
                                     include_compacted=False, include_ancestors=False):
    """One bounded read with one neighbor on each page edge; storage reads stay raw."""
    if limit == 0:
        return []
    padded_offset = max(0, offset - 1)
    messages = db.get_messages(session_id, limit=limit + 2, offset=padded_offset, latest=latest,
                               include_compacted=include_compacted, include_ancestors=include_ancestors)
    messages = project_legacy_empty_recovery_pairs(messages)
    start = offset - padded_offset
    return messages[::-1][start:start + limit][::-1] if latest else messages[start:start + limit]
