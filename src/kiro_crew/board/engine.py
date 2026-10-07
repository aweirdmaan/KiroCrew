"""Submits a board story's next job and advances it through the pipeline.

One in-process background task per story watches that story's in-flight
Task Runner run to completion and reconciles the result (same shape as the
SSE stream's producer/consumer split in dashboard/handlers/taskrunner.py: a
module-level dict keyed by story id, idempotent re-entry, no separate
service polling every story all the time - only stories with something
actually in flight cost anything).
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


def _render_task_yaml(task: Task, story_id: str) -> str:
    # Mirrors rocket-dag.sh's render_template technique exactly, just
    # generated in-memory instead of read from a committed .kiro/workflows/
    # file - the existing files are never read or touched by this engine.
    return (
        "agents:\n"
        f"  {task.key}:\n"
        "    prompt: >\n"
        f"      Read .kiro/skills/{task.skill}/SKILL.md and follow it for"
        f" epic/story {story_id}.\n"
        "    depends_on: []\n"
    )


def _write_tmp_yaml(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kc-board-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


async def _submit(
    task_runner: "TaskRunner", project_path: str, story_id: str,
    phase_key: str, task: Task,
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
    tmp_path = _write_tmp_yaml(_render_task_yaml(task, story_id))
    return await task_runner.start_background(
        tmp_path,
        agent=task.agent,
        name=f"board:{story_id}:{phase_key}:{task.key}",
        source="dashboard",
        workspace_dir=project_path,
        auto_approve=True,
    )


def _task_terminal_result(task_runner: "TaskRunner", task_id: str) -> tuple[str, str] | None:
    """("passed"|<run status>, combined result text), or None if the run
    isn't terminal yet (still "running").

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
    task_status = "passed" if run_status in ("completed", "passed") else run_status
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
        # watcher for this story_id (auto-chaining verify -> fix, looping an
        # implementation iteration, etc.) before this task gets here - only
        # pop the entry if it still points at THIS task, so a popped key
        # doesn't orphan a watcher that already replaced it.
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
    passed = task_status in ("passed", "completed")

    def _finish(run_status: str) -> None:
        current["status"] = run_status
        current["finished_at"] = now
        data["history"].append(dict(current))

    if phase_key == "review":
        # The one-off retro task, not part of PHASES - always terminal, no
        # gate, no loop: success advances to Done, failure just leaves the
        # story in Review for the human to retry "Mark reviewed" later.
        _finish("passed" if passed else "failed")
        if passed:
            await board_state.set_phase(project_path, story_id, "done")
        data["current_run"] = None
        board_state.save_history(project_path, story_id, data)
        return

    phase = PHASE_BY_KEY.get(phase_key)
    if phase is None:
        logger.warning("board: reconcile saw unknown phase %r for story %s", phase_key, story_id)
        return

    if not passed:
        _finish("failed")
        data["current_run"] = None
        board_state.save_history(project_path, story_id, data)
        return

    if phase.loop == "until_all_tasks_complete":
        if "ALL_TASKS_COMPLETE" in result_text:
            _finish("passed")
            await _advance_phase(project_path, story_id, phase_key, data, now)
            return
        if iteration + 1 >= phase.loop_cap:
            _finish("failed")  # exceeded the iteration cap - needs a human
            data["current_run"] = None
            board_state.save_history(project_path, story_id, data)
            return
        _finish("passed")
        await _resubmit(task_runner, project_path, story_id, phase, task_key, iteration + 1, data)
        return

    task_idx = next((i for i, t in enumerate(phase.tasks) if t.key == task_key), -1)
    is_last_task = task_idx == len(phase.tasks) - 1

    if not is_last_task:
        # Auto-chain within the phase (verify -> fix -> confirm): no gate,
        # no human needed between these, matching rocket-dag.sh's own
        # unattended sequence for this exact combination.
        _finish("passed")
        next_task = phase.tasks[task_idx + 1]
        await _resubmit(task_runner, project_path, story_id, phase, next_task.key, 0, data)
        return

    if phase.gate:
        gate_line = await board_state.last_gate_comment(project_path, story_id)
        if board_state.gate_passed(gate_line):
            _finish("passed")
            await _advance_phase(project_path, story_id, phase_key, data, now)
            return
        if phase.loop == "until_gate_pass" and iteration + 1 < phase.loop_cap and phase.loop_back_to:
            _finish("gate_failed")
            loop_task = next(t for t in phase.tasks if t.key == phase.loop_back_to)
            await _resubmit(task_runner, project_path, story_id, phase, loop_task.key, iteration + 1, data)
            return
        # Gate failed and either this phase doesn't loop, or the cap is
        # reached: stay here. A human comments the fix/approval beads expects
        # and clicks "Run next job" again, which re-checks the fresh comment.
        _finish("gate_failed")
        data["current_run"] = None
        board_state.save_history(project_path, story_id, data)
        return

    _finish("passed")
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


async def run_job(task_runner: "TaskRunner", project_path: str, story_id: str) -> str:
    """Submit the story's next job. Returns the new task_id.

    Idempotent: if a job for this story is already in flight, returns the
    existing task_id instead of submitting a duplicate.
    """
    existing = _inflight.get(story_id)
    if existing is not None and not existing.done():
        data = board_state.load_history(project_path, story_id)
        current = data.get("current_run")
        if current and current.get("task_id"):
            return current["task_id"]

    phase_key = await board_state.get_phase(project_path, story_id)
    if phase_key is None:
        phase_key = next_phase_key(None)  # "grooming" - leaving backlog
        await board_state.set_phase(project_path, story_id, phase_key)

    phase = PHASE_BY_KEY.get(phase_key)
    if phase is None or phase.manual:
        raise BoardError(f"story {story_id} is in phase {phase_key!r}, which has no runnable job")

    data = board_state.load_history(project_path, story_id)
    current = data.get("current_run")
    resume_task_key = current.get("task_key") if current and current.get("phase") == phase_key else None
    task_key = resume_task_key or phase.tasks[0].key
    iteration = current.get("iteration", 0) if resume_task_key else 0

    task = next((t for t in phase.tasks if t.key == task_key), phase.tasks[0])
    task_id = await _submit(task_runner, project_path, story_id, phase.key, task)
    data["current_run"] = {
        "phase": phase.key, "task_key": task.key, "iteration": iteration,
        "task_id": task_id, "status": "running",
        "started_at": datetime.now().isoformat(), "finished_at": None,
    }
    board_state.save_history(project_path, story_id, data)
    _inflight[story_id] = asyncio.create_task(
        _watch_and_reconcile(task_runner, project_path, story_id, task_id)
    )
    return task_id


async def complete_review(task_runner: "TaskRunner", project_path: str, story_id: str) -> str:
    """The one manual transition: Review -> Done, running rocket-retro."""
    phase_key = await board_state.get_phase(project_path, story_id)
    if phase_key != "review":
        raise BoardError(f"story {story_id} is in phase {phase_key!r}, not 'review'")
    existing = _inflight.get(story_id)
    if existing is not None and not existing.done():
        data = board_state.load_history(project_path, story_id)
        current = data.get("current_run")
        if current and current.get("task_id"):
            return current["task_id"]

    task_id = await _submit(task_runner, project_path, story_id, "review", REVIEW_COMPLETION_TASK)
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
