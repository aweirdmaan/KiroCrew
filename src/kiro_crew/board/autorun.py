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

Concurrency is capped on purpose (see _MAX_CONCURRENT_RUNS): a human
clicking "Run next job" naturally paces themselves, but nothing paces an
unattended tick - a tick that started every eligible story at once, with
each one's own chain spawning a fresh agent session the moment its
previous task finished, took down a real gateway during development
(nine concurrent sessions inside four minutes stalled the event loop past
the loop watchdog's dump-then-exit threshold). Bounding how many stories
may be actively running at once is what keeps this feature from being
able to do that again, not a tuning nicety.
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

# How many stories may have an autorun-started pipeline actively running
# at once, system-wide across every configured project. Each running story
# owns a real agent session (and that session's own chain spawns a NEW one
# per task), so this is a real resource bound, not a cosmetic one - see the
# module docstring for what happens without it. Conservative on purpose;
# raise it only once the host's headroom under real concurrent agent load
# has actually been measured, not assumed.
_MAX_CONCURRENT_RUNS = 1

# Stopped on its own, for a reason only a human can resolve (an unanswered
# question, a gate that failed and exhausted its own automatic loop, or a
# plain skill failure) - eligible for retry, but only once a NEW comment
# shows up (see _autorun_action).
_BLOCKED_STATUSES = {"gate_failed", "failed"}
# An infra hiccup, not a content problem (see engine.reconcile_stale_running's
# own docstring: the run vanished from Task Runner's own bookkeeping, most
# often a gateway restart mid-run) - nothing for a human to look at, so this
# retries immediately rather than waiting for a comment that will never come.
# Resumed at the EXACT task that went missing (engine.resume_last_task), not
# a full restart from ideate: the task is not known to be wrong, only
# untracked, and restarting the whole phase would re-run every earlier
# task's own real agent turn for nothing (confirmed live: a story whose only
# problem was a missing "implement" entry got ideate/plan/confirm_plan/
# approval_check all wastefully re-invoked before this fix).
_SELF_HEAL_STATUSES = {"missing"}
# "cancelled" is deliberately NOT in either set above: a human explicitly
# stopping a run is a real stop, not a block waiting on an answer, and this
# module never overrides an explicit cancel on its own initiative.


async def _autorun_action(project_path: str, story: StorySummary) -> str | None:
    """Which engine entrypoint (if any) this story is ready for right now:
    "run_job" (start Backlog, or a full restart once a block is lifted),
    "resume_last_task" (self-heal a "missing" run in place), or None (leave
    it alone - already running, manual-only, or genuinely blocked)."""
    if story.phase is None:
        return "run_job"  # Backlog: nothing blocks it, start it.
    if story.phase != "pipeline":
        return None  # "review"/"done" are manual-only (see phases.py).
    if story.current_run is not None:
        return None  # Already running, or its result isn't reconciled yet.

    history = board_state.load_history(project_path, story.id)
    entries = history.get("history") or []
    if not entries:
        return "run_job"  # Labeled but genuinely never run yet.

    last_entry = entries[-1]
    last_status = last_entry.get("status")
    if last_status in _SELF_HEAL_STATUSES:
        return "resume_last_task"
    if last_status not in _BLOCKED_STATUSES:
        return None  # e.g. "cancelled" - an explicit stop, left alone.

    comments = await run_bd(project_path, "comments", story.id)
    current_count = len(comments) if isinstance(comments, list) else 0
    # The baseline is keyed to THIS SPECIFIC failed run (its task_id), not
    # just to the story - a plain per-story counter that's never cleared is
    # a real bug, found live: every rocket-* skill posts its OWN comments as
    # part of normal operation (plan's decision log + OPEN QUESTIONS,
    # confirm's "GATE: FAIL ..."), so a stale baseline left over from an
    # EARLIER, already-resolved block eventually reads as "a human just
    # acted" purely because a LATER, unrelated run posted its own content -
    # triggering a full restart with no human involved at all, whose own
    # ideate/plan/confirm_plan posts push the count higher still, repeating
    # near-instantly. Confirmed in production: a 6-second gap between one
    # gate_failed and the next full restart, sustained for over an hour,
    # cascading into provider-side throttling. Keying the baseline to the
    # failing run's own task_id means a BRAND NEW failure always resets it
    # fresh and always waits at least one tick before any retry is possible.
    last_task_id = last_entry.get("task_id")
    seen_task_id = history.get("autorun_seen_task_id")
    seen_count = history.get("autorun_seen_comment_count")
    if seen_task_id != last_task_id:
        history["autorun_seen_task_id"] = last_task_id
        history["autorun_seen_comment_count"] = current_count
        board_state.save_history(project_path, story.id, history)
        return None
    if seen_count is not None and current_count > seen_count:
        history["autorun_seen_comment_count"] = current_count
        board_state.save_history(project_path, story.id, history)
        return "run_job"
    return None


def _is_actively_running(story: StorySummary) -> bool:
    return bool(story.current_run) and story.current_run.get("status") == "running"


async def autorun_tick(task_runner: "TaskRunner", project_paths: list[str]) -> None:
    """One pass over every configured project's stories, starting or
    resuming whichever ones are eligible - up to _MAX_CONCURRENT_RUNS
    actively running at once, system-wide (see the module docstring on
    why this is a hard resource bound, not a nicety). A story skipped for
    lack of budget this tick is simply picked up on a later one, once
    something currently running finishes or blocks.

    Safe to call repeatedly and concurrently with a human's own actions:
    both engine entrypoints used here are themselves idempotent (a story
    already in flight just returns its existing task_id), so a race with a
    manual action, or with this tick firing again before the last one's
    work settled, costs nothing beyond a redundant check.
    """
    all_stories: list[tuple[str, StorySummary]] = []
    for project_path in project_paths:
        try:
            stories = await list_stories(project_path)
        except Exception:
            logger.warning("board autorun: listing stories failed for %s", project_path, exc_info=True)
            continue
        all_stories.extend((project_path, story) for story in stories)

    budget = _MAX_CONCURRENT_RUNS - sum(1 for _, s in all_stories if _is_actively_running(s))
    if budget <= 0:
        return

    for project_path, story in all_stories:
        if budget <= 0:
            break
        try:
            action = await _autorun_action(project_path, story)
            if action == "run_job":
                await board_engine.run_job(task_runner, project_path, story.id)
                budget -= 1
            elif action == "resume_last_task":
                await board_engine.resume_last_task(task_runner, project_path, story.id)
                budget -= 1
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
