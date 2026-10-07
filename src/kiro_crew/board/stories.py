"""Lists board stories: every task-type beads issue with a parent epic,
across the configured beads project paths - the same
``knowledge.beads_project_paths`` config the Knowledge Library's beads sync
already reads, reused rather than inventing a second "which projects" list.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.bd_cli import run_bd
from kiro_crew.board import state as board_state
from kiro_crew.board.phases import PHASE_BY_KEY


@dataclass
class StorySummary:
    id: str
    title: str
    status: str
    epic_id: str
    epic_title: str
    project_path: str
    phase: str | None  # None = backlog
    phase_label: str
    current_run: dict | None
    priority: int | None
    owner: str


async def list_stories(project_path: str) -> list[StorySummary]:
    rows = await run_bd(project_path, "list", "--all", "--limit", "0")
    if not isinstance(rows, list):
        return []

    titles_by_id = {
        row.get("id"): row.get("title", "")
        for row in rows if isinstance(row, dict) and row.get("id")
    }

    summaries: list[StorySummary] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("issue_type") != "task" or not row.get("parent"):
            continue
        story_id = row["id"]
        labels = row.get("labels") or []
        phase_key = None
        for label in labels:
            if isinstance(label, str) and label.startswith("phase:"):
                phase_key = label[len("phase:"):]
                break
        phase = PHASE_BY_KEY.get(phase_key) if phase_key else None
        history = board_state.load_history(project_path, story_id)
        summaries.append(StorySummary(
            id=story_id,
            title=row.get("title", ""),
            status=row.get("status", ""),
            epic_id=row["parent"],
            epic_title=titles_by_id.get(row["parent"], row["parent"]),
            project_path=project_path,
            phase=phase_key,
            phase_label=phase.label if phase else "Backlog",
            current_run=history.get("current_run"),
            priority=row.get("priority"),
            owner=row.get("owner", ""),
        ))
    return summaries
