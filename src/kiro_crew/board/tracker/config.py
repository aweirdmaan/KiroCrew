from __future__ import annotations

import asyncio
import json
import logging

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.board.tracker.types import ProjectConfig

logger = logging.getLogger(__name__)

_APP_NAME = "board"
_VALID_TYPES = {"beads", "jira"}


def _coerce_entry(raw: object) -> ProjectConfig | None:
    if isinstance(raw, str):
        return ProjectConfig(path=raw, type="beads") if raw else None
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        return None
    tracker_type = raw.get("type")
    if tracker_type not in _VALID_TYPES:
        tracker_type = "beads"
    jira_host = raw.get("jira_host")
    jira_project_key = raw.get("jira_project_key")
    story_points_field = raw.get("story_points_field")
    dependency_link_type = raw.get("dependency_link_type")
    return ProjectConfig(
        path=path,
        type=tracker_type,
        jira_host=jira_host if isinstance(jira_host, str) else None,
        jira_project_key=jira_project_key if isinstance(jira_project_key, str) else None,
        story_points_field=story_points_field if isinstance(story_points_field, str) else None,
        dependency_link_type=dependency_link_type if isinstance(dependency_link_type, str) else "Blocks",
    )


def _read_project_configs_sync() -> list[ProjectConfig]:
    config_path = app_data_dir(_APP_NAME) / "config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("board: failed to read app config at %s", config_path, exc_info=True)
        return []
    if not isinstance(data, dict):
        return []
    raw_entries = data.get("project_paths")
    if not isinstance(raw_entries, list):
        return []
    configs = [_coerce_entry(e) for e in raw_entries]
    return [c for c in configs if c is not None]


async def project_configs() -> list[ProjectConfig]:
    """Every configured project, read off disk the same way
    ``handle_app_config``'s own GET does (off the event loop)."""
    return await asyncio.to_thread(_read_project_configs_sync)


async def config_for_path(project_path: str) -> ProjectConfig:
    """Resolve a bare path to its full config. Falls back to a plain beads
    config for a path not found in project_paths (keeps every existing
    caller - which only ever had a path string - working unchanged)."""
    for cfg in await project_configs():
        if cfg.path == project_path:
            return cfg
    return ProjectConfig(path=project_path, type="beads")
