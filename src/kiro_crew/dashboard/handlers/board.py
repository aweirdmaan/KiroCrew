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

from kiro_crew.bd_cli import run_bd
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
    """Which configured project path this story id belongs to - one cheap
    `bd show --id <id>` per configured project (run concurrently), checking
    which project's beads database actually has this id.

    Deliberately NOT `list_stories(project_path)` per project: that renders
    the WHOLE story list, including a `bd comments` subprocess call per
    pipeline/review-phase story (see its own module comment on why those are
    real, not-cheap calls) - and every handler below calls this on every
    single request (open a story, run a job, advance, comment, update...).
    Re-running that full scan just to resolve one id made every click on
    the board pay for a second full board load; this only asks each
    project's `bd` "do you have this id" once.
    """
    async def _has_story(project_path: str) -> str | None:
        result = await run_bd(project_path, "show", "--id", story_id)
        return project_path if isinstance(result, list) and result else None

    for found in await asyncio.gather(*(_has_story(p) for p in await _project_paths())):
        if found is not None:
            return found
    return None


async def get_phases(request: web.Request) -> web.Response:
    """GET /api/board/phases — the lane definitions (Backlog is implicit,
    not one of these - see stories.py), for the frontend to render the
    board structure without hard-coding it twice. `gate`/`loop` are
    per-TASK now (phases.py), not per-phase - nothing in the frontend reads
    them at the phase level, so they're not echoed here."""
    return web.json_response({
        "phases": [
            {
                "key": p.key, "label": p.label,
                "tasks": [t.key for t in p.tasks],
                "manual": p.manual,
            }
            for p in PHASES
        ]
    })


