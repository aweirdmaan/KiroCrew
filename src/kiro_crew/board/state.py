"""Durable state for a board story: its current phase (a beads label) and its
job-run history (a small sidecar JSON file).

Beads remains the source of truth for phase, by design: ``phase:<key>`` is a
plain beads label, readable with ``bd list --label phase:<key>`` by anyone,
not only this engine. The history file is a cache/index only - timing and
task-runner run ids for the timeline view, which beads itself has no concept
of - and is fully reconstructible (modulo exact timestamps) by replaying the
label and comment history on the issue if it is ever lost or deleted.
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

from kiro_crew.bd_cli import run_bd

logger = logging.getLogger(__name__)

_LABEL_PREFIX = "phase:"
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
    """Returns ``{"current_run": dict|None, "history": [dict, ...]}``.

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
    rows = await run_bd(project_path, "list", "--all", "--limit", "0", "--id", story_id)
    if not isinstance(rows, list) or not rows:
        return None
    labels = rows[0].get("labels") or []
    for label in labels:
        if isinstance(label, str) and label.startswith(_LABEL_PREFIX):
            return label[len(_LABEL_PREFIX):]
    return None


async def set_phase(project_path: str, story_id: str, new_phase: str) -> bool:
    """Swap the story's `phase:*` label for `phase:<new_phase>`. Removes
    every existing phase:* label first (there should only ever be one, but a
    hand-edited issue could carry a stale extra one - this makes the engine
    self-healing rather than silently picking an arbitrary one later)."""
    rows = await run_bd(project_path, "list", "--all", "--limit", "0", "--id", story_id)
    existing = []
    if isinstance(rows, list) and rows:
        existing = [
            label for label in (rows[0].get("labels") or [])
            if isinstance(label, str) and label.startswith(_LABEL_PREFIX)
        ]
    for label in existing:
        await run_bd(project_path, "label", "remove", story_id, label)
    result = await run_bd(project_path, "label", "add", story_id, f"{_LABEL_PREFIX}{new_phase}")
    return result is not None


async def last_gate_comment(project_path: str, story_id: str) -> str | None:
    """The last comment line starting with "GATE:" across every comment on
    the issue, in chronological order - the exact semantics of
    `bd comments <id> | grep '^GATE:' | tail -1` that rocket-gate-check.sh
    (and rocket-dag.sh's gate() function) already use."""
    comments = await run_bd(project_path, "comments", story_id)
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


def gate_passed(gate_line: str | None) -> bool:
    return bool(gate_line) and gate_line.startswith("GATE: PASS")
