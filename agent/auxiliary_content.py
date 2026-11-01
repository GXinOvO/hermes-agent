"""Visible content extraction shared by auxiliary callers and checkpoint validation."""


def extract_visible_content(response) -> str:
    """Extract the visible body, excluding inline reasoning, without consulting reasoning fields."""
    from agent.auxiliary_client import _coerce_llm_message, _message_field

    msg = _coerce_llm_message(response)
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg.strip()
    raw = _message_field(msg, "content")
    if not isinstance(raw, str):
        raw = str(raw) if raw else ""
    content = raw.strip()
    if content:
        from agent.agent_runtime_helpers import _REASONING_BLOCK_PATTERNS

        for pattern in _REASONING_BLOCK_PATTERNS:
            content = pattern.sub("", content)
        return content.strip()
    return ""
