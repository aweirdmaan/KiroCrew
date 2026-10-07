"""Lists timeline stories: every task-type beads issue with a parent epic,
across the configured beads project paths - the same
``knowledge.beads_project_paths`` config the Knowledge Library's beads sync
already reads, reused rather than inventing a second "which projects" list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.bd_cli import run_bd
from kiro_crew.board import engine as board_engine
from kiro_crew.board.open_questions import last_comment_has_pending_open_questions
from kiro_crew.board.phases import PHASE_BY_KEY

# The only phase where rocket-plan's own OPEN QUESTIONS post can still be
# sitting unanswered: the pipeline's own `plan` task has no gate (see
# phases.py's module docstring), so a story can sit in this phase with
# questions still unanswered until confirm_plan (also in this phase) runs
# and actually checks them.
_PHASES_WHERE_QUESTIONS_MAY_BE_PENDING = {"pipeline"}

# Scheduling fields live as beads' own native extension points rather than a
# bespoke sidecar store: `due_at` is a first-class issue field (`bd update
# --due`), and `start_date`/`timeline_rank` ride on beads' free-form
# `metadata` map (`bd update --set-metadata key=value`) since beads has no
# native "start date" or manual-ordering concept. `timeline_rank` is a float,
# not an index, so a card dropped between two others gets a value between
# their ranks instead of renumbering the whole list (the standard
# fractional-ranking technique for a user-reorderable list).
_START_DATE_METADATA_KEY = "start_date"
_RANK_METADATA_KEY = "timeline_rank"


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
    pending_open_questions: bool
    start_date: str | None
    due_date: str | None
    rank: float | None
    depends_on: list[str] = field(default_factory=list)


def _coerce_rank(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        # Self-heals a "running" entry orphaned by e.g. a gateway restart
        # mid-run - see reconcile_stale_running's own docstring. Every
        # timeline list read clears one, not just a "Run next job" click,
        # so the UI never shows a run as perpetually in progress when
        # nothing is actually watching it.
        history = board_engine.reconcile_stale_running(project_path, story_id)
        pending_open_questions = False
        if phase_key in _PHASES_WHERE_QUESTIONS_MAY_BE_PENDING:
            comments = await run_bd(project_path, "comments", story_id)
            if isinstance(comments, list):
                pending_open_questions = last_comment_has_pending_open_questions(comments)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        depends_on = [
            dep["depends_on_id"] for dep in (row.get("dependencies") or [])
            if isinstance(dep, dict) and dep.get("type") != "parent-child" and dep.get("depends_on_id")
        ]
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
            pending_open_questions=pending_open_questions,
            start_date=metadata.get(_START_DATE_METADATA_KEY),
            due_date=row.get("due_at"),
            rank=_coerce_rank(metadata.get(_RANK_METADATA_KEY)),
            depends_on=depends_on,
        ))
    return summaries
