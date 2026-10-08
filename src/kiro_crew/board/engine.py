"""Submits a board story's next job and advances it through the pipeline.

One in-process background task per story watches that story's in-flight
Task Runner run to completion and reconciles the result (same shape as the
SSE stream's producer/consumer split in dashboard/handlers/taskrunner.py: a
module-level dict keyed by story id, idempotent re-entry, no separate
service polling every story all the time - only stories with something
actually in flight cost anything).

Idempotency note (see phases.py's module docstring for the full reasoning):
``run_job`` always (re)starts a phase at its first task - it never tries to
resume partway through from history. A gate failure, a crash, or simply a
human clicking "Run next job" again after answering a question all look
identical to this function: "this phase's job is not in flight, start it
from task 0." That matches ``rocket-dag.sh``'s own unattended chain exactly,
and is only correct because every gated skill in the chain (confirm-plan
explicitly, approval-check and confirm effectively) is written to be safe
to re-invoke.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING

from kiro_crew.board import state as board_state
from kiro_crew.board.phases import (
    PHASE_BY_KEY,
    REVIEW_COMPLETION_TASK,
    Phase,
    Task,
    next_phase_key,
)

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2.0

# story_id -> background reconcile task, process-lifetime. Prevents a
# double-click (or a second browser tab) from submitting a second Task
# Runner run for the same story while one is already in flight.
_inflight: dict[str, asyncio.Task] = {}


class BoardError(Exception):
    """A board operation could not proceed (wrong phase, already running,
    nothing to run). Caught at the handler layer and turned into a 400."""


def _render_task_yaml(task: Task, story_id: str, extra_arg: str | None = None) -> str:
    # Mirrors rocket-dag.sh's render_template technique exactly, just
    # generated in-memory instead of read from a committed .kiro/workflows/
    # file - the existing files are never read or touched by this engine.
    # extra_arg covers the one task (harvest) whose skill takes an argument
    # other than the story/epic id - an MR/PR URL (see REVIEW_COMPLETION_TASK).
    target = f"{extra_arg}, story {story_id}" if extra_arg is not None else f"epic/story {story_id}"
    return (
        "agents:\n"
        f"  {task.key}:\n"
        "    prompt: >\n"
        f"      Read .kiro/skills/{task.skill}/SKILL.md and follow it for"
        f" {target}.\n"
        "    depends_on: []\n"
    )


def _write_tmp_yaml(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kc-board-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


async def _submit(
    task_runner: "TaskRunner", project_path: str, story_id: str,
    phase_key: str, task: Task, extra_arg: str | None = None,
) -> str:
    # start_background only SCHEDULES the run - it reads and decomposes the
    # spec file in the background, after this call already returned (same
    # async-submission contract its own callers in taskrunner.py rely on).
    # Deleting tmp_path right after awaiting this therefore races the actual
    # read and the run fails with "Spec not found" (caught by a real test
    # run, not a review). The dashboard's own inline-spec path has the same
    # shape and solves it the same way: just don't delete the file - a
    # handful-of-bytes YAML in the OS temp dir is a negligible, OS-swept cost
    # next to a torn-apart run.
    tmp_path = _write_tmp_yaml(_render_task_yaml(task, story_id, extra_arg))
    return await task_runner.start_background(
        tmp_path,
        agent=task.agent,
        name=f"board:{story_id}:{phase_key}:{task.key}",
        source="dashboard",
        workspace_dir=project_path,
        auto_approve=True,
    )


def _task_terminal_result(task_runner: "TaskRunner", task_id: str) -> tuple[str, str] | None:
    """("passed"|"failed"|"cancelled"|"missing", combined result text), or
    None if the run isn't terminal yet (still "running").

    Judges success from the RUN's own overall status, not task_details[0]:
    Task Runner can auto-replan a single submitted task into several
    recovery tasks when the first attempt fails mid-turn (confirmed live -
    "ACP process not running" after the machine slept, task_details[0]
    stayed "failed" while 5 auto-generated recovery tasks actually finished
    the real work and the RUN itself reached "completed"). Reading only
    task_details[0] reported the job as failed - leaving the story stuck
    and the next "Run next job" click re-running the whole skill from
    scratch, duplicating whatever it already posted (a real plan + its
    OPEN QUESTIONS comment, in the run that found this). The run's own
    status already accounts for that replan; task_details is only used
    here for its TEXT, stitched from every task in order so an
    ALL_TASKS_COMPLETE/gate-comment search sees what actually happened, not
    just the first (possibly superseded) attempt.

    "cancelled" and a vanished run ("missing" - the run disappeared from
    Task Runner's own in-memory status, e.g. a gateway restart losing an
    unpersisted entry) are kept distinct from a plain skill failure rather
    than collapsed into "failed": a human reading the timeline should see
    what actually happened, and a vanished run in particular did not fail
    on its own merits - it just needs a plain rerun, not a fix.
    """
    status = task_runner.status()
    entry = next((r for r in status["runs"] if r["task_id"] == task_id), None)
    if entry is None:
        return "missing", ""
    if entry.get("running") or entry["status"] == "running":
        return None
    run_status = entry["status"]
    details = entry.get("task_details") or []
    combined_result = "\n".join(d.get("result") or d.get("error") or "" for d in details)
    if run_status in ("completed", "passed"):
        task_status = "passed"
    elif run_status == "cancelled":
        task_status = "cancelled"
    else:
        task_status = "failed"
    return task_status, combined_result


async def _watch_and_reconcile(
    task_runner: "TaskRunner", project_path: str, story_id: str, task_id: str,
) -> None:
    try:
        while True:
            result = _task_terminal_result(task_runner, task_id)
            if result is not None:
                await _reconcile(task_runner, project_path, story_id, task_id, *result)
                return
            await asyncio.sleep(_POLL_INTERVAL_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("board: reconcile failed for story %s task %s", story_id, task_id)
    finally:
        # _reconcile may itself have already spawned and registered a NEW
        # watcher for this story_id (looping an implementation iteration,
        # etc.) before this task gets here - only pop the entry if it still
        # points at THIS task, so a popped key doesn't orphan a watcher that
        # already replaced it.
        if _inflight.get(story_id) is asyncio.current_task():
            _inflight.pop(story_id, None)


async def _reconcile(
    task_runner: "TaskRunner", project_path: str, story_id: str, task_id: str,
    task_status: str, result_text: str,
) -> None:
    data = board_state.load_history(project_path, story_id)
    current = data.get("current_run") or {}
    phase_key = current.get("phase", "")
    task_key = current.get("task_key", "")
    iteration = current.get("iteration", 0)
    now = datetime.now().isoformat()

    def _finish(run_status: str) -> None:
        current["status"] = run_status
        current["finished_at"] = now
        data["history"].append(dict(current))

    if phase_key == "review":
        # The one-off harvest task, not part of PHASES - always terminal,
        # no gate, no loop: success advances to Done, failure just leaves
        # the story in Review for the human to retry "Mark reviewed" later.
        _finish("passed" if task_status == "passed" else task_status)
        if task_status == "passed":
            await board_state.set_phase(project_path, story_id, "done")
        data["current_run"] = None
        board_state.save_history(project_path, story_id, data)
        return

    phase = PHASE_BY_KEY.get(phase_key)
    if phase is None:
        logger.warning("board: reconcile saw unknown phase %r for story %s", phase_key, story_id)
        return

    if task_status != "passed":
        _finish(task_status)  # "failed" | "cancelled" | "missing" - preserved, not collapsed
        data["current_run"] = None
        board_state.save_history(project_path, story_id, data)
        return

    task = next((t for t in phase.tasks if t.key == task_key), None)
    if task is None:
        logger.warning("board: reconcile saw unknown task %r in phase %r for story %s", task_key, phase_key, story_id)
        return

    if task.loop == "until_all_tasks_complete" and "ALL_TASKS_COMPLETE" not in result_text:
        if iteration + 1 >= task.loop_cap:
            _finish("failed")  # exceeded the iteration cap - needs a human
            data["current_run"] = None
            board_state.save_history(project_path, story_id, data)
            return
        _finish("passed")
        await _resubmit(task_runner, project_path, story_id, phase, task.key, iteration + 1, data)
        return

    if task.gate:
        gate_line = await board_state.last_gate_comment(project_path, story_id)
        if not board_state.gate_passed(gate_line):
            # Gate failed: stop right here, same as rocket-dag.sh's own
            # `gate()` check after confirm-plan/approval-check/confirm. A
            # human acts (answers questions, approves, fixes the break) and
            # clicks "Run next job" again, which restarts this phase from
            # its first task - see the module docstring on why that's
            # correct, not wasteful.
            _finish("gate_failed")
            data["current_run"] = None
            board_state.save_history(project_path, story_id, data)
            return

    _finish("passed")
    task_idx = next(i for i, t in enumerate(phase.tasks) if t.key == task_key)
    if task_idx + 1 < len(phase.tasks):
        next_task = phase.tasks[task_idx + 1]
        await _resubmit(task_runner, project_path, story_id, phase, next_task.key, 0, data)
    else:
        await _advance_phase(project_path, story_id, phase_key, data, now)


async def _resubmit(
    task_runner: "TaskRunner", project_path: str, story_id: str, phase: Phase,
    task_key: str, iteration: int, data: dict,
) -> None:
    task = next(t for t in phase.tasks if t.key == task_key)
    task_id = await _submit(task_runner, project_path, story_id, phase.key, task)
    data["current_run"] = {
        "phase": phase.key, "task_key": task_key, "iteration": iteration,
        "task_id": task_id, "status": "running",
        "started_at": datetime.now().isoformat(), "finished_at": None,
    }
    board_state.save_history(project_path, story_id, data)
    _inflight[story_id] = asyncio.create_task(
        _watch_and_reconcile(task_runner, project_path, story_id, task_id)
    )


async def _advance_phase(
    project_path: str, story_id: str, finished_phase_key: str, data: dict, now: str,
) -> None:
    data["current_run"] = None
    next_key = next_phase_key(finished_phase_key)
    if next_key is not None:
        await board_state.set_phase(project_path, story_id, next_key)
    board_state.save_history(project_path, story_id, data)


def reconcile_stale_running(project_path: str, story_id: str) -> dict:
    """A persisted ``current_run`` can say "running" long after the process
    watching it is gone - most commonly a gateway restart mid-run: the
    in-process ``_inflight`` watcher is Python-process-lifetime by design,
    so it's just gone, while the history file (and the real Task Runner run,
    if it's still alive elsewhere) carries on with no one polling it.
    Detected here as: marked "running", but no live ``_inflight`` entry for
    this story in THIS process. Recorded as "missing" - the same status a
    run that vanished from Task Runner's own status gets (see
    ``_task_terminal_result``'s docstring) - rather than left to claim
    forever that something is in progress when nothing is watching it.
    Called on every "Run next job" click and every board list read, so a
    stale entry self-heals the moment anyone looks, not just DO move on.
    Returns the (possibly updated) history dict.
    """
    data = board_state.load_history(project_path, story_id)
    current = data.get("current_run")
    if not current or current.get("status") != "running":
        return data
    watcher = _inflight.get(story_id)
    if watcher is not None and not watcher.done():
        return data  # genuinely still being watched by this process
    now = datetime.now().isoformat()
    current["status"] = "missing"
    current["finished_at"] = now
    data["history"].append(dict(current))
    data["current_run"] = None
    board_state.save_history(project_path, story_id, data)
    return data


async def run_job(task_runner: "TaskRunner", project_path: str, story_id: str) -> str:
    """Submit the story's next job - always starting the current phase at
    its first task (see the module docstring on why that's the right
    behavior, not a missing optimization). Returns the new task_id.

    Idempotent: if a job for this story is already in flight, returns the
    existing task_id instead of submitting a duplicate.
    """
    existing = _inflight.get(story_id)
    if existing is not None and not existing.done():
        data = board_state.load_history(project_path, story_id)
        current = data.get("current_run")
        if current and current.get("task_id"):
            return current["task_id"]

    reconcile_stale_running(project_path, story_id)

    phase_key = await board_state.get_phase(project_path, story_id)
    if phase_key is None:
        phase_key = next_phase_key(None)  # "pipeline" - leaving backlog
        await board_state.set_phase(project_path, story_id, phase_key)

    phase = PHASE_BY_KEY.get(phase_key)
    if phase is None or phase.manual:
        raise BoardError(f"story {story_id} is in phase {phase_key!r}, which has no runnable job")

    data = board_state.load_history(project_path, story_id)
    task = phase.tasks[0]
    task_id = await _submit(task_runner, project_path, story_id, phase.key, task)
    data["current_run"] = {
        "phase": phase.key, "task_key": task.key, "iteration": 0,
        "task_id": task_id, "status": "running",
        "started_at": datetime.now().isoformat(), "finished_at": None,
    }
    board_state.save_history(project_path, story_id, data)
    _inflight[story_id] = asyncio.create_task(
        _watch_and_reconcile(task_runner, project_path, story_id, task_id)
    )
    return task_id


async def complete_review(task_runner: "TaskRunner", project_path: str, story_id: str, mr_url: str) -> str:
    """The one manual transition: Review -> Done, running rocket-harvest
    against the MR/PR URL a human supplies after reading its review
    comments - matching rocket-dag.sh's separate `cmd_harvest`."""
    phase_key = await board_state.get_phase(project_path, story_id)
    if phase_key != "review":
        raise BoardError(f"story {story_id} is in phase {phase_key!r}, not 'review'")
    existing = _inflight.get(story_id)
    if existing is not None and not existing.done():
        data = board_state.load_history(project_path, story_id)
        current = data.get("current_run")
        if current and current.get("task_id"):
            return current["task_id"]

    task_id = await _submit(
        task_runner, project_path, story_id, "review", REVIEW_COMPLETION_TASK, extra_arg=mr_url,
    )
    data = board_state.load_history(project_path, story_id)
    data["current_run"] = {
        "phase": "review", "task_key": REVIEW_COMPLETION_TASK.key, "iteration": 0,
        "task_id": task_id, "status": "running",
        "started_at": datetime.now().isoformat(), "finished_at": None,
    }
    board_state.save_history(project_path, story_id, data)
    _inflight[story_id] = asyncio.create_task(
        _watch_and_reconcile(task_runner, project_path, story_id, task_id)
    )
    return task_id
