"""Tests for dashboard/handlers/board.py — the REST layer over board.engine."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.board import engine as board_engine
from kiro_crew.board.stories import StorySummary
from kiro_crew.dashboard.handlers import board as board_handlers


def _request(method: str, path: str, *, match_info: dict | None = None):
    app = {"state": SimpleNamespace(task_runner=MagicMock())}
    return make_mocked_request(method, path, app=app, match_info=match_info or {})


async def _body(resp) -> dict:
    return json.loads(resp.body.decode())


class TestGetPhases:
    @pytest.mark.asyncio
    async def test_returns_all_nine_phases_in_order(self):
        resp = await board_handlers.get_phases(_request("GET", "/api/board/phases"))
        data = await _body(resp)
        assert [p["key"] for p in data["phases"]] == [
            "grooming", "planning", "plan_review", "approval", "implementation",
            "verification", "pr", "review", "done",
        ]
        verification = next(p for p in data["phases"] if p["key"] == "verification")
        assert verification["tasks"] == ["verify", "fix", "confirm"]
        assert verification["gate"] is True


class TestListStories:
    @pytest.mark.asyncio
    async def test_no_configured_projects_returns_empty(self):
        with patch.object(board_handlers, "_project_paths", AsyncMock(return_value=[])):
            resp = await board_handlers.list_board_stories(_request("GET", "/api/board/stories"))
        data = await _body(resp)
        assert data == {"stories": [], "project_paths": []}

    @pytest.mark.asyncio
    async def test_lists_stories_across_configured_projects(self):
        summary = StorySummary(
            id="s-1", title="Story", status="open", epic_id="e-1", epic_title="Epic",
            project_path="/proj", phase="planning", phase_label="Planning", current_run=None,
        )
        with patch.object(board_handlers, "_project_paths", AsyncMock(return_value=["/proj"])), \
             patch.object(board_handlers, "list_stories", AsyncMock(return_value=[summary])):
            resp = await board_handlers.list_board_stories(_request("GET", "/api/board/stories"))
        data = await _body(resp)
        assert data["project_paths"] == ["/proj"]
        assert data["stories"][0]["id"] == "s-1"
        assert data["stories"][0]["phase_label"] == "Planning"


class TestRunStoryJob:
    @pytest.mark.asyncio
    async def test_no_task_runner_is_400(self):
        req = _request("POST", "/api/board/stories/s-1/run", match_info={"id": "s-1"})
        req.app["state"].task_runner = None
        resp = await board_handlers.run_story_job(req)
        assert resp.status == 400
        data = await _body(resp)
        assert data["code"] == "task_runner_unavailable"

    @pytest.mark.asyncio
    async def test_unknown_story_is_404(self):
        req = _request("POST", "/api/board/stories/s-1/run", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=None)):
            resp = await board_handlers.run_story_job(req)
        assert resp.status == 404
        data = await _body(resp)
        assert data["code"] == "story_not_found"

    @pytest.mark.asyncio
    async def test_runs_job_and_returns_task_id(self):
        req = _request("POST", "/api/board/stories/s-1/run", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_engine, "run_job", AsyncMock(return_value="task-123")):
            resp = await board_handlers.run_story_job(req)
        data = await _body(resp)
        assert resp.status == 200
        assert data == {"ok": True, "task_id": "task-123"}

    @pytest.mark.asyncio
    async def test_board_error_is_400(self):
        req = _request("POST", "/api/board/stories/s-1/run", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_engine, "run_job", AsyncMock(side_effect=board_engine.BoardError("nope"))):
            resp = await board_handlers.run_story_job(req)
        assert resp.status == 400
        data = await _body(resp)
        assert data["code"] == "board_phase_error"


class TestAdvanceStory:
    @pytest.mark.asyncio
    async def test_advances_via_complete_review(self):
        req = _request("POST", "/api/board/stories/s-1/advance", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_engine, "complete_review", AsyncMock(return_value="task-999")):
            resp = await board_handlers.advance_story(req)
        data = await _body(resp)
        assert data == {"ok": True, "task_id": "task-999"}


class TestGetStoryHistory:
    @pytest.mark.asyncio
    async def test_returns_history_for_known_story(self, tmp_path):
        from kiro_crew.board import state as board_state
        board_state.save_history(str(tmp_path), "s-1", {"current_run": None, "history": []})
        req = _request("GET", "/api/board/stories/s-1/history", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=str(tmp_path))):
            resp = await board_handlers.get_story_history(req)
        data = await _body(resp)
        assert data == {"current_run": None, "history": []}

    @pytest.mark.asyncio
    async def test_unknown_story_is_404(self):
        req = _request("GET", "/api/board/stories/s-1/history", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=None)):
            resp = await board_handlers.get_story_history(req)
        assert resp.status == 404


class TestGetStoryDetailRoute:
    @pytest.mark.asyncio
    async def test_returns_detail_for_known_story(self):
        req = _request("GET", "/api/board/stories/s-1/detail", match_info={"id": "s-1"})
        detail = {"id": "s-1", "title": "T", "description": "d", "comments": []}
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers.board_detail, "get_story_detail", AsyncMock(return_value=detail)):
            resp = await board_handlers.get_story_detail_route(req)
        data = await _body(resp)
        assert data == detail

    @pytest.mark.asyncio
    async def test_unknown_story_is_404(self):
        req = _request("GET", "/api/board/stories/s-1/detail", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=None)):
            resp = await board_handlers.get_story_detail_route(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_bd_failure_is_502(self):
        req = _request("GET", "/api/board/stories/s-1/detail", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers.board_detail, "get_story_detail", AsyncMock(return_value=None)):
            resp = await board_handlers.get_story_detail_route(req)
        assert resp.status == 502


class TestAddStoryComment:
    @pytest.mark.asyncio
    async def test_posts_comment(self):
        req = _request("POST", "/api/board/stories/s-1/comments", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({"text": "APPROVED"}, None))), \
             patch.object(board_handlers.board_detail, "add_comment", AsyncMock(return_value=True)) as mocked_add:
            resp = await board_handlers.add_story_comment(req)
        data = await _body(resp)
        assert data == {"ok": True}
        mocked_add.assert_called_once_with("/proj", "s-1", "APPROVED")

    @pytest.mark.asyncio
    async def test_empty_text_is_400(self):
        req = _request("POST", "/api/board/stories/s-1/comments", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({"text": "   "}, None))):
            resp = await board_handlers.add_story_comment(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_story_is_404(self):
        req = _request("POST", "/api/board/stories/s-1/comments", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=None)):
            resp = await board_handlers.add_story_comment(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_bd_failure_is_502(self):
        req = _request("POST", "/api/board/stories/s-1/comments", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({"text": "hi"}, None))), \
             patch.object(board_handlers.board_detail, "add_comment", AsyncMock(return_value=False)):
            resp = await board_handlers.add_story_comment(req)
        assert resp.status == 502


class TestUpdateStoryRoute:
    @pytest.mark.asyncio
    async def test_updates_title_and_description(self):
        req = _request("POST", "/api/board/stories/s-1/update", match_info={"id": "s-1"})
        body = {"title": "New title", "description": "New body"}
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=(body, None))), \
             patch.object(board_handlers.board_detail, "update_story", AsyncMock(return_value=True)) as mocked:
            resp = await board_handlers.update_story_route(req)
        data = await _body(resp)
        assert data == {"ok": True}
        mocked.assert_called_once_with("/proj", "s-1", title="New title", description="New body")

    @pytest.mark.asyncio
    async def test_blank_title_is_400(self):
        req = _request("POST", "/api/board/stories/s-1/update", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({"title": "   "}, None))):
            resp = await board_handlers.update_story_route(req)
        assert resp.status == 400
        data = await _body(resp)
        assert data["code"] == "empty_title"

    @pytest.mark.asyncio
    async def test_no_fields_is_400(self):
        req = _request("POST", "/api/board/stories/s-1/update", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({}, None))):
            resp = await board_handlers.update_story_route(req)
        assert resp.status == 400
        data = await _body(resp)
        assert data["code"] == "empty_update"

    @pytest.mark.asyncio
    async def test_unknown_story_is_404(self):
        req = _request("POST", "/api/board/stories/s-1/update", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value=None)):
            resp = await board_handlers.update_story_route(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_bd_failure_is_502(self):
        req = _request("POST", "/api/board/stories/s-1/update", match_info={"id": "s-1"})
        with patch.object(board_handlers, "_find_story_project", AsyncMock(return_value="/proj")), \
             patch.object(board_handlers, "read_bounded_json", AsyncMock(return_value=({"title": "x"}, None))), \
             patch.object(board_handlers.board_detail, "update_story", AsyncMock(return_value=False)):
            resp = await board_handlers.update_story_route(req)
        assert resp.status == 502
