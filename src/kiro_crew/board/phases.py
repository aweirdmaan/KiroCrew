"""The board's columns, matched 1:1 to the REAL pipeline in crew-rocket's
``scripts/rocket-dag.sh`` and ``.kiro/workflows/*.yaml`` - not a reinvention
of it. Those files are never read or modified by this engine (it renders
its own throwaway YAML per task - see ``engine.py``); this module exists so
the two never drift apart in SHAPE either. Three facts drove this layout,
each confirmed by reading the canonical files directly rather than assumed:

1. ``.kiro/workflows/rocket-plan.yaml`` already runs ``ideate`` then ``plan``
   as two dependent agents in ONE Task Runner submission - "Planning" merges
   them the same way, not as two separate board columns.
2. ``rocket-dag.sh``'s ``cmd_implement`` runs confirm-plan, approval-check,
   the implement loop, verify, fix, confirm, pr, and retro as one unattended
   shell function, each step gated on the previous step's own
   ``GATE: PASS`` beads comment where one exists - "Implementation" merges
   all eight the same way.
3. ``cmd_implement`` has NO per-step resume state: every rerun (e.g. after a
   gate failure) restarts the whole function from confirm-plan. It relies on
   each gated skill being safe to re-invoke - confirm-plan's own SKILL.md
   says so explicitly ("This step is rerun-safe"). ``run_job`` below copies
   that: it always (re)starts a phase at its first task, never partway
   through. That is what makes this idempotent - a human answering the
   OPEN QUESTIONS and clicking "Run next job" again just works, the same
   way rerunning ``rocket-dag.sh implement <epic>`` by hand always has.

``rocket-dag.sh`` itself runs retro automatically, right after `pr`, before
any human has reviewed the MR - the human-facing post-review step is
``rocket-harvest`` (it needs the MR/PR URL, not just the epic id), which is
the one skill this engine reserves for the manual Review -> Done action
(``REVIEW_COMPLETION_TASK`` below), matching ``cmd_harvest``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    """One Task Runner run: a crew-rocket skill invoked for a crew member."""

    key: str
    skill: str
    agent: str  # crew.yaml member key: meowth | jessie | james
    # After this task passes, a trailing "GATE: PASS" beads comment must
    # already be on the issue before the job continues - to the next task,
    # or to advance the phase if this is the last one. On "GATE: FAIL" (or
    # no gate comment at all), the job stops here: visible to a human as
    # needing attention, nothing further runs until they act and click "Run
    # next job" again (which restarts this phase from its first task - see
    # the module docstring on why that is correct, not wasteful).
    gate: bool = False
    # "until_all_tasks_complete": re-run this exact task (same task_key,
    # next iteration) until its result contains that literal string
    # (rocket-implement's own convention, matching rocket-dag.sh's loop),
    # up to loop_cap attempts, then stop and leave it for a human.
    loop: str | None = None
    loop_cap: int = 1


@dataclass(frozen=True)
class Phase:
    """One board column."""

    key: str
    label: str
    tasks: tuple[Task, ...] = ()
    # Purely a UI/manual-wait marker: no task runs automatically via "Run
    # next job" (the engine exposes no run entrypoint for these). Advancing
    # out of one is an explicit human action - see ENGINE.md / handlers/
    # board.py's complete_review for Review -> Done, the only one today.
    manual: bool = False


PHASES: tuple[Phase, ...] = (
    Phase(
        key="planning",
        label="Planning",
        tasks=(
            Task(key="ideate", skill="rocket-ideate", agent="meowth"),
            Task(key="plan", skill="rocket-plan", agent="meowth"),
        ),
        # No gate: rocket-plan ends in OPEN QUESTIONS, answered as a beads
        # comment by a human. Nothing here checks for that answer - the
        # very first task of Implementation (confirm-plan) does, exactly
        # like rocket-dag.sh's own cmd_plan/cmd_implement split.
    ),
    Phase(
        key="implementation",
        label="Implementation",
        tasks=(
            Task(key="confirm_plan", skill="rocket-confirm-plan", agent="meowth", gate=True),
            Task(key="approval_check", skill="rocket-approval-check", agent="meowth", gate=True),
            Task(key="implement", skill="rocket-implement", agent="james",
                 loop="until_all_tasks_complete", loop_cap=25),
            Task(key="verify", skill="rocket-verify", agent="jessie"),
            Task(key="fix", skill="rocket-fix", agent="james"),
            Task(key="confirm", skill="rocket-confirm", agent="jessie", gate=True),
            Task(key="pr", skill="rocket-pr", agent="james"),
            Task(key="retro", skill="rocket-retro", agent="meowth"),
        ),
    ),
    Phase(key="review", label="Review", manual=True),
    Phase(key="done", label="Done", manual=True),
)

PHASE_BY_KEY: dict[str, Phase] = {p.key: p for p in PHASES}
PHASE_ORDER: tuple[str, ...] = tuple(p.key for p in PHASES)

# Not a board column - the one-off task the engine runs when a human marks a
# story reviewed (Review -> Done), matching rocket-dag.sh's separate
# `cmd_harvest` (run by a human after reading the MR, with its URL - unlike
# every other task here, this one needs an argument beyond the story id).
REVIEW_COMPLETION_TASK = Task(key="harvest", skill="rocket-harvest", agent="james")


def next_phase_key(current: str | None) -> str | None:
    """The phase after *current* (``None`` meaning "backlog", before any
    phase has started), or ``None`` if *current* is the last phase."""
    if current is None:
        return PHASE_ORDER[0]
    try:
        idx = PHASE_ORDER.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(PHASE_ORDER):
        return None
    return PHASE_ORDER[idx + 1]
