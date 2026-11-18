"""Shared dataclasses for the board tracker abstraction - no I/O here, just
the shapes every tracker client (beads today, Jira next) produces/consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProjectConfig:
    """A configured board project: a local path (always required - even a
    Jira-backed project still needs a real git checkout for the pipeline to
    implement code in) plus which tracker owns its issue data."""
    path: str
    type: str = "beads"  # "beads" | "jira"
    jira_host: str | None = None
    jira_project_key: str | None = None
    story_points_field: str | None = None
    dependency_link_type: str = "Blocks"


@dataclass
class StorySummary:
    id: str
    title: str
    status: str
    epic_id: str
    epic_title: str
    project_path: str
    phase: str | None  # None = backlog
    phase_label: str
    current_run: dict | None
    priority: int | None
    owner: str
    pending_open_questions: bool
    recommendation: str | None
    start_date: str | None
    due_date: str | None
    rank: float | None
    story_points: float | None
    depends_on: list[str] = field(default_factory=list)
