"""Plain-text rendering helpers for the ``bd`` shim's Jira-backed dispatch.

Each function here mimics the shape of real beads' own plain-text output
closely enough that the ``grep``/``sed`` patterns crew-rocket's skill
scripts already run against ``bd`` output keep matching. The formats below
were confirmed by running a real ``bd`` binary (``/opt/homebrew/bin/bd``,
beads) against a real project (``crew-rocket-4da`` and others in
``~/.kiro/crew/apps/board/data/config.json``'s configured
``/Users/amaans/work/xplore/crew-rocket`` checkout) and capturing its exact
output - not guessed. In particular:

- ``format_comments``: confirmed byte-for-byte against ``bd comments <id>``
  (empty case: ``"No comments on <id>\\n"``; non-empty case: a leading blank
  line, ``"Comments on <id>:"``, a blank line, then one block per comment -
  ``"[<author>] at <YYYY-MM-DD HH:MM>"`` followed by every line of the
  comment body indented by exactly two spaces, followed by a blank line).
  This indentation means crew-rocket's own ``grep -A 999 '^FINDINGS'``
  pattern (anchored with no leading whitespace) will not match a real
  ``FINDINGS`` comment rendered this way either - that is a pre-existing
  property of real beads' own output, faithfully reproduced here, not a bug
  introduced by this shim. ``grep -E '^\\s*GATE:'`` (used by
  ``rocket-gate-check.sh``) does tolerate the indentation and keeps working.
- ``format_show``/``format_list``/``format_ready``: based on real ``bd
  show``/``bd list``/``bd ready`` output shapes observed the same way, but
  not grepped/sed structurally anywhere in crew-rocket's scripts (only read
  by the LLM agent running the skill) - so these are close approximations
  of the real layout (glyphs, section headers, field order) rather than
  byte-exact reproductions. Exact byte-for-byte fidelity was not required
  for these three, and there was no practical way to observe every possible
  field combination exhaustively.
"""

from __future__ import annotations

from typing import Any

_STATUS_GLYPH = {
    "open": "○",
    "in_progress": "◐",
    "blocked": "●",
    "closed": "✓",
    "deferred": "❄",
}


def _glyph(status: str) -> str:
    return _STATUS_GLYPH.get(status, "○")


def _priority_str(priority: int | None) -> str:
    if priority is None:
        return ""
    return f"● P{priority}"


def format_comments(story_id: str, comments: list[dict[str, Any]]) -> str:
    """Mirrors real ``bd comments <id>`` - see module docstring."""
    if not comments:
        return f"No comments on {story_id}\n"
    parts: list[str] = ["", f"Comments on {story_id}:", ""]
    for c in comments:
        author = str(c.get("author") or "")
        created = str(c.get("created_at") or "")
        parts.append(f"[{author}] at {created}")
        text = str(c.get("text") or "")
        lines = text.splitlines() or [""]
        for line in lines:
            parts.append(f"  {line}")
        parts.append("")
    return "\n".join(parts) + "\n"


def format_show(issue: dict[str, Any], children: list[dict[str, Any]] | None = None) -> str:
    """Approximates real ``bd show <id>`` for one issue - see module
    docstring for the fidelity note on this helper."""
    status = str(issue.get("status") or "")
    issue_type = str(issue.get("issue_type") or "")
    glyph = _glyph(status)
    title = str(issue.get("title") or "")
    priority = issue.get("priority")
    lines: list[str] = []
    type_tag = " [EPIC]" if issue_type.lower() == "epic" else ""
    lines.append(f"{glyph} {issue['id']}{type_tag} · {title}   [{_priority_str(priority)} · {status.upper()}]")
    owner = str(issue.get("owner") or "")
    owner_line = f"Owner: {owner} · Type: {issue_type}" if owner else f"Type: {issue_type}"
    lines.append(owner_line)
    created = str(issue.get("created_at") or "")[:10]
    updated = str(issue.get("updated_at") or "")[:10]
    due = issue.get("due_date")
    created_line = f"Created: {created} · Updated: {updated}"
    if due:
        created_line += f" · Due: {due}"
    lines.append(created_line)
    lines.append("")
    lines.append("DESCRIPTION")
    lines.append(str(issue.get("description") or ""))
    if children:
        lines.append("")
        lines.append("CHILDREN")
        for child in children:
            lines.append(
                f"  ↳ {_glyph(str(child.get('status') or ''))} {child.get('id')}: "
                f"{child.get('title', '')} {_priority_str(child.get('priority'))}"
            )
    return "\n".join(lines) + "\n"


def format_list(rows: list[dict[str, Any]], *, header: str | None = None) -> str:
    """Approximates real ``bd list`` - a flat line per issue (real bd draws
    a parent/child tree; this shim does not reconstruct that nesting, since
    nothing greps/seds this output structurally - see module docstring)."""
    lines: list[str] = []
    if header:
        lines.append(header)
    for row in rows:
        status = str(row.get("status") or "")
        issue_type = str(row.get("issue_type") or "")
        glyph = _glyph(status)
        lines.append(
            f"{glyph} {row.get('id')} {_priority_str(row.get('priority'))} "
            f"[{issue_type}] {row.get('title', '')}"
        )
    lines.append("")
    lines.append("-" * 80)
    lines.append(f"Total: {len(rows)} issues")
    return "\n".join(lines) + "\n"


def format_ready(rows: list[dict[str, Any]]) -> str:
    """Approximates real ``bd ready``."""
    lines: list[str] = []
    for row in rows:
        glyph = _glyph(str(row.get("status") or ""))
        epic_title = row.get("epic_title") or ""
        suffix = f" ← {epic_title}" if epic_title else ""
        lines.append(
            f"{glyph} {row.get('id')} {_priority_str(row.get('priority'))} {row.get('title', '')}{suffix}"
        )
    lines.append("")
    lines.append("-" * 80)
    lines.append(f"Ready: {len(rows)} issues with no active blockers")
    return "\n".join(lines) + "\n"
