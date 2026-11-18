"""Thin dispatch to this project's configured tracker client (beads or Jira) -
see kiro_crew.board.tracker for the actual implementations. Keeps every
existing caller (which only ever has a `project_path` string) unchanged.

Originally the full beads issue detail (description, comments, metadata) for
a board story's detail modal - the Jira-style view the board's "Run next
job" card expands into: everything `bd show <id>` carries, plus the ability
to post a new comment through the same channel the rocket-* skills already
use for gates/decisions (`GATE: PASS`, `APPROVED`, etc.), so a human can
answer OPEN QUESTIONS or approve a plan from the board instead of a
terminal. That logic now lives in `kiro_crew.board.tracker.beads_client`.
"""

from __future__ import annotations

from kiro_crew.board import tracker
from kiro_crew.board.tracker.config import config_for_path


async def get_story_detail(project_path: str, story_id: str) -> dict | None:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).get_story_detail(config, story_id)


async def add_comment(project_path: str, story_id: str, text: str) -> bool:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).add_comment(config, story_id, text)


async def update_story(
    project_path: str, story_id: str, *,
    title: str | None = None, description: str | None = None,
    start_date: str | None = None, due_date: str | None = None, rank: float | None = None,
    priority: int | None = None, story_points: float | None = None,
) -> bool:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).update_story(
        config, story_id, title=title, description=description,
        start_date=start_date, due_date=due_date, rank=rank,
        priority=priority, story_points=story_points,
    )


async def add_dependency(project_path: str, story_id: str, depends_on_id: str) -> bool:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).add_dependency(config, story_id, depends_on_id)


async def remove_dependency(project_path: str, story_id: str, depends_on_id: str) -> bool:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).remove_dependency(config, story_id, depends_on_id)


async def has_story(project_path: str, story_id: str) -> bool:
    """New - replaces the inline `run_bd(project_path, "show", "--id", ...)`
    that used to live directly in routes.py's _find_story_project."""
    config = await config_for_path(project_path)
    return await tracker.client_for(config).has_story(config, story_id)
