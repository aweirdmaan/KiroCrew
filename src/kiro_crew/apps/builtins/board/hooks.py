"""Lifecycle hooks — the autorun background loop.

One background task, started on enable, that ticks every 15s and advances
every board story that is not blocked on a human (see
``kiro_crew.board.autorun`` for the full "what counts as blocked" rule).
Same shape as every other builtin app's background worker (see
``aws_control/hooks.py``'s nightly backup loop): a module-level task handle,
idempotent start, cancel-on-disable.

Manually disabled by default (see _AUTORUN_ENABLED below) - a bug in an
earlier version of the unblock logic caused an unattended restart storm that
ran for over 90 minutes and cascaded into provider-side throttling, all of it
billable with zero human involvement. The root cause is fixed and covered by
a regression test (kiro_crew.board.autorun's own test suite), but autorun
stays opt-in until the operator is ready to re-enable it - flip the flag
below, nothing else needs to change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.builtins.board.backend.routes import _project_paths
from kiro_crew.board.autorun import autorun_loop

logger = logging.getLogger(__name__)

_AUTORUN_ENABLED = False

_task: asyncio.Task | None = None


async def on_startup(ctx: Any) -> None:
    """Start the autorun loop, idempotent across enable/disable cycles.

    NOTE (unverified as of this port): ``AppContext.http_app`` is only
    granted when ``context.manifest_declares_routes`` sees a
    ``backend.hooks.routes`` key, but every real manifest (this one
    included, matching issue_radar/aws_control) declares routes at
    ``backend.routes`` - a sibling of ``hooks``, not nested inside it. No
    builtin app currently reads ``ctx.http_app`` in practice, so this
    mismatch has never been exercised. Harmless today since autorun stays
    disabled regardless (``_AUTORUN_ENABLED`` below) - `_get_task_runner`
    degrades to ``None`` if the grant doesn't land - but re-verify this
    before flipping that flag on.
    """
    global _task
    if not _AUTORUN_ENABLED:
        return
    if _task is not None and not _task.done():
        return

    def _get_task_runner() -> object | None:
        http_app = ctx.http_app
        if http_app is None:
            return None
        state = http_app.get("state")
        return getattr(state, "task_runner", None) if state is not None else None

    _task = asyncio.get_running_loop().create_task(autorun_loop(_get_task_runner, _project_paths))


async def on_shutdown(ctx: Any) -> None:  # noqa: ARG001 - kept for the hook ABI
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
