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
