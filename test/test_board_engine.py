"""Tests for kiro_crew.board.engine: job submission, phase advancement,
per-task gate checks, and the implement loop - against the 5-lane pipeline
(Planning -> Implementation -> Review -> Done) that mirrors crew-rocket's
own rocket-dag.sh exactly (see phases.py's module docstring).

Uses two test doubles rather than mocking the engine's own internals:
- FakeTaskRunner: a minimal stand-in for TaskRunner.start_background/.status,
  letting a test script exactly when a submitted job becomes terminal and
  with what result text.
- FakeBeads: an in-memory labels/comments store behind run_bd, so
  board.state's real get_phase/set_phase/last_gate_comment logic runs
  unmodified against it.

Both talk through the same seams production code uses (start_background,
status(), run_bd), so these tests exercise the real reconcile/advance state
machine in engine.py, not a re-implementation of it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

from kiro_crew.board import engine as board_engine
from kiro_crew.board import state as board_state
from kiro_crew.board.phases import PHASE_BY_KEY


@pytest.fixture(autouse=True)
def _tmp_yaml_in_pytest_tmp_path(tmp_path, monkeypatch):
    # engine._submit deliberately no longer deletes the per-job YAML it
    # writes (see its own docstring: start_background reads it in the
    # background, after the call returns, so deleting it right away raced
    # that read in production). That's the right tradeoff for a real run,
    # but left unpatched here it leaks a tiny file into the real system temp
    # dir on every test in this file, every run - redirecting mkstemp's
    # directory to pytest's own self-cleaning tmp_path keeps that cost where
    # it belongs instead of accumulating on this machine indefinitely.
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(board_engine.tempfile, "mkstemp", _mkstemp)


class FakeTaskRunner:
    def __init__(self):
        self.submissions: list[dict] = []
        self._next_id = 0
        self._runs: dict[str, dict] = {}

    async def start_background(self, spec_path, agent="", name="", source="",
                                workspace_dir="", auto_approve=False):
        self._next_id += 1
        task_id = f"task-{self._next_id}"
        with open(spec_path, encoding="utf-8") as fh:
            content = fh.read()
        self.submissions.append({
            "task_id": task_id, "content": content, "agent": agent,
            "name": name, "workspace_dir": workspace_dir,
        })
        self._runs[task_id] = {
            "task_id": task_id, "status": "running", "running": True,
            "task_details": [{"status": "in_progress", "result": ""}],
        }
        return task_id

    def status(self) -> dict:
        return {"runs": list(self._runs.values())}

    def set_result(self, task_id: str, status: str, result: str = "") -> None:
        self._runs[task_id] = {
            "task_id": task_id, "status": status, "running": False,
            "task_details": [{"status": status, "result": result}],
        }

    def set_result_with_replan(self, task_id: str, run_status: str, task_details: list[dict]) -> None:
        """Simulates Task Runner's own auto-replan: the single task this
        engine submitted can fail mid-turn and get several recovery tasks
        appended, with the RUN still finishing successfully even though
        task_details[0] (the originally-submitted task) is "failed"."""
        self._runs[task_id] = {
            "task_id": task_id, "status": run_status, "running": False,
            "task_details": task_details,
        }

    @property
    def last_task_id(self) -> str:
        return self.submissions[-1]["task_id"]


class FakeBeads:
    def __init__(self):
        self.labels: dict[str, set[str]] = {}
        self.comments: dict[str, list[dict]] = {}

    async def run_bd(self, project_path: str, *args):
        if args[0] == "list":
            story_id = args[-1]
            labels = sorted(self.labels.get(story_id, set()))
            row = {"id": story_id}
            if labels:
                row["labels"] = labels
            return [row]
        if args[0] == "label" and args[1] == "add":
            self.labels.setdefault(args[2], set()).add(args[3])
            return [{"status": "added"}]
        if args[0] == "label" and args[1] == "remove":
            self.labels.get(args[2], set()).discard(args[3])
            return [{"status": "removed"}]
        if args[0] == "comments":
            return list(self.comments.get(args[1], []))
        raise AssertionError(f"unexpected bd args: {args}")

    def gate(self, story_id: str, verdict: str, reason: str = "") -> None:
        self.comments.setdefault(story_id, []).append({"text": f"GATE: {verdict} {reason}".strip()})


async def _await_inflight(story_id: str) -> None:
    """Wait for the background reconcile task this engine spawned to finish
    one full poll-and-settle cycle. The watcher loop sleeps _POLL_INTERVAL_S
    between polls, so tests patch that to ~0 and just await the tracked
    task directly instead of sleeping in real time."""
    task = board_engine._inflight.get(story_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


@pytest.fixture(autouse=True)
def _fast_poll():
    with patch.object(board_engine, "_POLL_INTERVAL_S", 0):
        yield


@pytest.fixture(autouse=True)
def _clear_inflight():
    board_engine._inflight.clear()
    yield
    board_engine._inflight.clear()


@pytest.fixture()
def beads():
    return FakeBeads()


@pytest.fixture(autouse=True)
def _patch_run_bd(beads):
    with patch.object(board_state, "run_bd", beads.run_bd):
        yield


class TestRunJobBasics:
    @pytest.mark.asyncio
    async def test_backlog_story_starts_at_planning(self, tmp_path, beads):
        tr = FakeTaskRunner()
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert task_id == tr.last_task_id
        assert "rocket-ideate" in tr.submissions[0]["content"]
        assert tr.submissions[0]["agent"] == "meowth"
        assert beads.labels["s-1"] == {"phase:planning"}

    @pytest.mark.asyncio
    async def test_second_call_while_running_is_idempotent(self, tmp_path, beads):
        tr = FakeTaskRunner()
        first = await board_engine.run_job(tr, str(tmp_path), "s-1")
        second = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert first == second
        assert len(tr.submissions) == 1

    @pytest.mark.asyncio
    async def test_manual_phase_raises_board_error(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:review"}
        with pytest.raises(board_engine.BoardError):
            await board_engine.run_job(tr, str(tmp_path), "s-1")


class TestPlanningPhase:
    @pytest.mark.asyncio
    async def test_ideate_auto_chains_to_plan_no_gate(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        ideate_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(ideate_id, "passed", "epic created")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:planning"}  # unchanged - still mid-phase
        assert len(tr.submissions) == 2
        assert "rocket-plan" in tr.submissions[1]["content"]
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"]["task_key"] == "plan"

    @pytest.mark.asyncio
    async def test_plan_finishing_advances_to_implementation_even_unanswered(self, tmp_path, beads):
        # Planning itself has no gate - rocket-plan ends in OPEN QUESTIONS
        # and rocket-dag.sh's cmd_plan just stops there; the gate that
        # actually checks the questions got answered lives on
        # Implementation's own first task (confirm_plan), not here.
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        ideate_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(ideate_id, "passed", "epic created")
        await _await_inflight("s-1")
        plan_id = tr.last_task_id
        tr.set_result(plan_id, "passed", "plan posted with OPEN QUESTIONS")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}

    @pytest.mark.asyncio
    async def test_run_level_completed_advances_even_if_the_submitted_task_itself_shows_failed(
        self, tmp_path, beads,
    ):
        # Regression, caught live: Task Runner can auto-replan a single
        # submitted task into several recovery tasks after a mid-turn
        # failure (the real case: "ACP process not running" after the
        # machine slept). task_details[0] (the task THIS engine submitted)
        # stays "failed" forever, but the recovery tasks did the real work
        # and the RUN finished "completed".
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        ideate_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(ideate_id, "passed", "epic created")
        await _await_inflight("s-1")
        plan_id = tr.last_task_id
        tr.set_result_with_replan(plan_id, "completed", [
            {"status": "failed", "result": "", "error": "ACP process not running"},
            {"status": "passed", "result": "ACP confirmed running"},
            {"status": "passed", "result": "plan posted to beads with OPEN QUESTIONS"},
        ])
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_failed_task_does_not_advance(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(task_id, "failed", "boom")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:planning"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "failed"
        assert data["current_run"] is None


def _patched_cap(task_key: str, cap: int):
    """dataclasses.replace-free patch: Task/Phase are frozen, so swap the
    module's PHASE_BY_KEY["implementation"] entry for a copy whose matching
    task has a lower loop_cap, for exactly one test's duration."""
    import dataclasses

    original = PHASE_BY_KEY["implementation"]
    new_tasks = tuple(
        dataclasses.replace(t, loop_cap=cap) if t.key == task_key else t
        for t in original.tasks
    )
    patched = dataclasses.replace(original, tasks=new_tasks)
    return patch.dict(PHASE_BY_KEY, {"implementation": patched})


