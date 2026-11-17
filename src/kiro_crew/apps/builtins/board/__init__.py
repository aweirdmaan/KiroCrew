"""Timeline — Jira-style board over beads specs, driving crew-rocket's pipeline.

Architecture — in-process builtin, like ``aws_control`` and ``issue_radar``.
"""

# Required re-export: dashboard/server.py's startup route registration imports
# the PACKAGE and checks hasattr(_mod, "register_routes") — the same convention
# every in-process builtin follows (the call site is the
# `for _builtin_name in BUILTIN_NAMES` loop in dashboard/routes/system.py).
# Routes are registered at STARTUP, not on enable, which is why every handler
# carries its own enabled check.
from kiro_crew.apps.builtins.board.backend.routes import (  # noqa: F401
    register_routes,
)
