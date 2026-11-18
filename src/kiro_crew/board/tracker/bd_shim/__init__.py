"""``bd``-compatible CLI shim for Jira-backed board projects.

crew-rocket's rocket-* skills/scripts (a separate repo) bare-invoke the real
``bd`` (beads) CLI, resolved via PATH, inside the pipeline job's working
directory. Board's own API calls already support Jira-backed projects (see
``kiro_crew.board.tracker.jira_client``), but the pipeline automation itself
is crew-rocket's skill scripts - not KiroCrew's code - so it cannot be
changed here.

This package is a drop-in shim for the exact ``bd`` subcommand surface those
scripts actually use (see ``dispatch_jira.py`` for the full list). When the
current working directory resolves to a configured board project whose
``type`` is ``"jira"``, each subcommand is translated to the equivalent Jira
REST call. Otherwise - no matching project, or a ``"beads"`` project - this
shim execs straight through to the real ``bd`` binary, found by walking PATH
and skipping the directory this shim itself lives in (see
``cli._find_real_bd``), so it is safe to install this shim AS "bd" on PATH
without infinite recursion.

Operator setup (this code cannot force it): this package registers its own
console script as ``kirocrew-bd-shim`` (see ``pyproject.toml``), deliberately
NOT named ``bd`` - installing it as ``bd`` directly would shadow every
other environment's real beads binary for anyone who installs this package.
To make Jira-backed pipeline runs work, an operator must put a copy/symlink
of ``kirocrew-bd-shim`` named exactly ``bd`` earlier on PATH than the real
beads binary, for whatever shell/session environment pipeline jobs inherit
(board_engine's jobs run with the gateway process's own inherited
environment - there is no per-job PATH override). This is a one-time
environment setup step.

Known limitation - ``bd remember``: beads' cross-session memory concept has
no Jira equivalent. When dispatched to a Jira-backed project, ``bd remember``
is handled locally-only: the text is appended to a sidecar JSONL file under
the project's own path (``.kiro/crew/jira-memory.jsonl``), never synced to
Jira. See ``dispatch_jira.cmd_remember`` for details.
"""

from __future__ import annotations
