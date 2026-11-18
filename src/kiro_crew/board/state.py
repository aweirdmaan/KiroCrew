"""Durable state for a board story: its current phase (tracker-owned, e.g. a
beads `phase:<key>` label) and its job-run history (a small sidecar JSON
file).

The phase getter/setter and the gate/recommendation comment readers below
are thin dispatch wrappers to this project's configured tracker client (see
kiro_crew.board.tracker) - a Jira-backed project reads/writes its phase and
comments through Jira instead, behind the exact same functions.

For beads specifically, beads remains the source of truth for phase, by
design: ``phase:<key>`` is a plain beads label, readable with ``bd list
--label phase:<key>`` by anyone, not only this engine.

The history file below is NOT tracker-specific - it's a cache/index only
(timing and task-runner run ids for the timeline view, which no tracker has
a concept of) and is fully reconstructible (modulo exact timestamps) by
replaying the label/comment history on the issue if it is ever lost or
deleted. It stays here, unchanged, for every project regardless of tracker.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.board import tracker
from kiro_crew.board.tracker.config import config_for_path

logger = logging.getLogger(__name__)

_HISTORY_SUBDIR = Path(".kiro") / "crew" / "board"


def _safe_id(story_id: str) -> str:
    return re.sub(r"[\\/]", "_", story_id)


def history_path(project_path: str, story_id: str) -> Path:
    return Path(project_path) / _HISTORY_SUBDIR / f"{_safe_id(story_id)}.json"


@dataclass
class RunRecord:
    """One completed (or in-progress) job-run entry in a story's history."""

    phase: str
    task_key: str
    iteration: int
    task_id: str | None
    status: str  # "running" | "passed" | "failed" | "gate_failed" | "cancelled" | "missing"
    started_at: str
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase, "task_key": self.task_key, "iteration": self.iteration,
            "task_id": self.task_id, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(
            phase=d.get("phase", ""), task_key=d.get("task_key", ""),
            iteration=d.get("iteration", 0), task_id=d.get("task_id"),
            status=d.get("status", ""), started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at"),
        )


def load_history(project_path: str, story_id: str) -> dict:
    """Returns ``{"current_run": dict|None, "history": [dict, ...], ...}``.

    Any OTHER top-level key a caller previously saved (e.g. autorun.py's
    ``autorun_seen_comment_count``) round-trips through unchanged - this
    file is a general per-story sidecar, not solely current_run/history's
    own private format, so a second feature's own small bit of durable
    state doesn't need a second file.

    Missing or unreadable file -> empty defaults, same "degrade, don't raise"
    contract every other beads-adjacent helper in this codebase follows.
    """
    path = history_path(project_path, story_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"current_run": None, "history": []}
    except (OSError, json.JSONDecodeError):
        logger.warning("board: failed to read history for %s at %s", story_id, path, exc_info=True)
        return {"current_run": None, "history": []}
    if not isinstance(data, dict):
        return {"current_run": None, "history": []}
    return {
        **data,
        "current_run": data.get("current_run"),
        "history": data.get("history") if isinstance(data.get("history"), list) else [],
    }


def save_history(project_path: str, story_id: str, data: dict) -> None:
    """Atomic write (temp file + os.replace), same pattern the rest of this
    codebase uses for small durable JSON state (see knowledge/store.py)."""
    path = history_path(project_path, story_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".board-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


async def get_phase(project_path: str, story_id: str) -> str | None:
    """Current phase key, or None (= backlog, no phase label yet)."""
    config = await config_for_path(project_path)
    return await tracker.client_for(config).get_phase(config, story_id)


async def set_phase(project_path: str, story_id: str, new_phase: str) -> bool:
    config = await config_for_path(project_path)
    return await tracker.client_for(config).set_phase(config, story_id, new_phase)


async def last_gate_comment(project_path: str, story_id: str) -> str | None:
    """The last comment line starting with "GATE:" across every comment on
    the issue, in chronological order - the exact semantics of
    `bd comments <id> | grep '^GATE:' | tail -1` that rocket-gate-check.sh
    (and rocket-dag.sh's gate() function) already use."""
    config = await config_for_path(project_path)
    return await tracker.client_for(config).last_gate_comment(config, story_id)


def gate_passed(gate_line: str | None) -> bool:
    return bool(gate_line) and gate_line.startswith("GATE: PASS")


_RECOMMENDATION_PREFIX = "RECOMMENDATION:"


def last_recommendation_in_comments(comments: list) -> str | None:
    """The pure parsing half of last_recommendation_comment, split out so a
    caller that already fetched a story's comments (e.g.
    tracker.beads_client's list_stories, batching every story's `bd
    comments` call together rather than issuing a second one per story
    here) can reuse it without another subprocess round-trip."""
    last: str | None = None
    for comment in comments:
        text = comment.get("text") if isinstance(comment, dict) else None
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if line.startswith(_RECOMMENDATION_PREFIX):
                last = line
    return last


async def last_recommendation_comment(project_path: str, story_id: str) -> str | None:
    """The last comment line starting with "RECOMMENDATION:" - rocket-harvest's
    own convention (step 5 of its SKILL.md) for proposing, after a reviewer
    asked for a story change, whether it is small enough to loop straight
    back into implementation ("QUICK_FIX") or big enough to re-enter
    planning ("RE_PLAN"). Read-only input to a human's choice - the engine
    never acts on it by itself (see board/engine.py's resume_pipeline_at /
    resume_pipeline_from_start, both triggered by an explicit board click,
    never by this function)."""
    config = await config_for_path(project_path)
    return await tracker.client_for(config).last_recommendation_comment(config, story_id)
