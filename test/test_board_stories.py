"""Tests for kiro_crew.board.stories.list_stories."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.board import stories as board_stories


@pytest.mark.asyncio
async def test_lists_only_task_type_issues_with_a_parent(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open"},
        {"id": "epic-1.2", "title": "Not a story", "issue_type": "bug",
         "parent": "epic-1", "status": "open"},
        {"id": "orphan-task", "title": "No parent", "issue_type": "task", "status": "open"},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert [s.id for s in summaries] == ["epic-1.1"]
    assert summaries[0].epic_title == "Epic"
    assert summaries[0].phase is None
    assert summaries[0].phase_label == "Backlog"


@pytest.mark.asyncio
async def test_excludes_a_grape_sub_task_parented_to_a_story_not_an_epic(tmp_path):
    # Regression, caught live: rocket-confirm-plan persists each grape as its
    # own task-type issue parented to the STORY (see its SKILL.md step 3),
    # not the epic. Without checking the parent's own issue_type, that grape
    # was indistinguishable from a real story and showed up as a second,
    # confusing "epic" of its own on the timeline (its title truncated to
    # read as a duplicate of the real story above it).
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open"},
        {"id": "epic-1.1.1", "title": "Grape 1: do the thing", "issue_type": "task",
         "parent": "epic-1.1", "status": "open"},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert [s.id for s in summaries] == ["epic-1.1"]


@pytest.mark.asyncio
async def test_reads_phase_from_label(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:pipeline"]},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].phase == "pipeline"
    assert summaries[0].phase_label == "Pipeline"


@pytest.mark.asyncio
async def test_carries_priority_and_owner_through(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "priority": 1, "owner": "amaan"},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].priority == 1
    assert summaries[0].owner == "amaan"


@pytest.mark.asyncio
async def test_missing_priority_and_owner_default_sanely(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open"},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].priority is None
    assert summaries[0].owner == ""


@pytest.mark.asyncio
async def test_flags_pending_open_questions_in_pipeline_phase(tmp_path):
    # plan has no gate - a story can sit in the pipeline phase with
    # questions unanswered until confirm_plan (also in this phase) runs
    # and actually checks them. That's the phase this flag is scoped to;
    # see stories.py's own comment.
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:pipeline"]},
    ]
    comments = [{"text": "OPEN QUESTIONS\n\n1. Is this ok?\n"}]

    async def fake_run_bd(project_path, *args):
        if args[0] == "list":
            return rows
        if args == ("comments", "epic-1.1"):
            return comments
        raise AssertionError(f"unexpected bd call: {args}")

    with patch.object(board_stories, "run_bd", side_effect=fake_run_bd):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].pending_open_questions is True


@pytest.mark.asyncio
async def test_does_not_flag_once_answered(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:pipeline"]},
    ]
    comments = [
        {"text": "OPEN QUESTIONS\n\n1. Is this ok?\n"},
        {"text": "1. yes"},
    ]

    async def fake_run_bd(project_path, *args):
        if args[0] == "list":
            return rows
        if args == ("comments", "epic-1.1"):
            return comments
        raise AssertionError(f"unexpected bd call: {args}")

    with patch.object(board_stories, "run_bd", side_effect=fake_run_bd):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].pending_open_questions is False


@pytest.mark.asyncio
async def test_does_not_check_comments_outside_pipeline(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:review"]},
    ]

    async def fake_run_bd(project_path, *args):
        if args[0] == "list":
            return rows
        raise AssertionError(f"unexpected bd call outside pipeline: {args}")

    with patch.object(board_stories, "run_bd", side_effect=fake_run_bd):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].pending_open_questions is False


@pytest.mark.asyncio
async def test_bd_failure_returns_empty_list(tmp_path):
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=None)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries == []


@pytest.mark.asyncio
async def test_a_stale_running_entry_self_heals_on_every_list_read(tmp_path):
    from kiro_crew.board import state as board_state

    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:pipeline"]},
    ]
    board_state.save_history(str(tmp_path), "epic-1.1", {
        "current_run": {
            "phase": "pipeline", "task_key": "implement", "iteration": 0,
            "task_id": "task-orphaned", "status": "running",
            "started_at": "t1", "finished_at": None,
        },
        "history": [],
    })

    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))

    assert summaries[0].current_run is None
    data = board_state.load_history(str(tmp_path), "epic-1.1")
    assert data["history"][-1]["status"] == "missing"
    assert data["history"][-1]["task_id"] == "task-orphaned"


@pytest.mark.asyncio
async def test_reads_timeline_scheduling_fields_from_due_at_and_metadata(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open",
         "due_at": "2026-10-15T00:00:00Z",
         "metadata": {"start_date": "2026-10-01", "timeline_rank": 2.5, "story_points": 8}},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].start_date == "2026-10-01"
    assert summaries[0].due_date == "2026-10-15T00:00:00Z"
    assert summaries[0].rank == 2.5
    assert summaries[0].story_points == 8.0


@pytest.mark.asyncio
async def test_missing_scheduling_fields_default_to_none(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open"},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].start_date is None
    assert summaries[0].due_date is None
    assert summaries[0].rank is None
    assert summaries[0].story_points is None
    assert summaries[0].depends_on == []


@pytest.mark.asyncio
async def test_a_non_numeric_rank_is_dropped_not_raised(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "metadata": {"timeline_rank": "not-a-number"}},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].rank is None


@pytest.mark.asyncio
async def test_depends_on_excludes_the_parent_child_epic_link(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.2", "title": "Story B", "issue_type": "task",
         "parent": "epic-1", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "dependencies": [
            {"issue_id": "epic-1.1", "depends_on_id": "epic-1", "type": "parent-child"},
            {"issue_id": "epic-1.1", "depends_on_id": "epic-1.2", "type": "blocks"},
        ]},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    story_a = next(s for s in summaries if s.id == "epic-1.1")
    assert story_a.depends_on == ["epic-1.2"]
