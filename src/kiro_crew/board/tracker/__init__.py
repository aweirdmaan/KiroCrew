from __future__ import annotations

from kiro_crew.board.tracker import beads_client, jira_client
from kiro_crew.board.tracker.types import ProjectConfig


def client_for(config: ProjectConfig):
    """Dispatch to the right tracker client module, mirroring
    issue_radar/backend/provider.py's client_for(RepoKey)."""
    if config.type == "jira":
        return jira_client
    return beads_client
