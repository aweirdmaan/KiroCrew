"""Automatic pipeline advancement: nothing here needs a human to click
"Run next job". A background tick (``autorun_loop``, wired into the
gateway in ``dashboard/server.py``) runs every story that is NOT blocked
on a human - either it hasn't started yet (Backlog), or its last stop
wasn't a real block (e.g. a self-healing "missing" run).

A story genuinely waiting on a person (an unanswered OPEN QUESTIONS post,
a gate that needs a fix, an approval) is left alone until they act. That
"has a human acted yet" question is answered the SAME way for every kind
of gate, rather than teaching this module each gate's own shape: every
rocket-* skill's human-facing answer/approval/fix note arrives as a new
beads comment (confirm-plan's OPEN QUESTIONS answer, approval-check's
APPROVED, a note left after a manual code fix) - so "a comment was posted
since this story stopped" is the one generic unblock signal this module
needs, computed once here instead of once per gate type.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from kiro_crew.bd_cli import run_bd
from kiro_crew.board import engine as board_engine
from kiro_crew.board import state as board_state
from kiro_crew.board.stories import StorySummary, list_stories

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner

logger = logging.getLogger(__name__)

_TICK_INTERVAL_S = 15.0

# Stopped on its own, for a reason only a human can resolve (an unanswered
# question, a gate that failed and exhausted its own automatic loop, or a
# plain skill failure) - eligible for retry, but only once a NEW comment
# shows up (see _should_autorun).
_BLOCKED_STATUSES = {"gate_failed", "failed"}
# An infra hiccup, not a content problem (see engine.reconcile_stale_running's
# own docstring: the run vanished from Task Runner's own bookkeeping, most
# often a gateway restart mid-run) - nothing for a human to look at, so this
# retries immediately rather than waiting for a comment that will never come.
_SELF_HEAL_STATUSES = {"missing"}
# "cancelled" is deliberately NOT in either set above: a human explicitly
# stopping a run is a real stop, not a block waiting on an answer, and this
# module never overrides an explicit cancel on its own initiative.


async def _should_autorun(project_path: str, story: StorySummary) -> bool:
    if story.phase is None:
        return True  # Backlog: nothing blocks it, start it.
    if story.phase != "pipeline":
        return False  # "review"/"done" are manual-only (see phases.py).
    if story.current_run is not None:
        return False  # Already running, or its result isn't reconciled yet.

    history = board_state.load_history(project_path, story.id)
    entries = history.get("history") or []
    if not entries:
        return True  # Labeled but genuinely never run yet.

    last_status = entries[-1].get("status")
    if last_status in _SELF_HEAL_STATUSES:
        return True
    if last_status not in _BLOCKED_STATUSES:
        return False  # e.g. "cancelled" - an explicit stop, left alone.

    comments = await run_bd(project_path, "comments", story.id)
    current_count = len(comments) if isinstance(comments, list) else 0
    seen = history.get("autorun_seen_comment_count")
    if seen is None:
        # First time this module has noticed the block - remember where the
        # comment count stood and wait for something NEW. Without this, the
        # very comment that caused the block (e.g. confirm's own
        # "GATE: FAIL ..." post) would itself read as "a human just acted",
        # and the pipeline would spin retrying instantly forever.
        history["autorun_seen_comment_count"] = current_count
        board_state.save_history(project_path, story.id, history)
        return False
    if current_count > seen:
        history["autorun_seen_comment_count"] = current_count
        board_state.save_history(project_path, story.id, history)
        return True
    return False


async def autorun_tick(task_runner: "TaskRunner", project_paths: list[str]) -> None:
    """One pass over every configured project's stories, starting or
    resuming whichever ones are eligible. Safe to call repeatedly and
    concurrently with a human's own actions: run_job is itself idempotent
    (a story already in flight just returns its existing task_id - see its
    own docstring), so a race with a manual run, or with this tick firing
    again before the last one's work settled, costs nothing beyond a
    redundant check.
    """
    for project_path in project_paths:
        try:
            stories = await list_stories(project_path)
        except Exception:
            logger.warning("board autorun: listing stories failed for %s", project_path, exc_info=True)
            continue
        for story in stories:
            try:
                if await _should_autorun(project_path, story):
                    await board_engine.run_job(task_runner, project_path, story.id)
            except board_engine.BoardError:
                continue  # Phase changed under us (e.g. a human just advanced it) - fine.
            except Exception:
                logger.warning("board autorun: tick failed for story %s", story.id, exc_info=True)


async def autorun_loop(
    get_task_runner: Callable[[], object | None],
    get_project_paths: Callable[[], Awaitable[list[str]]],
) -> None:
    """Runs ``autorun_tick`` on a timer for the life of the gateway.

    Both arguments are callables, not values, so each tick reads the
    CURRENT task runner and config - the task runner in particular may not
    exist yet the moment this loop starts (it is wired up elsewhere during
    boot), and the configured project paths can change at runtime.
    """
    while True:
        try:
            task_runner = get_task_runner()
            if task_runner is not None:
                project_paths = await get_project_paths()
                if project_paths:
                    await autorun_tick(task_runner, project_paths)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad tick must not take the gateway down
            logger.warning("board autorun: loop iteration failed", exc_info=True)
        await asyncio.sleep(_TICK_INTERVAL_S)
