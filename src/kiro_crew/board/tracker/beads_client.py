"""Beads tracker client - the ``BoardTrackerClient`` surface implemented
against beads (``bd``), ported verbatim from the formerly-beads-hardwired
``board/detail.py``, ``board/state.py`` and ``board/stories.py``.

Text is redacted the same way the Knowledge Library's beads sync redacts
ingested issue content (credentials/exfiltration URLs) before it is returned
- a beads comment can legitimately contain a pasted secret during incident
discussion, and echoing it back verbatim in the dashboard is the same leak
the Knowledge Library citation would be.
"""

from __future__ import annotations

import asyncio
import logging

from kiro_crew.apps.builtins.board.backend.bd_cli import run_bd
from kiro_crew.board.open_questions import last_comment_has_pending_open_questions
from kiro_crew.board.phases import PHASE_BY_KEY
from kiro_crew.board.tracker.types import ProjectConfig, StorySummary
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# board.engine and board.state are imported lazily, inside the functions
# that need them, rather than at module level: both of THEM import this
# package (board.engine for its own board_state calls, by way of
# board.state importing kiro_crew.board.tracker.config for its dispatch),
# so a module-level import here would be a real circular import, not a
# style nicety - see kiro_crew.board.tracker/__init__.py's own import of
# this module for the other half of the cycle.

logger = logging.getLogger(__name__)

_LABEL_PREFIX = "phase:"

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
_STORY_POINTS_METADATA_KEY = "story_points"

# The only phase where rocket-plan's own OPEN QUESTIONS post can still be
# sitting unanswered: the pipeline's own `plan` task has no gate (see
# phases.py's module docstring), so a story can sit in this phase with
# questions still unanswered until confirm_plan (also in this phase) runs
# and actually checks them.
_PHASES_WHERE_QUESTIONS_MAY_BE_PENDING = {"pipeline"}

# The only phase a RECOMMENDATION comment (rocket-harvest's SKILL.md step 5)
# can be sitting unread: harvest only ever runs from here (see
# engine.REVIEW_COMPLETION_TASK), and a "changes_requested" outcome leaves
# the story in Review on purpose for a human to read it and pick a loop-back
# - see engine.resume_pipeline_at / resume_pipeline_from_start.
_PHASES_WHERE_RECOMMENDATION_MAY_BE_PENDING = {"review"}


def _redact(text: object) -> str:
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def has_story(config: ProjectConfig, story_id: str) -> bool:
    result = await run_bd(config.path, "show", "--id", story_id)
    return isinstance(result, list) and bool(result)


async def get_story_detail(config: ProjectConfig, story_id: str) -> dict | None:
    result = await run_bd(config.path, "show", "--id", story_id)
    if not isinstance(result, list) or not result:
        return None
    issue = result[0]
    if not isinstance(issue, dict):
        return None
    comments = []
    for c in issue.get("comments") or []:
        if not isinstance(c, dict):
            continue
        comments.append({
            "id": c.get("id", ""),
            "author": c.get("author", ""),
            "text": _redact(c.get("text", "")),
            "created_at": c.get("created_at", ""),
        })
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    depends_on = [
        dep["depends_on_id"] for dep in (issue.get("dependencies") or [])
        if isinstance(dep, dict) and dep.get("type") != "parent-child" and dep.get("depends_on_id")
    ]
    rank = _coerce_float(metadata.get("timeline_rank"))
    story_points = _coerce_float(metadata.get("story_points"))
    return {
        "id": issue.get("id", story_id),
        "title": issue.get("title", ""),
        "description": _redact(issue.get("description", "")),
        "status": issue.get("status", ""),
        "issue_type": issue.get("issue_type", ""),
        "priority": issue.get("priority"),
        "owner": issue.get("owner", ""),
        "created_at": issue.get("created_at", ""),
        "updated_at": issue.get("updated_at", ""),
        "comments": comments,
        "start_date": metadata.get("start_date"),
        "due_date": issue.get("due_at"),
        "rank": rank,
        "story_points": story_points,
        "depends_on": depends_on,
    }


async def add_comment(config: ProjectConfig, story_id: str, text: str) -> bool:
    if not text.strip():
        return False
    result = await run_bd(config.path, "comments", "add", story_id, text)
    return result is not None


