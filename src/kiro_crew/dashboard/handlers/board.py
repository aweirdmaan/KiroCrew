"""Story Board API handlers — list stories, run a job, read history, advance.

See ``kiro_crew.board`` for the pipeline/engine this wraps. Operates over
every project path in ``knowledge.beads_project_paths`` (the same config the
Knowledge Library's beads sync already reads — one "which projects have a
board" list, not two).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.board import detail as board_detail
from kiro_crew.board import engine as board_engine
from kiro_crew.board import state as board_state
from kiro_crew.board.phases import PHASES
from kiro_crew.board.stories import list_stories
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


async def _project_paths() -> list[str]:
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return list(cfg.knowledge.beads_project_paths)


async def _find_story_project(story_id: str) -> str | None:
    """Which configured project path this story id belongs to. Scans every
    configured project's listing - fine at the scale this config is meant
    for (a handful of projects), and avoids a second "story -> project"
    index to keep in sync."""
    for project_path in await _project_paths():
        for story in await list_stories(project_path):
            if story.id == story_id:
                return project_path
    return None


async def get_phases(request: web.Request) -> web.Response:
    """GET /api/board/phases — the 9 column definitions, for the frontend to
    render the board structure without hard-coding it twice."""
    return web.json_response({
        "phases": [
            {
                "key": p.key, "label": p.label,
                "tasks": [t.key for t in p.tasks],
                "gate": p.gate, "manual": p.manual,
            }
            for p in PHASES
        ]
    })


async def list_board_stories(request: web.Request) -> web.Response:
    """GET /api/board/stories — every story across every configured project."""
    project_paths = await _project_paths()
    if not project_paths:
        return web.json_response({"stories": [], "project_paths": []})
    all_stories = []
    for project_path in project_paths:
        for story in await list_stories(project_path):
            all_stories.append({
                "id": story.id, "title": story.title, "status": story.status,
                "epic_id": story.epic_id, "epic_title": story.epic_title,
                "project_path": story.project_path,
                "phase": story.phase, "phase_label": story.phase_label,
                "current_run": story.current_run,
            })
    return web.json_response({"stories": all_stories, "project_paths": project_paths})


async def run_story_job(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/run — submit the story's next job."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response(
            {"error": "task runner not available", "code": "task_runner_unavailable"}, status=400
        )
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    try:
        task_id = await board_engine.run_job(state.task_runner, project_path, story_id)
    except board_engine.BoardError as exc:
        return web.json_response({"error": str(exc), "code": "board_phase_error"}, status=400)
    return web.json_response({"ok": True, "task_id": task_id})


async def advance_story(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/advance — the one manual transition,
    Review -> Done (runs rocket-retro, then advances on success)."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response(
            {"error": "task runner not available", "code": "task_runner_unavailable"}, status=400
        )
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    try:
        task_id = await board_engine.complete_review(state.task_runner, project_path, story_id)
    except board_engine.BoardError as exc:
        return web.json_response({"error": str(exc), "code": "board_phase_error"}, status=400)
    return web.json_response({"ok": True, "task_id": task_id})


async def get_story_history(request: web.Request) -> web.Response:
    """GET /api/board/stories/{id}/history — timeline data for the drawer."""
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    data = board_state.load_history(project_path, story_id)
    return web.json_response(data)


async def get_story_detail_route(request: web.Request) -> web.Response:
    """GET /api/board/stories/{id}/detail — full beads attributes: description,
    status, priority, owner, comments. The Jira-style modal's main content."""
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    detail = await board_detail.get_story_detail(project_path, story_id)
    if detail is None:
        return web.json_response({"error": "bd show failed", "code": "bd_unavailable"}, status=502)
    return web.json_response(detail)


async def add_story_comment(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/comments — post a beads comment. Body:
    {"text": "..."}. This is the same channel the rocket-* skills read for
    gates (GATE: PASS/FAIL) and human approval (APPROVED) - a human can
    answer OPEN QUESTIONS or approve a plan from here instead of a terminal."""
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=64_000)
    if body_err is not None:
        return body_err
    assert body is not None
    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text required", "code": "empty_comment"}, status=400)
    ok = await board_detail.add_comment(project_path, story_id, text)
    if not ok:
        return web.json_response({"error": "bd comment failed", "code": "bd_unavailable"}, status=502)
    return web.json_response({"ok": True})


async def update_story_route(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/update — edit the card's title/description.
    Body: {"title"?: "...", "description"?: "..."}, at least one required.
    Unlike comments (append-only in beads, see add_story_comment's sibling
    "edit" in the frontend), issue fields are plain mutable columns via
    `bd update`, so this is a real in-place edit."""
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=64_000)
    if body_err is not None:
        return body_err
    assert body is not None
    title = body.get("title")
    description = body.get("description")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        return web.json_response({"error": "title cannot be blank", "code": "empty_title"}, status=400)
    if description is not None and not isinstance(description, str):
        return web.json_response({"error": "description must be a string", "code": "bad_description"}, status=400)
    if title is None and description is None:
        return web.json_response({"error": "nothing to update", "code": "empty_update"}, status=400)
    ok = await board_detail.update_story(project_path, story_id, title=title, description=description)
    if not ok:
        return web.json_response({"error": "bd update failed", "code": "bd_unavailable"}, status=502)
    return web.json_response({"ok": True})


def setup_board_routes(app: web.Application) -> None:
    app.router.add_get("/api/board/phases", get_phases)
    app.router.add_get("/api/board/stories", list_board_stories)
    app.router.add_post("/api/board/stories/{id}/run", run_story_job)
    app.router.add_post("/api/board/stories/{id}/advance", advance_story)
    app.router.add_get("/api/board/stories/{id}/history", get_story_history)
    app.router.add_get("/api/board/stories/{id}/detail", get_story_detail_route)
    app.router.add_post("/api/board/stories/{id}/comments", add_story_comment)
    app.router.add_post("/api/board/stories/{id}/update", update_story_route)
