"""Tests for kiro_crew.board.autorun: which stories get picked up without
a human clicking "Run next job", and which are correctly left alone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.board import autorun as board_autorun
from kiro_crew.board import engine as board_engine
from kiro_crew.board import state as board_state
from kiro_crew.board.stories import StorySummary


def _story(**overrides) -> StorySummary:
    base = dict(
        id="s-1", title="Story", status="open", epic_id="e-1", epic_title="Epic",
        project_path="/proj", phase="pipeline", phase_label="Pipeline", current_run=None,
        priority=None, owner="", pending_open_questions=False, recommendation=None,
        start_date=None, due_date=None, rank=None, story_points=None, depends_on=[],
    )
    base.update(overrides)
    return StorySummary(**base)


class TestShouldAutorun:
    @pytest.mark.asyncio
    async def test_backlog_story_is_always_eligible(self, tmp_path):
        story = _story(phase=None, phase_label="Backlog")
        assert await board_autorun._should_autorun(str(tmp_path), story) is True

    @pytest.mark.asyncio
    async def test_manual_phase_is_never_eligible(self, tmp_path):
        for phase in ("review", "done"):
            story = _story(phase=phase, phase_label=phase.title())
            assert await board_autorun._should_autorun(str(tmp_path), story) is False

    @pytest.mark.asyncio
    async def test_already_running_is_not_eligible(self, tmp_path):
        story = _story(current_run={"status": "running"})
        assert await board_autorun._should_autorun(str(tmp_path), story) is False

    @pytest.mark.asyncio
    async def test_labeled_but_never_run_is_eligible(self, tmp_path):
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {"current_run": None, "history": []})
        assert await board_autorun._should_autorun(str(tmp_path), story) is True

    @pytest.mark.asyncio
    async def test_missing_status_self_heals_immediately(self, tmp_path):
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "missing"}],
        })
        assert await board_autorun._should_autorun(str(tmp_path), story) is True

    @pytest.mark.asyncio
    async def test_cancelled_is_never_auto_resumed(self, tmp_path):
        # An explicit stop is a real stop, not a block waiting on an
        # answer - this module must never override it on its own.
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "cancelled"}],
        })
        assert await board_autorun._should_autorun(str(tmp_path), story) is False

    @pytest.mark.asyncio
    async def test_gate_failed_waits_for_a_new_comment_first_tick(self, tmp_path):
        # The first time autorun notices the block, it must NOT treat the
        # block's own final comment (confirm's "GATE: FAIL ...") as "a
        # human just acted" - it records the baseline and waits.
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "gate_failed"}],
        })
        with patch.object(board_autorun, "run_bd", AsyncMock(return_value=[{"text": "GATE: FAIL still broken"}])):
            result = await board_autorun._should_autorun(str(tmp_path), story)
        assert result is False
        data = board_state.load_history(str(tmp_path), story.id)
        assert data["autorun_seen_comment_count"] == 1

    @pytest.mark.asyncio
    async def test_gate_failed_retries_once_a_new_comment_appears(self, tmp_path):
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "gate_failed"}],
            "autorun_seen_comment_count": 1,
        })
        with patch.object(board_autorun, "run_bd", AsyncMock(return_value=[
            {"text": "GATE: FAIL still broken"}, {"text": "fixed it, try again"},
        ])):
            result = await board_autorun._should_autorun(str(tmp_path), story)
        assert result is True
        data = board_state.load_history(str(tmp_path), story.id)
        assert data["autorun_seen_comment_count"] == 2

    @pytest.mark.asyncio
    async def test_gate_failed_stays_blocked_with_no_new_comment(self, tmp_path):
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "gate_failed"}],
            "autorun_seen_comment_count": 1,
        })
        with patch.object(board_autorun, "run_bd", AsyncMock(return_value=[{"text": "GATE: FAIL still broken"}])):
            result = await board_autorun._should_autorun(str(tmp_path), story)
        assert result is False

    @pytest.mark.asyncio
    async def test_plain_failed_status_follows_the_same_comment_rule_as_gate_failed(self, tmp_path):
        story = _story()
        board_state.save_history(str(tmp_path), story.id, {
            "current_run": None, "history": [{"status": "failed"}],
            "autorun_seen_comment_count": 0,
        })
        with patch.object(board_autorun, "run_bd", AsyncMock(return_value=[{"text": "a human left a note"}])):
            result = await board_autorun._should_autorun(str(tmp_path), story)
        assert result is True


class TestAutorunTick:
    @pytest.mark.asyncio
    async def test_runs_eligible_stories_and_skips_ineligible_ones(self, tmp_path):
        eligible = _story(id="s-eligible", phase=None, phase_label="Backlog")
        blocked = _story(id="s-blocked", phase="review", phase_label="Review")
        board_state.save_history(str(tmp_path), blocked.id, {"current_run": None, "history": []})

        with patch.object(board_autorun, "list_stories", AsyncMock(return_value=[eligible, blocked])), \
             patch.object(board_engine, "run_job", AsyncMock(return_value="task-1")) as mocked_run:
            await board_autorun.autorun_tick(task_runner=object(), project_paths=[str(tmp_path)])

        mocked_run.assert_called_once_with(mocked_run.call_args[0][0], str(tmp_path), "s-eligible")

    @pytest.mark.asyncio
    async def test_a_board_error_for_one_story_does_not_stop_the_rest(self, tmp_path):
        first = _story(id="s-1", phase=None, phase_label="Backlog")
        second = _story(id="s-2", phase=None, phase_label="Backlog")

        async def fake_run_job(task_runner, project_path, story_id):
            if story_id == "s-1":
                raise board_engine.BoardError("race lost")
            return "task-2"

        with patch.object(board_autorun, "list_stories", AsyncMock(return_value=[first, second])), \
             patch.object(board_engine, "run_job", side_effect=fake_run_job) as mocked_run:
            await board_autorun.autorun_tick(task_runner=object(), project_paths=[str(tmp_path)])

        assert mocked_run.call_count == 2

    @pytest.mark.asyncio
    async def test_a_listing_failure_for_one_project_does_not_stop_the_others(self, tmp_path):
        eligible = _story(id="s-ok", phase=None, phase_label="Backlog")

        async def fake_list_stories(project_path):
            if project_path == "/broken":
                raise RuntimeError("bd exploded")
            return [eligible]

        with patch.object(board_autorun, "list_stories", side_effect=fake_list_stories), \
             patch.object(board_engine, "run_job", AsyncMock(return_value="task-1")) as mocked_run:
            await board_autorun.autorun_tick(task_runner=object(), project_paths=["/broken", str(tmp_path)])

        mocked_run.assert_called_once()
