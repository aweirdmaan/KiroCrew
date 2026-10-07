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
async def test_reads_phase_from_label(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:implementation"]},
    ]
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=rows)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries[0].phase == "implementation"
    assert summaries[0].phase_label == "Implementation"


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
async def test_flags_pending_open_questions_in_implementation_phase(tmp_path):
    # Planning has no gate - a story always lands in Implementation the
    # moment `plan` finishes, whether or not a human answered yet. That's
    # the phase this flag is scoped to; see stories.py's own comment.
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:implementation"]},
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
         "parent": "epic-1", "status": "open", "labels": ["phase:implementation"]},
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
async def test_does_not_check_comments_outside_implementation(tmp_path):
    rows = [
        {"id": "epic-1", "title": "Epic", "issue_type": "epic", "status": "open"},
        {"id": "epic-1.1", "title": "Story A", "issue_type": "task",
         "parent": "epic-1", "status": "open", "labels": ["phase:planning"]},
    ]

    async def fake_run_bd(project_path, *args):
        if args[0] == "list":
            return rows
        raise AssertionError(f"unexpected bd call outside implementation: {args}")

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
         "parent": "epic-1", "status": "open", "labels": ["phase:implementation"]},
    ]
    board_state.save_history(str(tmp_path), "epic-1.1", {
        "current_run": {
            "phase": "implementation", "task_key": "implement", "iteration": 0,
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
