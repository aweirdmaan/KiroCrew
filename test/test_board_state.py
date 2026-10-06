"""Tests for kiro_crew.board.state: beads phase label + history file."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.board import state as board_state


def _row(story_id: str, labels: list[str] | None = None) -> dict:
    row = {"id": story_id}
    if labels:
        row["labels"] = labels
    return [row]


class TestGetSetPhase:
    @pytest.mark.asyncio
    async def test_no_labels_is_backlog(self):
        with patch.object(board_state, "run_bd", AsyncMock(return_value=_row("s-1"))):
            phase = await board_state.get_phase("/proj", "s-1")
        assert phase is None

    @pytest.mark.asyncio
    async def test_reads_phase_label(self):
        with patch.object(board_state, "run_bd", AsyncMock(return_value=_row("s-1", ["phase:planning"]))):
            phase = await board_state.get_phase("/proj", "s-1")
        assert phase == "planning"

    @pytest.mark.asyncio
    async def test_ignores_non_phase_labels(self):
        rows = _row("s-1", ["priority:high", "phase:verification", "team:x"])
        with patch.object(board_state, "run_bd", AsyncMock(return_value=rows)):
            phase = await board_state.get_phase("/proj", "s-1")
        assert phase == "verification"

    @pytest.mark.asyncio
    async def test_set_phase_removes_old_and_adds_new(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            if args[0] == "list":
                return _row("s-1", ["phase:planning"])
            return [{"status": "ok"}]

        with patch.object(board_state, "run_bd", side_effect=fake_run_bd):
            ok = await board_state.set_phase("/proj", "s-1", "plan_review")

        assert ok is True
        assert ("label", "remove", "s-1", "phase:planning") in calls
        assert ("label", "add", "s-1", "phase:plan_review") in calls

    @pytest.mark.asyncio
    async def test_set_phase_with_no_prior_label_just_adds(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            if args[0] == "list":
                return _row("s-1")
            return [{"status": "ok"}]

        with patch.object(board_state, "run_bd", side_effect=fake_run_bd):
            await board_state.set_phase("/proj", "s-1", "grooming")

        assert not any(c[0] == "label" and c[1] == "remove" for c in calls)
        assert ("label", "add", "s-1", "phase:grooming") in calls

    @pytest.mark.asyncio
    async def test_set_phase_returns_false_on_bd_failure(self):
        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return _row("s-1")
            return None  # label add failed

        with patch.object(board_state, "run_bd", side_effect=fake_run_bd):
            ok = await board_state.set_phase("/proj", "s-1", "grooming")
        assert ok is False


class TestGateComment:
    @pytest.mark.asyncio
    async def test_no_comments_is_none(self):
        with patch.object(board_state, "run_bd", AsyncMock(return_value=[])):
            result = await board_state.last_gate_comment("/proj", "s-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_finds_the_last_gate_line_across_comments(self):
        comments = [
            {"text": "some unrelated note"},
            {"text": "GATE: FAIL needs answers"},
            {"text": "answered the questions"},
            {"text": "GATE: PASS looks good"},
        ]
        with patch.object(board_state, "run_bd", AsyncMock(return_value=comments)):
            result = await board_state.last_gate_comment("/proj", "s-1")
        assert result == "GATE: PASS looks good"

    @pytest.mark.asyncio
    async def test_multiline_comment_gate_line_not_required_at_start(self):
        comments = [{"text": "some preamble\nGATE: PASS embedded\nmore text"}]
        with patch.object(board_state, "run_bd", AsyncMock(return_value=comments)):
            result = await board_state.last_gate_comment("/proj", "s-1")
        assert result == "GATE: PASS embedded"

    def test_gate_passed(self):
        assert board_state.gate_passed("GATE: PASS") is True
        assert board_state.gate_passed("GATE: PASS reason") is True
        assert board_state.gate_passed("GATE: FAIL") is False
        assert board_state.gate_passed(None) is False
        assert board_state.gate_passed("") is False


class TestHistoryFile:
    def test_missing_file_returns_defaults(self, tmp_path):
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data == {"current_run": None, "history": []}

    def test_round_trip(self, tmp_path):
        payload = {
            "current_run": {"phase": "planning", "task_key": "plan", "iteration": 0,
                             "task_id": "t-1", "status": "running",
                             "started_at": "now", "finished_at": None},
            "history": [{"phase": "grooming", "task_key": "ideate", "status": "passed"}],
        }
        board_state.save_history(str(tmp_path), "s-1", payload)
        loaded = board_state.load_history(str(tmp_path), "s-1")
        assert loaded == payload

    def test_corrupt_file_returns_defaults_not_an_exception(self, tmp_path):
        path = board_state.history_path(str(tmp_path), "s-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data == {"current_run": None, "history": []}

    def test_story_id_with_path_separators_is_sanitized(self, tmp_path):
        path = board_state.history_path(str(tmp_path), "../../etc/passwd")
        assert ".." not in path.parts
        assert str(path).startswith(str(tmp_path))
