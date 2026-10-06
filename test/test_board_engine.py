"""Tests for kiro_crew.board.engine: job submission, phase advancement,
gate checks, and the implementation/verification loop logic.

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
    async def test_backlog_story_starts_at_grooming(self, tmp_path, beads):
        tr = FakeTaskRunner()
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert task_id == tr.last_task_id
        assert "rocket-ideate" in tr.submissions[0]["content"]
        assert tr.submissions[0]["agent"] == "meowth"
        assert beads.labels["s-1"] == {"phase:grooming"}

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


class TestSingleTaskPhaseAdvance:
    @pytest.mark.asyncio
    async def test_no_gate_phase_advances_on_success(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:grooming"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(task_id, "passed", "done")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:planning"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"] is None
        assert data["history"][-1]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_gated_phase_stays_on_gate_fail(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:plan_review"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "FAIL", "needs answers")
        tr.set_result(task_id, "passed", "ran confirm-plan")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:plan_review"}  # unchanged
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "gate_failed"

    @pytest.mark.asyncio
    async def test_gated_phase_advances_on_gate_pass(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:plan_review"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        beads.gate("s-1", "PASS")
        tr.set_result(task_id, "passed", "ran confirm-plan")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:approval"}

    @pytest.mark.asyncio
    async def test_failed_task_does_not_advance(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:grooming"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(task_id, "failed", "boom")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:grooming"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "failed"


class TestImplementationLoop:
    @pytest.mark.asyncio
    async def test_loops_until_all_tasks_complete(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(task_id, "passed", "still working")
        await _await_inflight("s-1")
        # Still in implementation - no ALL_TASKS_COMPLETE yet, resubmitted.
        assert beads.labels["s-1"] == {"phase:implementation"}
        assert len(tr.submissions) == 2
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"]["iteration"] == 1

        second_id = tr.last_task_id
        tr.set_result(second_id, "passed", "ALL_TASKS_COMPLETE")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:verification"}

    @pytest.mark.asyncio
    async def test_exceeding_loop_cap_stops_without_advancing(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:implementation"}
        with _patched_cap("implementation", 1):
            task_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
            tr.set_result(task_id, "passed", "still working")
            await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:implementation"}
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["history"][-1]["status"] == "failed"
        assert data["current_run"] is None


def _patched_cap(phase_key: str, cap: int):
    """dataclasses.replace-free patch: Phase is frozen, so swap the module's
    PHASE_BY_KEY entry for a copy with a lower loop_cap, for exactly one
    test's duration."""
    import dataclasses

    from kiro_crew.board.phases import PHASE_BY_KEY

    original = PHASE_BY_KEY[phase_key]
    patched = dataclasses.replace(original, loop_cap=cap)
    return patch.dict(PHASE_BY_KEY, {phase_key: patched})


class TestVerificationChain:
    @pytest.mark.asyncio
    async def test_verify_fix_confirm_chain_then_gate_pass_advances(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:verification"}

        verify_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        assert "rocket-verify" in tr.submissions[0]["content"]
        tr.set_result(verify_id, "passed", "found issues")
        await _await_inflight("s-1")
        assert len(tr.submissions) == 2
        assert "rocket-fix" in tr.submissions[1]["content"]

        fix_id = tr.last_task_id
        tr.set_result(fix_id, "passed", "fixed")
        await _await_inflight("s-1")
        assert len(tr.submissions) == 3
        assert "rocket-confirm" in tr.submissions[2]["content"]

        confirm_id = tr.last_task_id
        beads.gate("s-1", "PASS")
        tr.set_result(confirm_id, "passed", "looks good")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:pr"}

    @pytest.mark.asyncio
    async def test_gate_fail_loops_back_to_fix_not_verify(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:verification"}

        verify_id = await board_engine.run_job(tr, str(tmp_path), "s-1")
        tr.set_result(verify_id, "passed", "found issues")
        await _await_inflight("s-1")
        fix_id = tr.last_task_id
        tr.set_result(fix_id, "passed", "fixed")
        await _await_inflight("s-1")
        confirm_id = tr.last_task_id

        beads.gate("s-1", "FAIL", "still broken")
        tr.set_result(confirm_id, "passed", "not quite")
        await _await_inflight("s-1")

        assert beads.labels["s-1"] == {"phase:verification"}  # unchanged
        assert len(tr.submissions) == 4
        assert "rocket-fix" in tr.submissions[3]["content"]  # looped to fix, not verify
        data = board_state.load_history(str(tmp_path), "s-1")
        assert data["current_run"]["iteration"] == 1


class TestReviewCompletion:
    @pytest.mark.asyncio
    async def test_complete_review_runs_retro_then_advances_to_done(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:review"}
        task_id = await board_engine.complete_review(tr, str(tmp_path), "s-1")
        assert "rocket-retro" in tr.submissions[0]["content"]
        tr.set_result(task_id, "passed", "retro done")
        await _await_inflight("s-1")
        assert beads.labels["s-1"] == {"phase:done"}

    @pytest.mark.asyncio
    async def test_complete_review_wrong_phase_raises(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:pr"}
        with pytest.raises(board_engine.BoardError):
            await board_engine.complete_review(tr, str(tmp_path), "s-1")

    @pytest.mark.asyncio
    async def test_failed_retro_leaves_story_in_review(self, tmp_path, beads):
        tr = FakeTaskRunner()
        beads.labels["s-1"] = {"phase:review"}
        task_id = await board_engine.complete_review(tr, str(tmp_path), "s-1")
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
        task = PHASE_BY_KEY["grooming"].tasks[0]
        task_id = await board_engine._submit(runner, "/proj", "s-1", "grooming", task)
        assert task_id == "task-x"
        assert runner.spec_path is not None
        assert os.path.exists(runner.spec_path)
        with open(runner.spec_path, encoding="utf-8") as f:
            assert "rocket-ideate" in f.read()
