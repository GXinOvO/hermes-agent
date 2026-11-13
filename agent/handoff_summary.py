"""Recognise — and elide — a model-written "context handoff" document.

A model can answer a turn with a recap of the whole conversation instead of the answer: an
``<analysis>…</analysis><summary>…</summary>`` body carrying the recap headings below. The
provider reports it as an ordinary answer (``finish_reason=stop``, no tool calls, non-empty
content), so nothing downstream can tell it apart from a real reply.

Two places need the same judgement, which is why it lives here:

* ``agent/turn_stop_gates`` withholds such a document when it *is* the visible answer.
* ``tools/session_search_tool`` elides one that a *past transcript* carries into this turn —
  a persisted recap is a worked example of the pattern, and models copy what they see.

Detection is deliberately shape-based and conservative: the wrapper, a body long enough to be a
document, and several recap headings. A short reply that merely mentions the tags never matches.
"""
from __future__ import annotations

import re

# The headings of the recap format models reproduce (Claude-Code-style compaction output).
SECTION_TITLES = (
    "primary request and intent",
    "key technical concepts",
    "files and code sections",
    "errors and fixes",
    "problem solving",
    "all user messages",
    "pending tasks",
    "current work",
    "optional next step",
)

MIN_CHARS = 1500
MIN_TITLES = 3

_SPAN = re.compile(r"<analysis\b.*?</summary>", re.IGNORECASE | re.DOTALL)
_OPEN = "<analysis"
_CLOSE = "</summary>"

ELISION = (
    "[handoff document elided: {chars} chars — this was a model-written recap of a conversation, "
    "not a message a user or tool produced. Re-ask for the specific fact you need.]"
)


def title_hits(text: str) -> int:
    """How many recap headings ``text`` carries."""
    if not isinstance(text, str):
        return 0
    lowered = text.lower()
    return sum(1 for title in SECTION_TITLES if title in lowered)


def looks_like_handoff(text: object, *, min_chars: int = MIN_CHARS, min_titles: int = MIN_TITLES) -> bool:
    """True when ``text`` is a handoff document rather than an answer about one."""
    if not isinstance(text, str) or len(text) < min_chars:
        return False
    lowered = text.lower()
    if _OPEN not in lowered:
        return False
    if _CLOSE not in lowered and "<summary" not in lowered:
        return False
    return title_hits(lowered) >= min_titles


def redact_handoff_documents(text: str) -> tuple[str, int]:
    """Replace every embedded handoff document with a short placeholder; return the count.

    A transcript read back by ``session_search`` can carry these inline (inside JSON tool results
    too), so the substitution works on the raw string rather than a parsed message body.
    """
    if not isinstance(text, str) or _OPEN not in text.lower():
        return text, 0

    count = 0
    last = 0
    pieces: list[str] = []
    for match in _SPAN.finditer(text):
        span = match.group(0)
        if not looks_like_handoff(span):
            continue
        pieces.append(text[last:match.start()])
        pieces.append(ELISION.format(chars=len(span)))
        last = match.end()
        count += 1
    if not count:
        return text, 0
    pieces.append(text[last:])
    return "".join(pieces), count