async def update_story(
    config: ProjectConfig, story_id: str, *,
    title: str | None = None, description: str | None = None,
    start_date: str | None = None, due_date: str | None = None, rank: float | None = None,
    priority: int | None = None, story_points: float | None = None,
) -> bool:
    """Edit the card itself via `bd update`. Unlike comments, beads issue
    fields are plain mutable columns - no embedded-mode restriction here
    (see add_comment's docstring/the board's comment "edit" for why that
    one has to work differently).

    start_date/rank/story_points ride on beads' free-form --set-metadata
    (beads has no native "start date", manual-ordering, or story-points
    concept); due_date and priority are native fields. See stories.py's
    module comment for why."""
    args: list[str] = ["update", story_id]
    if title is not None:
        args += ["--title", title]
    if description is not None:
        args += ["--description", description]
    if due_date is not None:
        args += ["--due", due_date]
    if priority is not None:
        args += ["--priority", str(priority)]
    if start_date is not None:
        args += ["--set-metadata", f"start_date={start_date}"]
    if rank is not None:
        args += ["--set-metadata", f"timeline_rank={rank}"]
    if story_points is not None:
        args += ["--set-metadata", f"story_points={story_points}"]
    if len(args) == 2:
        return False
    result = await run_bd(config.path, *args)
    return result is not None


async def add_dependency(config: ProjectConfig, story_id: str, depends_on_id: str) -> bool:
    """story_id depends on (is blocked by) depends_on_id - `bd dep add
    <blocked> <blocker>` per `bd dep --help`."""
    if not depends_on_id.strip():
        return False
    result = await run_bd(config.path, "dep", "add", story_id, depends_on_id.strip())
    return result is not None


async def remove_dependency(config: ProjectConfig, story_id: str, depends_on_id: str) -> bool:
    result = await run_bd(config.path, "dep", "remove", story_id, depends_on_id)
    return result is not None


async def get_phase(config: ProjectConfig, story_id: str) -> str | None:
    """Current phase key, or None (= backlog, no phase label yet)."""
    rows = await run_bd(config.path, "list", "--all", "--limit", "0", "--id", story_id)
    if not isinstance(rows, list) or not rows:
        return None
    labels = rows[0].get("labels") or []
    for label in labels:
        if isinstance(label, str) and label.startswith(_LABEL_PREFIX):
            return label[len(_LABEL_PREFIX):]
    return None


async def set_phase(config: ProjectConfig, story_id: str, new_phase: str) -> bool:
    """Swap the story's `phase:*` label for `phase:<new_phase>`. Removes
    every existing phase:* label first (there should only ever be one, but a
    hand-edited issue could carry a stale extra one - this makes the engine
    self-healing rather than silently picking an arbitrary one later)."""
    rows = await run_bd(config.path, "list", "--all", "--limit", "0", "--id", story_id)
    existing = []
    if isinstance(rows, list) and rows:
        existing = [
            label for label in (rows[0].get("labels") or [])
            if isinstance(label, str) and label.startswith(_LABEL_PREFIX)
        ]
    for label in existing:
        await run_bd(config.path, "label", "remove", story_id, label)
    result = await run_bd(config.path, "label", "add", story_id, f"{_LABEL_PREFIX}{new_phase}")
    return result is not None


async def last_gate_comment(config: ProjectConfig, story_id: str) -> str | None:
    """The last comment line starting with "GATE:" across every comment on
    the issue, in chronological order - the exact semantics of
    `bd comments <id> | grep '^GATE:' | tail -1` that rocket-gate-check.sh
    (and rocket-dag.sh's gate() function) already use."""
    comments = await run_bd(config.path, "comments", story_id)
    if not isinstance(comments, list):
        return None
    last: str | None = None
    for comment in comments:
        text = comment.get("text") if isinstance(comment, dict) else None
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if line.startswith("GATE:"):
                last = line
    return last


async def last_recommendation_comment(config: ProjectConfig, story_id: str) -> str | None:
    """The last comment line starting with "RECOMMENDATION:" - rocket-harvest's
    own convention (step 5 of its SKILL.md) for proposing, after a reviewer
    asked for a story change, whether it is small enough to loop straight
    back into implementation ("QUICK_FIX") or big enough to re-enter
    planning ("RE_PLAN"). Read-only input to a human's choice - the engine
    never acts on it by itself (see board/engine.py's resume_pipeline_at /
    resume_pipeline_from_start, both triggered by an explicit board click,
    never by this function)."""
    from kiro_crew.board.state import last_recommendation_in_comments

    comments = await run_bd(config.path, "comments", story_id)
    if not isinstance(comments, list):
        return None
    return last_recommendation_in_comments(comments)


