"""Detects a pending "OPEN QUESTIONS" post - a direct port of the regex in
``website/src/apps/board/openQuestions.ts`` (keep the two in sync; the
frontend parses the questions out for the quick-answer panel, this module
only needs to know whether one is pending, to flag a story as needing a
human on the board list without shipping every story's full comment text
on every poll).
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{0,3}\s*OPEN QUESTIONS\s*$", re.IGNORECASE | re.MULTILINE)
_NUMBERED_ITEM_RE = re.compile(r"^\d+\.\s", re.MULTILINE)
_HR_RE = re.compile(r"\n\s*-{3,}\s*\n")


def has_pending_open_questions(text: str) -> bool:
    heading = _HEADING_RE.search(text)
    if heading is None:
        return False
    rest = text[heading.end():]
    hr = _HR_RE.search(rest)
    if hr is not None:
        rest = rest[:hr.start()]
    return _NUMBERED_ITEM_RE.search(rest) is not None


def last_comment_has_pending_open_questions(comments: list[dict]) -> bool:
    if not comments:
        return False
    last = comments[-1]
    text = last.get("text", "") if isinstance(last, dict) else ""
    return has_pending_open_questions(text) if isinstance(text, str) else False
