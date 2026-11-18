"""Thin dispatch to this project's configured tracker client (beads or Jira)
for the timeline's story list - see kiro_crew.board.tracker for the actual
implementations. Keeps every existing caller (which only ever has a
`project_path` string) unchanged.

Originally: every task-type beads issue with a parent epic, across the
configured beads project paths - this app's own ``project_paths`` config
(``apps/builtins/board/backend/routes.py``'s ``_project_paths``, read
through the generic per-app config every installed app gets for free), not
a second subsystem's config: a plugin owns its own settings. That listing
logic now lives in ``kiro_crew.board.tracker.beads_client``.
"""

from __future__ import annotations

from kiro_crew.board import tracker
from kiro_crew.board.tracker.config import config_for_path
from kiro_crew.board.tracker.types import StorySummary  # noqa: F401 - re-exported for backward compat


async def list_stories(project_path: str) -> list[StorySummary]:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).list_stories(config)