async def list_board_stories(request: web.Request) -> web.Response:
    """GET /api/board/stories — every story across every configured project."""
    project_paths = await _project_paths()
    if not project_paths:
        return web.json_response({"stories": [], "project_paths": []})
    # Each project's own list_stories() already parallelizes its per-story
    # `bd comments` calls internally - this gathers across PROJECTS too, so
    # project 2's board doesn't wait for project 1's to finish first.
    per_project = await asyncio.gather(*(list_stories(p) for p in project_paths))
    all_stories = []
    for stories in per_project:
        for story in stories:
            all_stories.append({
                "id": story.id, "title": story.title, "status": story.status,
                "epic_id": story.epic_id, "epic_title": story.epic_title,
                "project_path": story.project_path,
                "phase": story.phase, "phase_label": story.phase_label,
                "current_run": story.current_run,
                "priority": story.priority, "owner": story.owner,
                "pending_open_questions": story.pending_open_questions,
                "recommendation": story.recommendation,
                "start_date": story.start_date, "due_date": story.due_date,
                "rank": story.rank, "depends_on": story.depends_on,
                "story_points": story.story_points,
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


_ADVANCE_OUTCOMES = {"approved", "changes_requested"}


async def advance_story(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/advance — the Review phase's job. Body:
    {"mr_url": "...", "outcome"?: "approved" | "changes_requested"} - runs
    rocket-harvest against that MR/PR URL (a human supplies it after reading
    its review comments, matching rocket-dag.sh's separate `harvest`
    command). "approved" (default) advances to Done on success, same as
    before. "changes_requested" also runs harvest (it records every comment
    and, per its SKILL.md step 5, posts a RECOMMENDATION comment) but keeps
    the story in Review - see resume_implementation/resume_planning below
    for the two buttons a human picks from next."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response(
            {"error": "task runner not available", "code": "task_runner_unavailable"}, status=400
        )
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=64_000)
    if body_err is not None:
        return body_err
    assert body is not None
    mr_url = body.get("mr_url", "")
    if not isinstance(mr_url, str) or not mr_url.strip():
        return web.json_response({"error": "mr_url required", "code": "empty_mr_url"}, status=400)
    outcome = body.get("outcome", "approved")
    if outcome not in _ADVANCE_OUTCOMES:
        return web.json_response({"error": "invalid outcome", "code": "bad_outcome"}, status=400)
    try:
        task_id = await board_engine.complete_review(
            state.task_runner, project_path, story_id, mr_url.strip(), outcome=outcome,
        )
    except board_engine.BoardError as exc:
        return web.json_response({"error": str(exc), "code": "board_phase_error"}, status=400)
    return web.json_response({"ok": True, "task_id": task_id})


async def resume_implementation(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/resume-implementation — "Loop to
    Implementation": a human read harvest's RECOMMENDATION and judged the
    requested change small enough to go straight back into the pipeline's
    `implement` task, skipping ideate/plan/confirm_plan/approval_check."""
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
        task_id = await board_engine.resume_pipeline_at(state.task_runner, project_path, story_id, "implement")
    except board_engine.BoardError as exc:
        return web.json_response({"error": str(exc), "code": "board_phase_error"}, status=400)
    return web.json_response({"ok": True, "task_id": task_id})


async def resume_planning(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/resume-planning — "Loop to Planning": a
    human read harvest's RECOMMENDATION and judged the requested change a
    re-scope, re-entering the pipeline from its first task (ideate/plan),
    the same re-scope path rocket-confirm-plan's own gate already uses."""
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
        task_id = await board_engine.resume_pipeline_from_start(state.task_runner, project_path, story_id)
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
    status, priority, owner, comments. The Jira-style modal's main content.

    Deliberately not `_find_story_project` + `get_story_detail`: both call
    `bd show --id` under the hood, so doing them in sequence paid for the
    same subprocess call twice on the single hottest path in the UI (opening
    a story). `get_story_detail`'s own `bd show` already tells us whether
    the id exists in that project, so this tries each configured project
    directly and keeps the first real result - one `bd show` per candidate
    project, not two."""
    story_id = request.match_info["id"]
    for project_path in await _project_paths():
        detail = await board_detail.get_story_detail(project_path, story_id)
        if detail is not None:
            return web.json_response(detail)
    return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)


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
    """POST /api/board/stories/{id}/update — edit the card itself. Body:
    {"title"?, "description"?, "start_date"?, "due_date"?, "rank"?,
    "priority"?, "story_points"?}, at least one required. Unlike comments
    (append-only in beads, see add_story_comment's sibling "edit" in the
    frontend), issue fields are plain mutable columns via `bd update`, so
    this is a real in-place edit - including the timeline/sidebar's own
    scheduling and planning fields (see stories.py's module comment for
    where those actually live in beads)."""
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
    start_date = body.get("start_date")
    due_date = body.get("due_date")
    rank = body.get("rank")
    priority = body.get("priority")
    story_points = body.get("story_points")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        return web.json_response({"error": "title cannot be blank", "code": "empty_title"}, status=400)
    if description is not None and not isinstance(description, str):
        return web.json_response({"error": "description must be a string", "code": "bad_description"}, status=400)
    if start_date is not None and not isinstance(start_date, str):
        return web.json_response({"error": "start_date must be a string", "code": "bad_start_date"}, status=400)
    if due_date is not None and not isinstance(due_date, str):
        return web.json_response({"error": "due_date must be a string", "code": "bad_due_date"}, status=400)
    if rank is not None and not isinstance(rank, (int, float)):
        return web.json_response({"error": "rank must be a number", "code": "bad_rank"}, status=400)
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 4):
        return web.json_response({"error": "priority must be an integer 0-4", "code": "bad_priority"}, status=400)
    if story_points is not None and not isinstance(story_points, (int, float)):
        return web.json_response({"error": "story_points must be a number", "code": "bad_story_points"}, status=400)
    if all(v is None for v in (title, description, start_date, due_date, rank, priority, story_points)):
        return web.json_response({"error": "nothing to update", "code": "empty_update"}, status=400)
    ok = await board_detail.update_story(
        project_path, story_id, title=title, description=description,
        start_date=start_date, due_date=due_date, rank=rank,
        priority=priority, story_points=story_points,
    )
    if not ok:
        return web.json_response({"error": "bd update failed", "code": "bd_unavailable"}, status=502)
    return web.json_response({"ok": True})


async def add_dependency_route(request: web.Request) -> web.Response:
    """POST /api/board/stories/{id}/dependencies — body: {"depends_on_id":
    "..."}. Draws the timeline's dependency arrow from this story to
    another; `story_id` depends on (is blocked by) `depends_on_id`."""
    story_id = request.match_info["id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=64_000)
    if body_err is not None:
        return body_err
    assert body is not None
    depends_on_id = body.get("depends_on_id", "")
    if not isinstance(depends_on_id, str) or not depends_on_id.strip():
        return web.json_response({"error": "depends_on_id required", "code": "empty_depends_on_id"}, status=400)
    ok = await board_detail.add_dependency(project_path, story_id, depends_on_id)
    if not ok:
        return web.json_response({"error": "bd dep add failed", "code": "bd_unavailable"}, status=502)
    return web.json_response({"ok": True})


async def remove_dependency_route(request: web.Request) -> web.Response:
    """DELETE /api/board/stories/{id}/dependencies/{depends_on_id}"""
    story_id = request.match_info["id"]
    depends_on_id = request.match_info["depends_on_id"]
    project_path = await _find_story_project(story_id)
    if project_path is None:
        return web.json_response({"error": "story not found", "code": "story_not_found"}, status=404)
    ok = await board_detail.remove_dependency(project_path, story_id, depends_on_id)
    if not ok:
        return web.json_response({"error": "bd dep remove failed", "code": "bd_unavailable"}, status=502)
    return web.json_response({"ok": True})


def setup_board_routes(app: web.Application) -> None:
    app.router.add_get("/api/board/phases", get_phases)
    app.router.add_get("/api/board/stories", list_board_stories)
    app.router.add_post("/api/board/stories/{id}/run", run_story_job)
    app.router.add_post("/api/board/stories/{id}/advance", advance_story)
    app.router.add_post("/api/board/stories/{id}/resume-implementation", resume_implementation)
    app.router.add_post("/api/board/stories/{id}/resume-planning", resume_planning)
    app.router.add_get("/api/board/stories/{id}/history", get_story_history)
    app.router.add_get("/api/board/stories/{id}/detail", get_story_detail_route)
    app.router.add_post("/api/board/stories/{id}/comments", add_story_comment)
    app.router.add_post("/api/board/stories/{id}/update", update_story_route)
    app.router.add_post("/api/board/stories/{id}/dependencies", add_dependency_route)
    app.router.add_delete("/api/board/stories/{id}/dependencies/{depends_on_id}", remove_dependency_route)
