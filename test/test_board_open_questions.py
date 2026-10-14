"""Tests for kiro_crew.board.open_questions - a Python port of
website/src/apps/board/openQuestions.ts's detection regex, mirrored here so
the board list can flag a story as needing a human without shipping full
comment text on every poll. Keep these cases in sync with
openQuestions.test.ts."""

from __future__ import annotations

from kiro_crew.board.open_questions import (
    has_pending_open_questions,
    last_comment_has_pending_open_questions,
)

REAL_PLAN_COMMENT = """## PLAN DRAFT: crew-rocket-4da.1

DECISION: Language is Python.
REASON: No existing application code in crew-rocket.

---

## OPEN QUESTIONS

1. **Language:** Python is assumed based on absence of evidence. Is there a language constraint or preference for the calculator?
2. **Module location:** The plan places the calculator at `crew-rocket/calculator/`. Should it live somewhere else?

---

To proceed: answer these questions as a comment on crew-rocket-4da.1, then invoke `rocket-confirm-plan crew-rocket-4da.1`.
"""


class TestHasPendingOpenQuestions:
    def test_true_for_the_real_plan_comment_shape(self):
        assert has_pending_open_questions(REAL_PLAN_COMMENT) is True

    def test_false_with_no_heading(self):
        assert has_pending_open_questions("just a regular comment, no heading here") is False

    def test_case_insensitive_and_tolerates_markdown_heading_prefix(self):
        assert has_pending_open_questions("### open questions\n\n1. Is this ok?\n") is True

    def test_true_with_no_trailing_hr_at_all(self):
        text = "OPEN QUESTIONS\n\n1. Only one question here, nothing after it."
        assert has_pending_open_questions(text) is True

    def test_heading_with_no_numbered_items_is_false(self):
        assert has_pending_open_questions("OPEN QUESTIONS\n\nNothing numbered here.") is False


class TestLastCommentHasPendingOpenQuestions:
    def test_no_comments_is_false(self):
        assert last_comment_has_pending_open_questions([]) is False

    def test_true_when_the_last_comment_carries_them(self):
        comments = [{"text": "earlier note"}, {"text": REAL_PLAN_COMMENT}]
        assert last_comment_has_pending_open_questions(comments) is True

    def test_false_once_a_later_comment_has_superseded_them(self):
        comments = [
            {"text": REAL_PLAN_COMMENT},
            {"text": "1. Python is fine\n2. keep it here"},
        ]
        assert last_comment_has_pending_open_questions(comments) is False
