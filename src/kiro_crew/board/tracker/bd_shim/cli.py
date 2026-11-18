"""Entry point for the ``bd``-compatible shim - see the package docstring
(``kiro_crew.board.tracker.bd_shim.__init__``) for the full rationale.

``main()`` resolves the current working directory to a configured board
project (reusing ``tracker.config``'s own config-file reader directly, not
re-parsing it); if that project is Jira-backed, each supported subcommand is
dispatched to ``dispatch_jira.py``. Otherwise (no matching project, or a
``"beads"`` one) this execs straight through to the real ``bd`` binary,
found by walking PATH and skipping this shim's own directory - never
``shutil.which``, which would just find this shim again if it is first on
PATH (the whole point of installing it as ``bd``).
"""

from __future__ import annotations

import os
import sys

from kiro_crew.board.tracker import config as tracker_config
from kiro_crew.board.tracker.types import ProjectConfig


def _find_real_bd() -> str | None:
    """The first executable literally named ``bd`` on PATH, skipping the
    directory that holds this shim's own ``sys.argv[0]`` (by realpath, so a
    symlinked shim is skipped by its target directory too)."""
    self_dir = os.path.dirname(os.path.realpath(sys.argv[0])) if sys.argv else ""
    seen_self = False
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        entry_real = os.path.realpath(entry)
        if entry_real == self_dir or entry == self_dir:
            seen_self = True
            continue
        candidate = os.path.join(entry, "bd")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # self_dir may not have been on PATH at all (e.g. invoked by absolute
    # path) - in that case every PATH entry was already a candidate above,
    # so nothing further to retry. seen_self is only tracked for clarity.
    del seen_self
    return None


def _resolve_project_config(cwd: str) -> ProjectConfig | None:
    """Exact cwd match first, else the configured project whose path is the
    longest matching ancestor directory of cwd (a skill script may ``cd``
    into a subdirectory of the checkout) - see package docstring."""
    configs = tracker_config._read_project_configs_sync()
    best: ProjectConfig | None = None
    best_len = -1
    cwd_real = os.path.realpath(cwd)
    for cfg in configs:
        cfg_real = os.path.realpath(cfg.path)
        if cfg_real == cwd_real:
            return cfg
        prefix = cfg_real.rstrip(os.sep) + os.sep
        if cwd_real.startswith(prefix) and len(cfg_real) > best_len:
            best = cfg
            best_len = len(cfg_real)
    return best


def _exec_real_bd(argv: list[str]) -> None:
    real_bd = _find_real_bd()
    if real_bd is None:
        print(
            "bd-shim: no beads binary found on PATH beyond this shim - cannot "
            "run in a beads-backed project without a copy of the real `bd` "
            "CLI installed",
            file=sys.stderr,
        )
        sys.exit(1)
    os.execv(real_bd, [real_bd, *argv])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: bd <command> [args...]", file=sys.stderr)
        return 2

    config = _resolve_project_config(os.getcwd())
    if config is None or config.type != "jira":
        _exec_real_bd(args)  # never returns on success
        return 1  # pragma: no cover - only reached if execv itself fails

    from kiro_crew.board.tracker.bd_shim import dispatch_jira

    subcommand, rest = args[0], args[1:]
    return dispatch_jira.dispatch(subcommand, rest, config)


if __name__ == "__main__":
    sys.exit(main())
