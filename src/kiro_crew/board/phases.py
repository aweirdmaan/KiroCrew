"""The 9-column pipeline a board story moves through.

Each :class:`Phase` is a board column; each :class:`Task` within it is one
Task Runner run (one crew-rocket skill, invoked against the story's own beads
id via a minimal on-the-fly YAML template -- see ``engine.py``). The existing
``.kiro/workflows/*.yaml`` files and ``scripts/rocket-dag.sh`` are never read
or modified: this module only reuses the *skills* they also point at.

Only one phase (``verification``) has more than one task: verify always runs,
then fix, then confirm; confirm's result is gated on a trailing
``GATE: PASS`` beads comment (the same convention ``rocket-gate-check.sh``
reads), and on ``GATE: FAIL`` the engine loops back to ``fix`` (not back to
``verify``) up to ``loop_cap`` times. This is the one place the Job/Task
composition is more than a formality, matching rocket-dag.sh's own sequence
(verify -> fix -> confirm) with one addition: rocket-dag.sh gates once and
stops on failure, requiring a human to re-invoke it; this loops automatically.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    """One Task Runner run: a crew-rocket skill invoked for a crew member."""

    key: str
    skill: str
    agent: str  # crew.yaml member key: meowth | jessie | james


@dataclass(frozen=True)
class Phase:
    """One board column."""

    key: str
    label: str
    tasks: tuple[Task, ...] = ()
    # Whether the LAST task's result must carry a trailing "GATE: PASS" beads
    # comment to advance. On "GATE: FAIL" (or no GATE comment at all), the
    # story stays in this phase - visible to a human as needing attention -
    # and running the phase's job again re-checks the fresh comment.
    gate: bool = False
    # None | "until_all_tasks_complete" (implementation: re-run the single
    # task until its result contains that literal string, same as
    # rocket-dag.sh) | "until_gate_pass" (verification: loop fix->confirm).
    loop: str | None = None
    loop_cap: int = 1
    # Task key to resume at when looping (verification loops back to "fix",
    # not "verify" - matches rocket-dag.sh never re-running verify either).
    loop_back_to: str | None = None
    # Purely a UI/manual-wait marker: no task runs automatically. The engine
    # exposes no "run" entrypoint for these; advancing out of them is the
    # one explicit human action (see ENGINE.md / handlers/board.py's
    # complete_review).
    manual: bool = False


PHASES: tuple[Phase, ...] = (
    Phase(
        key="grooming",
        label="Grooming",
        tasks=(Task(key="ideate", skill="rocket-ideate", agent="meowth"),),
    ),
    Phase(
        key="planning",
        label="Planning",
        tasks=(Task(key="plan", skill="rocket-plan", agent="meowth"),),
        # No gate: rocket-plan ends in OPEN QUESTIONS, answered as a beads
        # comment by a human before Plan Review's job is worth running - the
        # gate lives one phase later, on confirm-plan's own result.
    ),
    Phase(
        key="plan_review",
        label="Plan Review",
        tasks=(Task(key="confirm_plan", skill="rocket-confirm-plan", agent="meowth"),),
        gate=True,
    ),
    Phase(
        key="approval",
        label="Approval",
        tasks=(Task(key="approval_check", skill="rocket-approval-check", agent="meowth"),),
        gate=True,
    ),
    Phase(
        key="implementation",
        label="Implementation",
        tasks=(Task(key="implement", skill="rocket-implement", agent="james"),),
        loop="until_all_tasks_complete",
        loop_cap=25,  # same cap rocket-dag.sh uses
    ),
    Phase(
        key="verification",
        label="Verification",
        tasks=(
            Task(key="verify", skill="rocket-verify", agent="jessie"),
            Task(key="fix", skill="rocket-fix", agent="james"),
            Task(key="confirm", skill="rocket-confirm", agent="jessie"),
        ),
        gate=True,
        loop="until_gate_pass",
        loop_cap=10,
        loop_back_to="fix",
    ),
    Phase(
        key="pr",
        label="PR",
        tasks=(Task(key="pr", skill="rocket-pr", agent="james"),),
    ),
    Phase(key="review", label="Review", manual=True),
    Phase(key="done", label="Done", manual=True),
)

PHASE_BY_KEY: dict[str, Phase] = {p.key: p for p in PHASES}
PHASE_ORDER: tuple[str, ...] = tuple(p.key for p in PHASES)

# Not a board column - the one-off task the engine runs when a human marks a
# story reviewed (Review -> Done). Separate from PHASES because it has no
# column of its own and never runs via the normal "run next job" path.
REVIEW_COMPLETION_TASK = Task(key="retro", skill="rocket-retro", agent="meowth")


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