def _coerce_rank(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def list_stories(config: ProjectConfig) -> list[StorySummary]:
    from kiro_crew.board import engine as board_engine
    from kiro_crew.board.state import last_recommendation_in_comments

    project_path = config.path
    rows = await run_bd(project_path, "list", "--all", "--limit", "0")
    if not isinstance(rows, list):
        return []

    titles_by_id = {
        row.get("id"): row.get("title", "")
        for row in rows if isinstance(row, dict) and row.get("id")
    }
    epic_ids = {
        row["id"] for row in rows
        if isinstance(row, dict) and row.get("issue_type") == "epic" and row.get("id")
    }

    story_rows: list[dict] = []
    phase_keys: dict[str, str | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # A direct child of an EPIC is a story - the timeline's own unit.
        # confirm_plan persists each grape as its own task-type issue
        # parented to the STORY, not the epic (see rocket-confirm-plan's
        # SKILL.md step 3) - those are implementation detail one level
        # down, already visible in the story's own pipeline DAG, not a
        # second timeline row of their own.
        if row.get("issue_type") != "task" or row.get("parent") not in epic_ids:
            continue
        story_id = row["id"]
        labels = row.get("labels") or []
        phase_key = None
        for label in labels:
            if isinstance(label, str) and label.startswith("phase:"):
                phase_key = label[len("phase:"):]
                break
        story_rows.append(row)
        phase_keys[story_id] = phase_key

    # One `bd comments <id>` subprocess call per story that might need one
    # (open questions pending in "pipeline", a RECOMMENDATION pending in
    # "review") - run concurrently rather than one at a time. Each `bd`
    # invocation is real subprocess + sqlite overhead (measured live:
    # 0.2-3.7s, not a cheap call), so awaiting them in a sequential loop
    # multiplied the whole board's load time by however many stories were
    # in those two phases; gathering them lets the slowest one set the
    # total instead of their sum.
    comment_story_ids = [
        row["id"] for row in story_rows
        if phase_keys[row["id"]] in _PHASES_WHERE_QUESTIONS_MAY_BE_PENDING
        or phase_keys[row["id"]] in _PHASES_WHERE_RECOMMENDATION_MAY_BE_PENDING
    ]
    comments_by_id: dict[str, object] = {}
    if comment_story_ids:
        results = await asyncio.gather(*(run_bd(project_path, "comments", sid) for sid in comment_story_ids))
        comments_by_id = dict(zip(comment_story_ids, results))

    summaries: list[StorySummary] = []
    for row in story_rows:
        story_id = row["id"]
        phase_key = phase_keys[story_id]
        phase = PHASE_BY_KEY.get(phase_key) if phase_key else None
        # Self-heals a "running" entry orphaned by e.g. a gateway restart
        # mid-run - see reconcile_stale_running's own docstring. Every
        # timeline list read clears one, not just a "Run next job" click,
        # so the UI never shows a run as perpetually in progress when
        # nothing is actually watching it.
        history = board_engine.reconcile_stale_running(project_path, story_id)
        pending_open_questions = False
        if phase_key in _PHASES_WHERE_QUESTIONS_MAY_BE_PENDING:
            comments = comments_by_id.get(story_id)
            if isinstance(comments, list):
                pending_open_questions = last_comment_has_pending_open_questions(comments)
        recommendation = None
        if phase_key in _PHASES_WHERE_RECOMMENDATION_MAY_BE_PENDING:
            comments = comments_by_id.get(story_id)
            if isinstance(comments, list):
                recommendation = last_recommendation_in_comments(comments)
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
            recommendation=recommendation,
            start_date=metadata.get(_START_DATE_METADATA_KEY),
            due_date=row.get("due_at"),
            rank=_coerce_rank(metadata.get(_RANK_METADATA_KEY)),
            story_points=_coerce_rank(metadata.get(_STORY_POINTS_METADATA_KEY)),
            depends_on=depends_on,
        ))
    return summaries
