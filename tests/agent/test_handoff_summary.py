"""Invariants for handoff-document recognition and elision (``agent/handoff_summary.py``).

The same shape test serves two callers: the stop gate withholds a document offered *as* the
answer, and ``session_search`` elides one that a replayed transcript carries into the turn. Both
must recognise a real document and neither may damage an ordinary answer.
"""

from __future__ import annotations

import json

from agent.handoff_summary import (
    MIN_CHARS,
    looks_like_handoff,
    redact_handoff_documents,
    title_hits,
)


def _document(chars: int = 2400) -> str:
    body = (
        "<analysis>\nLet me chronologically analyze this conversation.\n</analysis>\n<summary>\n"
        "1. Primary Request and Intent:\n   explain\n\n"
        "2. Key Technical Concepts:\n   turn loop\n\n"
        "3. Files and Code Sections:\n   agent/turn_stop_gates.py\n\n"
        "7. Pending Tasks:\n   none\n\n"
        "9. Optional Next Step:\n   commit and push.\n"
        + ("detail " * max(0, chars // 7 - 40))
        + "\n</summary>"
    )
    return body


def test_recognises_a_document_and_not_an_answer_about_one():
    assert looks_like_handoff(_document()) is True
    # A long answer that merely discusses the tags is not a document.
    chatter = ("The tag <analysis> wraps a scratchpad. " * 80) + "That is the answer."
    assert looks_like_handoff(chatter) is False
    # Under the size floor.
    assert looks_like_handoff("<analysis>x</analysis><summary>ok</summary>") is False
    # Wrapper without the recap headings (a genuine short summary request).
    assert looks_like_handoff("<analysis>a</analysis><summary>" + "y" * MIN_CHARS + "</summary>") is False
    assert looks_like_handoff(None) is False


def test_title_hits_counts_headings_only():
    assert title_hits("Primary Request and Intent and Optional Next Step") == 2
    assert title_hits("nothing here") == 0


def test_elides_an_embedded_document_and_keeps_the_rest():
    """A replayed tool result embeds the document inside JSON; only the document may go."""
    inner = json.dumps({"output": "before\n" + _document() + "\nafter"}, ensure_ascii=False)
    cleaned, count = redact_handoff_documents(inner)
    assert count == 1
    assert "Optional Next Step" not in cleaned
    assert "handoff document elided" in cleaned
    # The genuine surrounding transcript survives.
    assert "before" in cleaned and "after" in cleaned


def test_leaves_ordinary_text_and_short_tag_mentions_untouched():
    plain = "a normal assistant answer with no wrapper at all" * 100
    assert redact_handoff_documents(plain) == (plain, 0)
    chatter = ("The tag <analysis> opens a block, </summary> closes it. " * 60)
    assert redact_handoff_documents(chatter) == (chatter, 0)


def test_session_search_shaping_elides_documents():
    """The tool's own shaping applies the elision, so a replayed recap never re-enters context."""
    from tools.session_search_tool import _shape_message

    shaped = _shape_message({"id": 1, "role": "assistant", "content": _document(), "timestamp": 0})
    assert "Optional Next Step" not in shaped["content"]
    assert "handoff document elided" in shaped["content"]

    kept = _shape_message({"id": 2, "role": "assistant", "content": "plain reply", "timestamp": 0})
    assert kept["content"] == "plain reply"
