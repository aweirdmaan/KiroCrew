"""Jira-backed implementations of the ``bd`` subcommand surface crew-rocket's
rocket-* skill scripts actually invoke (see package docstring for the full
list and rationale). Built directly on top of
``kiro_crew.board.tracker.jira_client``'s existing internals - its
``_request``/``_headers``/ADF helpers, etc. - reused verbatim rather than
re-implemented, since this is the same codebase, not a public API boundary.

Every function here takes a ``ProjectConfig`` (already resolved by
``cli.py`` to be Jira-backed) and the subcommand's raw argv (after the
subcommand word itself), does its own light flag parsing (see
``_split_args``), and returns a process exit code. Output goes to stdout
(success) or stderr (errors/notes), matching real ``bd``'s own split.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from kiro_crew.board.tracker import jira_client
from kiro_crew.board.tracker.bd_shim import formatting
from kiro_crew.board.tracker.types import ProjectConfig

# Jira issue type used for a beads "task" (a grape, parented to a story/epic)
# and for the top-level epic itself - mirrors jira_client._STORY_ISSUE_TYPES'
# own "Story"/"Task" convention plus the top-level "Epic" type that
# list_stories() never searches for directly (it finds epics only as the
# *parent* of a Story/Task) but `bd create --type=epic` has to create
# explicitly.
_TASK_ISSUE_TYPE = "Task"
_EPIC_ISSUE_TYPE = "Epic"

_BOOL_FLAGS = frozenset({"claim", "json", "reverse", "all", "stdin"})

_MEMORY_SIDECAR = ".kiro/crew/jira-memory.jsonl"


def _split_args(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Light ``--flag value`` / ``--flag=value`` / bare-positional parser -
    good enough for the fixed, known flag set this shim supports (see the
    per-subcommand handlers below); not a general CLI parser."""
    positionals: list[str] = []
    flags: dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            body = a[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                flags[k] = v
            elif body in _BOOL_FLAGS:
                flags[body] = True
            elif i + 1 < len(args):
                flags[body] = args[i + 1]
                i += 1
            else:
                flags[body] = True
        else:
            positionals.append(a)
        i += 1
    return positionals, flags


def _print_err(message: str) -> None:
    print(message, file=sys.stderr)


def _jira_date(value: object) -> str:
    """Jira's ISO timestamp -> beads' own "YYYY-MM-DD HH:MM" rendering
    (confirmed against a real ``bd comments`` run - see formatting.py's
    docstring). A best-effort slice, no timezone conversion."""
    text = str(value or "")
    if len(text) >= 16 and text[10] in ("T", " "):
        return f"{text[:10]} {text[11:16]}"
    return text


async def _fetch_issue(config: ProjectConfig, headers: dict[str, str], story_id: str, fields: list[str]) -> dict | None:
    url = jira_client._issue_url(config, story_id, fields=fields)
    try:
        status, data, _body = await jira_client._request("GET", url, headers)
    except jira_client.BoardTrackerError:
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    return data


def _issue_to_plain(data: dict, story_id: str) -> dict[str, Any]:
    fields_obj = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    issuetype = fields_obj.get("issuetype") if isinstance(fields_obj.get("issuetype"), dict) else {}
    status_obj = fields_obj.get("status") if isinstance(fields_obj.get("status"), dict) else {}
    priority_obj = fields_obj.get("priority") if isinstance(fields_obj.get("priority"), dict) else {}
    assignee = fields_obj.get("assignee") if isinstance(fields_obj.get("assignee"), dict) else {}
    return {
        "id": str(data.get("key") or story_id),
        "title": str(fields_obj.get("summary") or ""),
        "description": jira_client._redact(jira_client._adf_text(fields_obj.get("description"))),
        "status": str(status_obj.get("name") or ""),
        "issue_type": str(issuetype.get("name") or ""),
        "priority": jira_client._PRIORITY_NAME_TO_INT.get(str(priority_obj.get("name") or "").lower()),
        "owner": str(assignee.get("displayName") or assignee.get("name") or ""),
        "created_at": str(fields_obj.get("created") or ""),
        "updated_at": str(fields_obj.get("updated") or ""),
        "due_date": fields_obj.get("duedate"),
    }


async def _run_jql(config: ProjectConfig, headers: dict[str, str], jql: str, fields: list[str], limit: int = 0) -> list[dict]:
    issues: list[dict] = []
    next_token: str | None = None
    for _page in range(200):
        params = [("jql", jql), ("fields", ",".join(fields)), ("maxResults", "100")]
        if next_token:
            params.append(("nextPageToken", next_token))
        url = f"{jira_client._api_base(config)}/search/jql?{urlencode(params, quote_via=quote)}"
        try:
            status, data, _body = await jira_client._request("GET", url, headers)
        except jira_client.BoardTrackerError:
            break
        if status != 200 or not isinstance(data, dict):
            break
        page_issues = data.get("issues")
        if isinstance(page_issues, list):
            issues.extend(i for i in page_issues if isinstance(i, dict))
        if limit and len(issues) >= limit:
            issues = issues[:limit]
            break
        next_token = data.get("nextPageToken") or None
        if not next_token or data.get("isLast"):
            break
    return issues


async def cmd_show(args: list[str], config: ProjectConfig) -> int:
    positionals, _flags = _split_args(args)
    if not positionals:
        _print_err("usage: bd show <id>")
        return 2
    story_id = positionals[0]
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    fields = ["summary", "description", "status", "issuetype", "assignee", "priority", "created", "updated", "duedate"]
    data = await _fetch_issue(config, headers, story_id, fields)
    if data is None:
        _print_err(f"bd-shim: issue {story_id} not found")
        return 1
    issue = _issue_to_plain(data, story_id)
    children: list[dict] = []
    if issue["issue_type"].lower() == _EPIC_ISSUE_TYPE.lower():
        child_rows = await _run_jql(
            config, headers, f'parent = "{story_id}"',
            ["summary", "status", "priority"],
        )
        children = [_issue_to_plain(row, row.get("key", "")) for row in child_rows]
    print(formatting.format_show(issue, children), end="")
    return 0


async def cmd_list_parent(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    parent_id = flags.get("parent") or (positionals[0] if positionals else None)
    if not parent_id:
        _print_err("usage: bd list --parent <id>")
        return 2
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    rows = await _run_jql(config, headers, f'parent = "{parent_id}"', ["summary", "status", "priority", "issuetype"])
    issues = [_issue_to_plain(row, row.get("key", "")) for row in rows]
    print(formatting.format_list(issues), end="")
    return 0


async def cmd_list_all(args: list[str], config: ProjectConfig) -> int:
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    jql = f'project = "{config.jira_project_key}"'
    rows = await _run_jql(config, headers, jql, ["summary", "status", "priority", "issuetype"])
    issues = [_issue_to_plain(row, row.get("key", "")) for row in rows]
    print(formatting.format_list(issues), end="")
    return 0


async def cmd_list_epic_json(args: list[str], config: ProjectConfig) -> int:
    """``bd list --type epic --sort created --limit 1 --json`` -
    scripts/rocket-dag.sh:113 is the one place in crew-rocket that parses
    this structurally (``d[0]['id']``), so the output here must be a real
    JSON array on stdout, newest-first by default - ``--reverse`` flips to
    oldest-first (confirmed against a real ``bd`` install per that script's
    own comment; do not "fix" this to be the opposite)."""
    _positionals, flags = _split_args(args)
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    reverse = bool(flags.get("reverse"))
    order = "ASC" if reverse else "DESC"
    jql = f'project = "{config.jira_project_key}" AND issuetype = {_EPIC_ISSUE_TYPE} ORDER BY created {order}'
    limit = int(flags.get("limit") or 0) or 0
    rows = await _run_jql(
        config, headers, jql,
        ["summary", "status", "priority", "issuetype", "created", "updated"],
        limit=limit,
    )
    issues = [_issue_to_plain(row, row.get("key", "")) for row in rows]
    print(json.dumps(issues))
    return 0


async def cmd_ready(args: list[str], config: ProjectConfig) -> int:
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    jql = (
        f'project = "{config.jira_project_key}" AND issuetype = {_TASK_ISSUE_TYPE} '
        'AND statusCategory != Done'
    )
    rows = await _run_jql(
        config, headers, jql,
        ["summary", "status", "priority", "issuelinks", "parent"],
    )
    ready_rows = []
    for row in rows:
        fields_obj = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        blockers = fields_obj.get("issuelinks") or []
        blocked = False
        for link in blockers if isinstance(blockers, list) else []:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
            if str(link_type.get("name") or "") != config.dependency_link_type:
                continue
            blocker = link.get("inwardIssue")
            if not isinstance(blocker, dict):
                continue
            blocker_fields = blocker.get("fields") if isinstance(blocker.get("fields"), dict) else {}
            blocker_status = blocker_fields.get("status") if isinstance(blocker_fields.get("status"), dict) else {}
            category = blocker_status.get("statusCategory") if isinstance(blocker_status.get("statusCategory"), dict) else {}
            if str(category.get("key") or "") != "done":
                blocked = True
                break
        if blocked:
            continue
        parent = fields_obj.get("parent") if isinstance(fields_obj.get("parent"), dict) else {}
        parent_fields = parent.get("fields") if isinstance(parent.get("fields"), dict) else {}
        item = _issue_to_plain(row, row.get("key", ""))
        item["epic_title"] = str(parent_fields.get("summary") or "")
        ready_rows.append(item)
    print(formatting.format_ready(ready_rows), end="")
    return 0


async def cmd_search(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    query = flags.get("query") or (positionals[0] if positionals else "")
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    status_clause = "" if flags.get("status") == "all" else "AND statusCategory != Done"
    escaped = str(query).replace('"', '\\"')
    jql = f'project = "{config.jira_project_key}" AND summary ~ "{escaped}*" {status_clause}'.strip()
    rows = await _run_jql(config, headers, jql, ["summary", "status", "priority", "issuetype"])
    issues = [_issue_to_plain(row, row.get("key", "")) for row in rows]
    print(formatting.format_list(issues), end="")
    return 0


async def cmd_create(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    issue_type = str(flags.get("type") or "task")
    jira_type = _EPIC_ISSUE_TYPE if issue_type.lower() == "epic" else _TASK_ISSUE_TYPE
    title = flags.get("title") or (positionals[0] if positionals else "")
    description = flags.get("description")
    parent = flags.get("parent")
    if not title:
        _print_err("bd-shim: create requires --title (or a positional title)")
        return 2
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    fields: dict[str, Any] = {
        "project": {"key": config.jira_project_key},
        "issuetype": {"name": jira_type},
        "summary": title,
    }
    if description:
        fields["description"] = jira_client._adf_wrap_text(description)
    if parent:
        fields["parent"] = {"key": parent}
    url = f"{jira_client._api_base(config)}/issue"
    try:
        status, data, body = await jira_client._request("POST", url, headers, json_body={"fields": fields})
    except jira_client.BoardTrackerError:
        _print_err("bd-shim: could not reach Jira")
        return 1
    if not (200 <= status < 300) or not isinstance(data, dict):
        _print_err(f"bd-shim: create failed (HTTP {status}): {body}")
        return 1
    new_id = str(data.get("key") or "")
    print(f"Created {jira_type.lower()} {new_id}: {title}")
    return 0


async def cmd_update_claim(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    if not positionals or not flags.get("claim"):
        _print_err("usage: bd update <id> --claim")
        return 2
    story_id = positionals[0]
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    try:
        status, me, _body = await jira_client._request("GET", f"{jira_client._api_base(config)}/myself", headers)
    except jira_client.BoardTrackerError:
        _print_err("bd-shim: could not reach Jira")
        return 1
    fields: dict[str, Any] = {}
    if status == 200 and isinstance(me, dict) and me.get("accountId"):
        fields["assignee"] = {"accountId": me["accountId"]}
    url = jira_client._issue_url(config, story_id)
    try:
        put_status, _data, body = await jira_client._request("PUT", url, headers, json_body={"fields": fields})
    except jira_client.BoardTrackerError:
        _print_err("bd-shim: could not reach Jira")
        return 1
    if put_status not in (200, 204):
        _print_err(f"bd-shim: claim failed (HTTP {put_status}): {body}")
        return 1
    await _transition_to(config, headers, story_id, "in progress")
    return 0


async def _transition_to(config: ProjectConfig, headers: dict[str, str], story_id: str, name_contains: str) -> bool:
    url = f"{jira_client._issue_url(config, story_id)}/transitions"
    try:
        status, data, _body = await jira_client._request("GET", url, headers)
    except jira_client.BoardTrackerError:
        return False
    if status != 200 or not isinstance(data, dict):
        return False
    transitions = data.get("transitions") if isinstance(data.get("transitions"), list) else []
    match = None
    for t in transitions:
        if not isinstance(t, dict):
            continue
        if name_contains.lower() in str(t.get("name") or "").lower():
            match = t
            break
    if match is None:
        return False
    try:
        put_status, _data, _body = await jira_client._request(
            "POST", url, headers, json_body={"transition": {"id": match.get("id")}}
        )
    except jira_client.BoardTrackerError:
        return False
    return 200 <= put_status < 300


async def cmd_close(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    if not positionals:
        _print_err("usage: bd close <id> --reason=\"...\"")
        return 2
    story_id = positionals[0]
    reason = flags.get("reason")
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    if reason:
        await jira_client.add_comment(config, story_id, f"Close reason: {reason}")
    await _transition_to(config, headers, story_id, "done")
    return 0


async def cmd_comment_add(args: list[str], config: ProjectConfig) -> int:
    positionals, flags = _split_args(args)
    if not positionals:
        _print_err("usage: bd comment <id> \"<text>\"")
        return 2
    story_id = positionals[0]
    text = flags.get("file") and Path(str(flags["file"])).read_text(encoding="utf-8")
    if not text:
        text = " ".join(positionals[1:])
    if not text or not str(text).strip():
        if sys.stdin is not None and not sys.stdin.isatty():
            text = sys.stdin.read()
    ok = await jira_client.add_comment(config, story_id, str(text or ""))
    if not ok:
        _print_err(f"bd-shim: could not add comment to {story_id}")
        return 1
    return 0


async def cmd_comments(args: list[str], config: ProjectConfig) -> int:
    positionals, _flags = _split_args(args)
    if not positionals:
        _print_err("usage: bd comments <id>")
        return 2
    story_id = positionals[0]
    try:
        headers = await jira_client._headers(config)
    except ValueError as exc:
        _print_err(f"bd-shim: {exc}")
        return 1
    try:
        comments = await jira_client._issue_comments(config, headers, story_id)
    except jira_client.BoardTrackerError:
        comments = None
    if comments is None:
        _print_err(f"bd-shim: could not read comments on {story_id}")
        return 1
    rendered = [
        {**c, "created_at": _jira_date(c.get("created_at"))}
        for c in comments
    ]
    print(formatting.format_comments(story_id, rendered), end="")
    return 0


def cmd_remember(args: list[str], config: ProjectConfig) -> int:
    """No Jira equivalent for beads' cross-session ``bd remember`` - stores
    the insight locally only, under the project's own path, and says so on
    stderr (never stdout, so a script piping ``bd remember``'s stdout never
    sees this note mixed in). See the package docstring."""
    positionals, flags = _split_args(args)
    text = positionals[0] if positionals else ""
    key = flags.get("key")
    if not text:
        _print_err('usage: bd remember "<insight>"')
        return 2
    sidecar = Path(config.path) / _MEMORY_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    entry = {"text": text, "at": datetime.now(timezone.utc).isoformat()}
    if key:
        entry["key"] = key
    with sidecar.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    _print_err(
        "bd-shim NOTE: this project is Jira-backed; 'bd remember' has no Jira "
        f"equivalent, so this insight was stored locally only, in {sidecar}, "
        "not synced to Jira."
    )
    return 0


_ASYNC_HANDLERS = {
    "show": cmd_show,
    "ready": cmd_ready,
    "search": cmd_search,
    "create": cmd_create,
    "comment": cmd_comment_add,
    "comments": cmd_comments,
}


def dispatch(subcommand: str, args: list[str], config: ProjectConfig) -> int:
    import asyncio

    if subcommand == "list":
        positionals, flags = _split_args(args)
        if flags.get("json") and str(flags.get("type") or "").lower() == "epic":
            return asyncio.run(cmd_list_epic_json(args, config))
        if flags.get("parent") or (positionals and not flags):
            return asyncio.run(cmd_list_parent(args, config))
        return asyncio.run(cmd_list_all(args, config))
    if subcommand == "update":
        return asyncio.run(cmd_update_claim(args, config))
    if subcommand == "close":
        return asyncio.run(cmd_close(args, config))
    if subcommand == "remember":
        return cmd_remember(args, config)
    handler = _ASYNC_HANDLERS.get(subcommand)
    if handler is None:
        _print_err(f"bd-shim: unsupported bd subcommand '{subcommand}' for a Jira-backed project")
        return 1
    return asyncio.run(handler(args, config))