class TestImplementationPhase:
    @pytest.mark.asyncio
    async def test_confirm_plan_gate_fail_stops_in_place(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert "rocket-confirm-plan" in tr.submissions[0]["content"]
        beads.gate("s-1", "FAIL", "unanswered question")
        tr.set_result(task_id, "passed", "ran confirm-plan")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}  # unchanged
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "gate_failed"
        assert data["current_run"] is None

    @pytest.mark.asyncio
    async def test_rerun_after_gate_fail_restarts_from_confirm_plan_not_mid_phase(self, tmp_path, beads):
        # The idempotency guarantee: a human answers the questions, then
        # clicks "Run next job" again - this must resubmit confirm_plan
        # (task[0] of Implementation), not resume "where it left off" at
        # some other task, and not skip straight to approval_check either.
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        first_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "FAIL", "unanswered question")
        tr.set_result(first_id, "passed", "ran confirm-plan")
        await _await_inflight("s-1")

        beads.gate("s-1", "PASS", "plan confirmed")  # human answered, then...
        second_id = await board_engine.run_job(tr, str(tmp_path), "s-1")  # ...clicks Run again
        assert "rocket-confirm-plan" in tr.submissions[-1]["content"]
        tr.set_result(second_id, "passed", "ran confirm-plan")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        assert "rocket-approval-check" in tr.submissions[-1]["content"]

    @pytest.mark.asyncio
    async def test_confirm_plan_gate_pass_chains_to_approval_check(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS")
        tr.set_result(task_id, "passed", "confirmed")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        assert "rocket-approval-check" in tr.submissions[-1]["content"]

    @pytest.mark.asyncio
    async def test_approval_check_gate_fail_stops(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        confirm_plan_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS", "plan confirmed")
        tr.set_result(confirm_plan_id, "passed", "confirmed")
        await _await_inflight("s-1")
        approval_id = tr.last_task_id
        # approval_check's own GATE comment supersedes the earlier PASS -
        # last_gate_comment reads the LAST GATE: line across the thread.
        beads.gate("s-1", "FAIL", "no APPROVED comment yet")
        tr.set_result(approval_id, "passed", "checked approval")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "gate_failed"

    @pytest.mark.asyncio
    async def test_implement_loops_until_all_tasks_complete_then_chains_to_verify(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        confirm_plan_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS")
        tr.set_result(confirm_plan_id, "passed", "confirmed")
        await _await_inflight("s-1")
        approval_id = tr.last_task_id
        beads.gate("s-1", "PASS", "APPROVED")
        tr.set_result(approval_id, "passed", "approved")
        await _await_inflight("s-1")

        implement_id = tr.last_task_id
        assert "rocket-implement" in tr.submissions[-1]["content"]
        tr.set_result(implement_id, "passed", "still working")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}  # still looping
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"]["task_key"] == "implement"
        assert data["current_run"]["iteration"] == 1

        implement_id_2 = tr.last_task_id
        tr.set_result(implement_id_2, "passed", "ALL_TASKS_COMPLETE")
        await _await_inflight("s-1")
        assert "rocket-verify" in tr.submissions[-1]["content"]
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"]["task_key"] == "verify"
        assert data["current_run"]["iteration"] == 0

    @pytest.mark.asyncio
    async def test_implement_loop_cap_exceeded_fails(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        confirm_plan_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS")
        tr.set_result(confirm_plan_id, "passed", "confirmed")
        await _await_inflight("s-1")
        approval_id = tr.last_task_id
        beads.gate("s-1", "PASS", "APPROVED")
        tr.set_result(approval_id, "passed", "approved")
        await _await_inflight("s-1")

        with _patched_cap("implement", 1):
            implement_id = tr.last_task_id
            tr.set_result(implement_id, "passed", "still working")
            await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "failed"
        assert data["current_run"] is None

    async def _reach_verify(self, tr: FakeTaskRunner, beads: FakeBeads, tmp_path) -> str:
        beads.labels["s-1"] = {"phase:implementation"}
        confirm_plan_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS")
        tr.set_result(confirm_plan_id, "passed", "confirmed")
        await _await_inflight("s-1")
        approval_id = tr.last_task_id
        beads.gate("s-1", "PASS", "APPROVED")
        tr.set_result(approval_id, "passed", "approved")
        await _await_inflight("s-1")
        implement_id = tr.last_task_id
        tr.set_result(implement_id, "passed", "ALL_TASKS_COMPLETE")
        await _await_inflight("s-1")
        return tr.last_task_id  # verify's task_id

    @pytest.mark.asyncio
    async def test_verify_fix_auto_chain_no_gate(self, tmp_path, beads):
        tr = FakeTaskRunner()
        verify_id = await self._reach_verify(tr, beads, tmp_path)
        assert "rocket-verify" in tr.submissions[-1]["content"]
        tr.set_result(verify_id, "passed", "found issues")
        await _await_inflight("s-1")
        assert "rocket-fix" in tr.submissions[-1]["content"]
        assert beads.labels["s-1"] == {"phase:implementation"}

    @pytest.mark.asyncio
    async def test_confirm_gate_fail_stops_before_pr(self, tmp_path, beads):
        tr = FakeTaskRunner()
        verify_id = await self._reach_verify(tr, beads, tmp_path)
        tr.set_result(verify_id, "passed", "found issues")
        await _await_inflight("s-1")
        fix_id = tr.last_task_id
        tr.set_result(fix_id, "passed", "fixed")
        await _await_inflight("s-1")
        confirm_id = tr.last_task_id
        assert "rocket-confirm" in tr.submissions[-1]["content"]

        beads.gate("s-1", "FAIL", "still broken")
        tr.set_result(confirm_id, "passed", "not quite")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}  # unchanged
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "gate_failed"

    @pytest.mark.asyncio
    async def test_confirm_gate_pass_chains_to_pr_then_retro_then_advances_to_review(self, tmp_path, beads):
        tr = FakeTaskRunner()
        verify_id = await self._reach_verify(tr, beads, tmp_path)
        tr.set_result(verify_id, "passed", "found issues")
        await _await_inflight("s-1")
        fix_id = tr.last_task_id
        tr.set_result(fix_id, "passed", "fixed")
        await _await_inflight("s-1")
        confirm_id = tr.last_task_id

        beads.gate("s-1", "PASS", "looks good")
        tr.set_result(confirm_id, "passed", "looks good")
        await _await_inflight("s-1")
        assert "rocket-pr" in tr.submissions[-1]["content"]
        pr_id = tr.last_task_id

        tr.set_result(pr_id, "passed", "MR opened")
        await _await_inflight("s-1")
        assert "rocket-retro" in tr.submissions[-1]["content"]
        retro_id = tr.last_task_id

        tr.set_result(retro_id, "passed", "retro posted")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:review"}


class TestTaskStateHandling:
    @pytest.mark.asyncio
    async def test_cancelled_run_is_recorded_distinctly_not_as_failed(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(task_id, "cancelled", "")
        await _await_inflight("s-1")
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "cancelled"
        assert beads.labels["s-1"] == {"phase:planning"}

    @pytest.mark.asyncio
    async def test_vanished_run_is_recorded_as_missing_not_failed(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:planning"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        del tr._runs[task_id]  # simulate the run vanishing from Task Runner's own status
        await _await_inflight("s-1")
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "missing"


class TestReconcileStaleRunning:
    @pytest.mark.asyncio
    async def test_a_running_entry_with_no_live_watcher_is_marked_missing(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        await board_engine.run_job(tr, str(tmp_path), "s-1")
        # Simulate a process restart: the in-process watcher is just gone,
        # but the history file still says "running" from before.
        board_engine._inflight.clear()

        data = board_engine.reconcile_stale_running(str(tmp_path), "s-1")
        assert data["current_run"] is None
        assert data["history"][-1]["status"] == "missing"

    @pytest.mark.asyncio
    async def test_a_genuinely_in_flight_run_is_left_alone(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        await board_engine.run_job(tr, str(tmp_path), "s-1")
        # The watcher is still live (this process submitted it moments ago).

        data = board_engine.reconcile_stale_running(str(tmp_path), "s-1")
        assert data["current_run"] is not None
        assert data["current_run"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_running_next_job_self_heals_a_stale_entry_before_resubmitting(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        first_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        board_engine._inflight.clear()  # simulate the restart

        second_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert second_id != first_id
        data = board_state.load_history(str(tmp_path), "s-1")
        assert any(h["status"] == "missing" and h["task_id"] == first_id for h in data["history"])

    def test_no_current_run_is_a_no_op(self, tmp_path):
        data = board_engine.reconcile_stale_running(str(tmp_path), "s-1")
        assert data == {"current_run": None, "history": []}


class TestReviewCompletion:
    @pytest.mark.asyncio
    async def test_complete_review_runs_harvest_with_mr_url_then_advances_to_done(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:review"}
        task_id = await board_engine.complete_review(tr, str(tmp_path), "s-1", "https://example.com/mr/1")
        assert "rocket-harvest" in tr.submissions[0]["content"]
        assert "https://example.com/mr/1" in tr.submissions[0]["content"]
        tr.set_result(task_id, "passed", "harvest done")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:done"}

    @pytest.mark.asyncio
    async def test_complete_review_wrong_phase_raises(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        with pytest.raises(board_engine.BoardError):
            await board_engine.complete_review(tr, str(tmp_path), "s-1", "https://example.com/mr/1")

    @pytest.mark.asyncio
    async def test_failed_harvest_leaves_story_in_review(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:review"}
        task_id = await board_engine.complete_review(tr, str(tmp_path), "s-1", "https://example.com/mr/1")
        tr.set_result(task_id, "failed", "boom")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:review"}


class _CapturingRunner:
    """Bare double that only records the spec_path it was handed - unlike
    FakeTaskRunner, it does NOT read the file synchronously, so it cannot
    accidentally mask a caller that deletes the file too early."""

    def __init__(self):
        self.spec_path: str | None = None

    async def start_background(self, spec_path, **kwargs):
        self.spec_path = spec_path
        return "task-x"


class TestSubmitLeavesSpecFileInPlace:
    @pytest.mark.asyncio
    async def test_submit_does_not_delete_the_spec_file_it_just_created(self):
        # Regression: start_background only SCHEDULES the run - it reads and
        # decomposes the spec in the background, after this call already
        # returned. _submit used to delete the temp file immediately in a
        # `finally`, racing that read; a real run against it failed with
        # "Spec not found" (caught live, not by the test suite at the time,
        # since FakeTaskRunner's synchronous read elsewhere in this file
        # masked the race entirely). This pins the fix directly: the file
        # must still exist and be readable after _submit returns.
        runner = _CapturingRunner()
        task = PHASE_BY_KEY["planning"].tasks[0]
        task_id = await board_engine._submit(runner, "/proj", "s-1", "planning", task)
        assert task_id == "task-x"
        assert runner.spec_path is not None
        assert os.path.exists(runner.spec_path)
        with open(runner.spec_path, encoding="utf-8") as f:
            assert "rocket-ideate" in f.read()
