"""Full beads issue detail (description, comments, metadata) for a board
story's detail modal - the Jira-style view the board's "Run next job"
card expands into: everything `bd show <id>` carries, plus the ability to
post a new comment through the same channel the rocket-* skills already use
for gates/decisions (`GATE: PASS`, `APPROVED`, etc.), so a human can answer
OPEN QUESTIONS or approve a plan from the board instead of a terminal.

Text is redacted the same way the Knowledge Library's beads sync redacts
ingested issue content (credentials/exfiltration URLs) before it is returned
- a beads comment can legitimately contain a pasted secret during incident
discussion, and echoing it back verbatim in the dashboard is the same leak
the Knowledge Library citation would be.
"""

from __future__ import annotations

from kiro_crew.bd_cli import run_bd
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _redact(text: object) -> str:
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


async def get_story_detail(project_path: str, story_id: str) -> dict | None:
    result = await run_bd(project_path, "show", "--id", story_id)
    if not isinstance(result, list) or not result:
        return None
    issue = result[0]
    if not isinstance(issue, dict):
        return None
    comments = []
    for c in issue.get("comments") or []:
        if not isinstance(c, dict):
            continue
        comments.append({
            "id": c.get("id", ""),
            "author": c.get("author", ""),
            "text": _redact(c.get("text", "")),
            "created_at": c.get("created_at", ""),
        })
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    depends_on = [
        dep["depends_on_id"] for dep in (issue.get("dependencies") or [])
        if isinstance(dep, dict) and dep.get("type") != "parent-child" and dep.get("depends_on_id")
    ]
    rank = metadata.get("timeline_rank")
    try:
        rank = float(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None
    return {
        "id": issue.get("id", story_id),
        "title": issue.get("title", ""),
        "description": _redact(issue.get("description", "")),
        "status": issue.get("status", ""),
        "issue_type": issue.get("issue_type", ""),
        "priority": issue.get("priority"),
        "owner": issue.get("owner", ""),
        "created_at": issue.get("created_at", ""),
        "updated_at": issue.get("updated_at", ""),
        "comments": comments,
        "start_date": metadata.get("start_date"),
        "due_date": issue.get("due_at"),
        "rank": rank,
        "depends_on": depends_on,
    }


async def add_comment(project_path: str, story_id: str, text: str) -> bool:
    if not text.strip():
        return False
    result = await run_bd(project_path, "comments", "add", story_id, text)
    return result is not None


async def update_story(
    project_path: str, story_id: str, *,
    title: str | None = None, description: str | None = None,
    start_date: str | None = None, due_date: str | None = None, rank: float | None = None,
) -> bool:
    """Edit the card itself via `bd update`. Unlike comments, beads issue
    fields are plain mutable columns - no embedded-mode restriction here
    (see add_comment's docstring/the board's comment "edit" for why that
    one has to work differently).

    start_date/rank ride on beads' free-form --set-metadata (beads has no
    native "start date" or manual-ordering concept); due_date is the native
    --due field. See stories.py's module comment for why."""
    args: list[str] = ["update", story_id]
    if title is not None:
        args += ["--title", title]
    if description is not None:
        args += ["--description", description]
    if due_date is not None:
        args += ["--due", due_date]
    if start_date is not None:
        args += ["--set-metadata", f"start_date={start_date}"]
    if rank is not None:
        args += ["--set-metadata", f"timeline_rank={rank}"]
    if len(args) == 2:
        return False
    result = await run_bd(project_path, *args)
    return result is not None


async def add_dependency(project_path: str, story_id: str, depends_on_id: str) -> bool:
    """story_id depends on (is blocked by) depends_on_id - `bd dep add
    <blocked> <blocker>` per `bd dep --help`."""
    if not depends_on_id.strip():
        return False
    result = await run_bd(project_path, "dep", "add", story_id, depends_on_id.strip())
    return result is not None


async def remove_dependency(project_path: str, story_id: str, depends_on_id: str) -> bool:
    result = await run_bd(project_path, "dep", "remove", story_id, depends_on_id)
    return result is not None
