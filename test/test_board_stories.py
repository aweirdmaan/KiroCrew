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
async def test_bd_failure_returns_empty_list(tmp_path):
    with patch.object(board_stories, "run_bd", AsyncMock(return_value=None)):
        summaries = await board_stories.list_stories(str(tmp_path))
    assert summaries == []
