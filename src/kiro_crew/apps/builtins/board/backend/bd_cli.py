"""Shared ``bd`` (beads) CLI subprocess helper.

Extracted from ``knowledge/beads_ingest.py`` so ``board/`` can issue the same
kind of ``bd <args> --json`` call (label add/remove, comment add, list, show)
without duplicating the subprocess/timeout/error-handling logic. Both callers
want the exact same failure contract: None on any failure, never an
exception, so a transient `bd` hiccup degrades to "nothing to do this round"
rather than escalating into a fatal error for whatever polling loop called it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

logger = logging.getLogger(__name__)

# Wall-clock budget for one `bd` invocation. Even `bd show` on a large issue
# (many comments) is a local sqlite/dolt read, not a network call -- this is
# a "the binary hung" guard, not a tuned timeout for real data volume.
BD_TIMEOUT_SECS = 30


async def run_bd(project_path: str, *args: str) -> object | None:
    """Run ``bd <args> --json`` in ``project_path``; return parsed stdout, or
    None on any failure (missing binary, non-zero exit, timeout, bad JSON).

    ``--json`` is appended uniformly, including for mutating commands
    (``label add``, ``comments add``) - confirmed those also emit a
    structured JSON result (e.g. ``[{"issue_id": ..., "label": ...,
    "status": "added"}]``) rather than only plain/human text, so callers get
    the same parse-or-None contract everywhere instead of a special case for
    writes.
    """
    bd = shutil.which("bd")
    if not bd:
        logger.debug("bd not on PATH, skipping %s in %s", args, project_path)
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            bd, *args, "--json",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BD_TIMEOUT_SECS)
    except (OSError, asyncio.TimeoutError):
        logger.warning("bd %s failed to run in %s", " ".join(args), project_path, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.warning(
            "bd %s exited %d in %s: %s",
            " ".join(args), proc.returncode, project_path,
            stderr.decode(errors="replace")[:500],
        )
        return None
    try:
        return json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        logger.warning("bd %s returned non-JSON stdout in %s", " ".join(args), project_path)
        return None
