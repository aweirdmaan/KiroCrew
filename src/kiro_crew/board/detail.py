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
    }


async def add_comment(project_path: str, story_id: str, text: str) -> bool:
    if not text.strip():
        return False
    result = await run_bd(project_path, "comments", "add", story_id, text)
    return result is not None
