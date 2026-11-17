"""Story Board: a beads-story pipeline on top of Task Runner.

Deliberately named ``board``, not ``workflow`` -- ``kiro_crew.workflows`` is
an unrelated, pre-existing execution engine (Python ctx-scripts run via
agent_exec/agent_pool, behind the ``Workflow`` tool and the apps/workflows
UI), not what runs crew-rocket's ``.kiro/workflows/*.yaml`` templates (that's
``taskrunner.py``). This package stays on Task Runner, the same system
``scripts/rocket-dag.sh`` already drives, and does not import or extend
``kiro_crew.workflows`` at all.

See ``src/kiro_crew/board/phases.py`` for the 9-phase pipeline definition,
``state.py`` for how a story's current phase and run history are tracked,
and ``engine.py`` for the orchestrator that submits jobs and advances stories.
"""
