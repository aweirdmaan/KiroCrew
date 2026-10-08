"""Tests for kiro_crew.board.detail: full beads issue detail + add_comment."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.board import detail as board_detail


def _issue(**overrides) -> list[dict]:
    issue = {
        "id": "s-1", "title": "Story one", "description": "the why/what",
        "status": "open", "issue_type": "task", "priority": 2,
        "owner": "me", "created_at": "t1", "updated_at": "t2",
        "comments": [{"id": "c1", "author": "a", "text": "hello", "created_at": "t3"}],
    }
    issue.update(overrides)
    return [issue]


class TestGetStoryDetail:
    @pytest.mark.asyncio
    async def test_returns_full_detail(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=_issue())):
            detail = await board_detail.get_story_detail("/proj", "s-1")
        assert detail["id"] == "s-1"
        assert detail["description"] == "the why/what"
        assert detail["comments"] == [
            {"id": "c1", "author": "a", "text": "hello", "created_at": "t3"}
        ]

    @pytest.mark.asyncio
    async def test_redacts_credentials_in_description_and_comments(self):
        secret = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=_issue(
            description=secret,
            comments=[{"id": "c1", "author": "a", "text": secret, "created_at": "t"}],
        ))):
            detail = await board_detail.get_story_detail("/proj", "s-1")
        assert "ghp_" not in detail["description"]
        assert "ghp_" not in detail["comments"][0]["text"]

    @pytest.mark.asyncio
    async def test_bd_failure_returns_none(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=None)):
            detail = await board_detail.get_story_detail("/proj", "s-1")
        assert detail is None

    @pytest.mark.asyncio
    async def test_exposes_timeline_scheduling_fields(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=_issue(
            due_at="2026-10-15T00:00:00Z",
            metadata={"start_date": "2026-10-01", "timeline_rank": 3, "story_points": 5},
            dependencies=[
                {"issue_id": "s-1", "depends_on_id": "epic-1", "type": "parent-child"},
                {"issue_id": "s-1", "depends_on_id": "s-0", "type": "blocks"},
            ],
        ))):
            detail = await board_detail.get_story_detail("/proj", "s-1")
        assert detail["start_date"] == "2026-10-01"
        assert detail["due_date"] == "2026-10-15T00:00:00Z"
        assert detail["rank"] == 3.0
        assert detail["story_points"] == 5.0
        assert detail["depends_on"] == ["s-0"]

    @pytest.mark.asyncio
    async def test_missing_scheduling_fields_default_to_none(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=_issue())):
            detail = await board_detail.get_story_detail("/proj", "s-1")
        assert detail["start_date"] is None
        assert detail["due_date"] is None
        assert detail["rank"] is None
        assert detail["story_points"] is None
        assert detail["depends_on"] == []


class TestAddComment:
    @pytest.mark.asyncio
    async def test_posts_comment_via_bd(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            ok = await board_detail.add_comment("/proj", "s-1", "APPROVED")
        assert ok is True
        assert calls == [("comments", "add", "s-1", "APPROVED")]

    @pytest.mark.asyncio
    async def test_empty_text_is_not_posted(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=[{"status": "ok"}])) as mocked:
            ok = await board_detail.add_comment("/proj", "s-1", "   ")
        assert ok is False
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_bd_failure_returns_false(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=None)):
            ok = await board_detail.add_comment("/proj", "s-1", "hello")
        assert ok is False


class TestUpdateStory:
    @pytest.mark.asyncio
    async def test_updates_title_and_description_via_bd(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            ok = await board_detail.update_story("/proj", "s-1", title="New title", description="New body")
        assert ok is True
        assert calls == [("update", "s-1", "--title", "New title", "--description", "New body")]

    @pytest.mark.asyncio
    async def test_updates_only_the_field_given(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            await board_detail.update_story("/proj", "s-1", description="Only this changed")
        assert calls == [("update", "s-1", "--description", "Only this changed")]

    @pytest.mark.asyncio
    async def test_no_fields_given_is_not_posted(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=[{"status": "ok"}])) as mocked:
            ok = await board_detail.update_story("/proj", "s-1")
        assert ok is False
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_bd_failure_returns_false(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=None)):
            ok = await board_detail.update_story("/proj", "s-1", title="x")
        assert ok is False

    @pytest.mark.asyncio
    async def test_updates_timeline_scheduling_fields(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            await board_detail.update_story(
                "/proj", "s-1", start_date="2026-10-01", due_date="2026-10-15", rank=2.5,
            )
        assert calls == [(
            "update", "s-1", "--due", "2026-10-15",
            "--set-metadata", "start_date=2026-10-01",
            "--set-metadata", "timeline_rank=2.5",
        )]

    @pytest.mark.asyncio
    async def test_updates_priority_and_story_points(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            await board_detail.update_story("/proj", "s-1", priority=1, story_points=5)
        assert calls == [(
            "update", "s-1", "--priority", "1",
            "--set-metadata", "story_points=5",
        )]


class TestDependencies:
    @pytest.mark.asyncio
    async def test_add_dependency(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            ok = await board_detail.add_dependency("/proj", "s-1", "s-0")
        assert ok is True
        assert calls == [("dep", "add", "s-1", "s-0")]

    @pytest.mark.asyncio
    async def test_add_dependency_blank_id_is_not_posted(self):
        with patch.object(board_detail, "run_bd", AsyncMock(return_value=[{"status": "ok"}])) as mocked:
            ok = await board_detail.add_dependency("/proj", "s-1", "   ")
        assert ok is False
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_dependency(self):
        calls = []

        async def fake_run_bd(project_path, *args):
            calls.append(args)
            return [{"status": "ok"}]

        with patch.object(board_detail, "run_bd", side_effect=fake_run_bd):
            ok = await board_detail.remove_dependency("/proj", "s-1", "s-0")
        assert ok is True
        assert calls == [("dep", "remove", "s-1", "s-0")]
