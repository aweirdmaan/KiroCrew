"""Jira tracker client - the ``BoardTrackerClient`` surface implemented
against Jira Cloud's REST API v3, so a project configured with
``type: "jira"`` works identically to a beads-backed one from the board
UI's point of view (see ``beads_client.py``, the reference implementation
this mirrors field-for-field).

Credential resolution and Cloud-vs-Server/DC auth header construction are
NOT reimplemented here - both already exist in
``kiro_crew.dashboard.source_providers.jira`` (the dashboard's own Jira
reader, used for chat link-rewriting/issue search) and are reused verbatim:
``_jira_request_headers`` resolves the vault/.env-stored API token for a
host and builds the Basic (Cloud) or Bearer (Server/DC) header.

Two fields beads gets natively (a free-form ``metadata`` map) have no Jira
equivalent: ``start_date`` and ``rank`` (timeline manual-ordering). Jira
issue fields are a fixed schema an admin would have to add a custom field
for, which this integration cannot assume exists - so those two ride the
same local sidecar JSON file ``board/state.py`` already uses per-story for
job-run history (``load_history``/``save_history``), under new top-level
keys ``"start_date"``/``"rank"`` that round-trip untouched for every other
reader per that module's own docstring.

Phase is read/written the same way beads does it: a ``phase:<key>`` label,
just on a Jira issue's ``labels`` field instead of a beads label.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from kiro_crew.board.open_questions import last_comment_has_pending_open_questions
from kiro_crew.board.phases import PHASE_BY_KEY
from kiro_crew.board.tracker.types import ProjectConfig, StorySummary
from kiro_crew.dashboard.handlers._shared_request_body import read_capped_response
from kiro_crew.dashboard.source_providers import adf as jira_adf
from kiro_crew.dashboard.source_providers import sanitize
from kiro_crew.dashboard.source_providers.jira import _jira_request_headers
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# board.engine and board.state are imported lazily, inside the functions
# that need them, for the exact same reason beads_client.py does it (see
# that module's own comment): both import this package back, so a
# module-level import here would be a real circular import.

logger = logging.getLogger(__name__)

_JIRA_FETCH_TIMEOUT = 15  # seconds per HTTP call
_RETRY_BACKOFF_SECONDS = 0.5
_LABEL_PREFIX = "phase:"
_API_VERSION = "3"

# A "story" on the board is Jira's Story or Task issue type - a reasonable
# default convention (mirroring beads' own "task parented to an epic" rule)
# since Jira has no single universal "is this a story" field. Configurable
# later (e.g. a per-project issue-type list) if a project needs something
# different; nothing else in this module assumes exactly these two names.
_STORY_ISSUE_TYPES = ("Story", "Task")

_PRIORITY_INT_TO_NAME = {0: "Highest", 1: "High", 2: "Medium", 3: "Low", 4: "Lowest"}
_PRIORITY_NAME_TO_INT = {name.lower(): num for num, name in _PRIORITY_INT_TO_NAME.items()}

# Mirrors beads_client's own set - the pipeline/review phases where an open
# question or a pending RECOMMENDATION comment can legitimately still be
# unread. See that module's docstring for the full rationale.
_PHASES_WHERE_QUESTIONS_MAY_BE_PENDING = {"pipeline"}
_PHASES_WHERE_RECOMMENDATION_MAY_BE_PENDING = {"review"}

_GATE_PREFIX = "GATE:"


class BoardTrackerError(Exception):
    """A Jira call failed in a way worth a clear server-side log line - an
    auth/permission failure (401/403) or an unreachable host/timeout.

    Every public function in this module catches this at its own boundary,
    logs the failure (status + a truncated response body, same spirit as
    ``run_bd``'s own failure logging), and then degrades to the
    ``BoardTrackerClient`` protocol's own False/None/[] contract rather than
    letting it propagate - callers (routes.py) already treat a tracker
    client returning a falsy value as a clean failure, same as beads."""


def _redact(text: object) -> str:
    if not isinstance(text, str) or not text:
        return "" if text is None else str(text)
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _adf_text(node: object) -> str:
    """Plain-ish text for an ADF document/comment body - reuses the
    dashboard's existing ADF->markdown converter (it already redacts
    credentials/exfiltration URLs internally and bounds traversal depth) so
    this module doesn't duplicate that walker. Markdown is a superset of
    plain text for display purposes here: every GATE:/RECOMMENDATION:/OPEN
    QUESTIONS convention this module scans for is a plain line prefix, which
    survives the conversion unchanged."""
    if isinstance(node, dict):
        try:
            return jira_adf._adf_to_markdown(node).strip()
        except Exception:
            return ""
    if isinstance(node, str):
        return node
    return ""


def _adf_wrap_text(text: str) -> dict:
    """Wrap plain text as a single-paragraph ADF document, for a comment
    POST or a description PUT."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _api_base(config: ProjectConfig) -> str:
    return f"https://{config.jira_host}/rest/api/{_API_VERSION}"


def _issue_url(config: ProjectConfig, story_id: str, *, fields: list[str] | None = None) -> str:
    url = f"{_api_base(config)}/issue/{quote(story_id, safe='')}"
    if fields:
        url += "?" + urlencode([("fields", ",".join(fields))])
    return url


async def _headers(config: ProjectConfig) -> dict[str, str]:
    """Reuses the dashboard Jira reader's credential resolution/header
    construction. Raises ValueError (its own contract) when no credentials
    are configured for the host - callers below catch that and log+degrade,
    same as every other failure mode."""
    return await _jira_request_headers(config.jira_host or "")


async def _request(
    method: str, url: str, headers: dict[str, str], *, json_body: Any = None
) -> tuple[int, Any, str]:
    """Issue one Jira HTTP call, with a single retry after a short fixed
    backoff on a 5xx GET (mutating calls - POST/PUT/DELETE - are never
    retried, so a transient 5xx can't risk a double-post/double-link).

    Returns (status, decoded_json_or_None, truncated_body_for_logging).
    Raises BoardTrackerError on 401/403 (logged here) or a network
    failure/timeout - every caller degrades that to its own protocol
    contract rather than letting it propagate.
    """
    timeout = aiohttp.ClientTimeout(total=_JIRA_FETCH_TIMEOUT)
    max_attempts = 2 if method == "GET" else 1
    status = 0
    raw = b""
    for attempt in range(1, max_attempts + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.request(
                    method, url, json=json_body, allow_redirects=False
                ) as resp:
                    status = resp.status
                    raw = await read_capped_response(resp, sanitize._MAX_PAYLOAD_BYTES)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("jira %s %s: network failure: %s", method, url, exc)
            raise BoardTrackerError(f"Could not reach Jira: {type(exc).__name__}") from exc
        if status >= 500 and attempt < max_attempts:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            continue
        break

    body_text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if status in (401, 403):
        logger.warning(
            "jira %s %s: HTTP %s (auth/permission failure): %s",
            method, url, status, body_text[:500],
        )
        raise BoardTrackerError(f"Jira returned HTTP {status} for {method} {url}")

    data = None
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            data = None
    return status, data, body_text[:500]


def _issue_links_depends_on(fields: dict, dependency_link_type: str) -> list[str]:
    """The keys of every issue this one "is blocked by" for the configured
    link type - the ``inwardIssue`` side of a matching link (see
    ``add_dependency``'s docstring for the inward/outward semantics this
    mirrors)."""
    depends_on: list[str] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") if isinstance(link.get("type"), dict) else {}
        if str(link_type.get("name") or "") != dependency_link_type:
            continue
        inward = link.get("inwardIssue")
        if isinstance(inward, dict) and inward.get("key"):
            depends_on.append(str(inward["key"]))
    return depends_on


def _phase_from_labels(labels: object) -> str | None:
    for label in labels or []:
        if isinstance(label, str) and label.startswith(_LABEL_PREFIX):
            return label[len(_LABEL_PREFIX):]
    return None


def _comment_dicts(comment_field: object) -> list[dict]:
    """Normalize ``fields.comment.comments`` (Jira's inline comment payload)
    into the same ``{id, author, text, created_at}`` shape beads_client
    produces, with ADF bodies converted to text and redacted."""
    raw_comments = []
    if isinstance(comment_field, dict):
        raw_comments = comment_field.get("comments") or []
    out = []
    for c in raw_comments:
        if not isinstance(c, dict):
            continue
        author = c.get("author") if isinstance(c.get("author"), dict) else {}
        out.append({
            "id": str(c.get("id") or ""),
            "author": str(author.get("displayName") or author.get("name") or ""),
            "text": _redact(_adf_text(c.get("body"))),
            "created_at": str(c.get("created") or ""),
        })
    return out


async def has_story(config: ProjectConfig, story_id: str) -> bool:
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira has_story(%s): credential error: %s", story_id, exc)
        return False
    url = _issue_url(config, story_id, fields=["id"])
    try:
        status, _data, _body = await _request("GET", url, headers)
    except BoardTrackerError:
        return False
    if status == 404:
        return False
    if status != 200:
        logger.warning("jira has_story(%s): unexpected HTTP %s", story_id, status)
        return False
    return True


async def _search_issues(config: ProjectConfig, headers: dict[str, str], fields: list[str]) -> list[dict]:
    """Every issue matching the board's "story" JQL, paginated through
    Jira's token-paged ``/search/jql`` endpoint. Capped at a generous number
    of pages so a misconfigured project can't spin this forever."""
    issue_types = ", ".join(_STORY_ISSUE_TYPES)
    jql = f'project = "{config.jira_project_key}" AND issuetype in ({issue_types})'
    issues: list[dict] = []
    next_token: str | None = None
    for _page in range(200):
        params = [("jql", jql), ("fields", ",".join(fields)), ("maxResults", "100")]
        if next_token:
            params.append(("nextPageToken", next_token))
        url = f"{_api_base(config)}/search/jql?{urlencode(params, quote_via=quote)}"
        status, data, body = await _request("GET", url, headers)
        if status != 200 or not isinstance(data, dict):
            logger.warning("jira list_stories: search HTTP %s: %s", status, body)
            break
        page_issues = data.get("issues")
        if isinstance(page_issues, list):
            issues.extend(i for i in page_issues if isinstance(i, dict))
        next_token = data.get("nextPageToken") or None
        if not next_token or data.get("isLast"):
            break
    return issues


async def list_stories(config: ProjectConfig) -> list[StorySummary]:
    from kiro_crew.board import engine as board_engine
    from kiro_crew.board.state import last_recommendation_in_comments

    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira list_stories: credential error: %s", exc)
        return []

    fields = ["summary", "status", "assignee", "priority", "labels", "duedate", "comment", "issuelinks", "parent"]
    if config.story_points_field:
        fields.append(config.story_points_field)

    try:
        issues = await _search_issues(config, headers, fields)
    except BoardTrackerError:
        return []

    summaries: list[StorySummary] = []
    for issue in issues:
        story_id = str(issue.get("key") or "")
        if not story_id:
            continue
        fields_obj = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}

        phase_key = _phase_from_labels(fields_obj.get("labels"))
        phase = PHASE_BY_KEY.get(phase_key) if phase_key else None

        parent = fields_obj.get("parent") if isinstance(fields_obj.get("parent"), dict) else {}
        epic_id = str(parent.get("key") or "")
        parent_fields = parent.get("fields") if isinstance(parent.get("fields"), dict) else {}
        epic_title = str(parent_fields.get("summary") or epic_id)

        comments = _comment_dicts(fields_obj.get("comment"))
        pending_open_questions = False
        if phase_key in _PHASES_WHERE_QUESTIONS_MAY_BE_PENDING:
            pending_open_questions = last_comment_has_pending_open_questions(comments)
        recommendation = None
        if phase_key in _PHASES_WHERE_RECOMMENDATION_MAY_BE_PENDING:
            recommendation = last_recommendation_in_comments(comments)

        # Local-only fields (no Jira equivalent) - see module docstring.
        history = board_engine.reconcile_stale_running(config.path, story_id)

        assignee = fields_obj.get("assignee") if isinstance(fields_obj.get("assignee"), dict) else {}
        priority_obj = fields_obj.get("priority") if isinstance(fields_obj.get("priority"), dict) else {}
        priority_int = _PRIORITY_NAME_TO_INT.get(str(priority_obj.get("name") or "").lower())

        story_points = None
        if config.story_points_field:
            story_points = _coerce_float(fields_obj.get(config.story_points_field))

        summaries.append(StorySummary(
            id=story_id,
            title=str(fields_obj.get("summary") or ""),
            status=str((fields_obj.get("status") or {}).get("name") or "") if isinstance(fields_obj.get("status"), dict) else "",
            epic_id=epic_id,
            epic_title=epic_title,
            project_path=config.path,
            phase=phase_key,
            phase_label=phase.label if phase else "Backlog",
            current_run=history.get("current_run"),
            priority=priority_int,
            owner=str(assignee.get("displayName") or assignee.get("name") or ""),
            pending_open_questions=pending_open_questions,
            recommendation=recommendation,
            start_date=history.get("start_date"),
            due_date=fields_obj.get("duedate"),
            rank=_coerce_float(history.get("rank")),
            story_points=story_points,
            depends_on=_issue_links_depends_on(fields_obj, config.dependency_link_type),
        ))
    return summaries


async def get_story_detail(config: ProjectConfig, story_id: str) -> dict | None:
    from kiro_crew.board.state import load_history

    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira get_story_detail(%s): credential error: %s", story_id, exc)
        return None

    fields = [
        "summary", "description", "status", "issuetype", "assignee", "priority",
        "created", "updated", "comment", "duedate", "issuelinks",
    ]
    if config.story_points_field:
        fields.append(config.story_points_field)

    url = _issue_url(config, story_id, fields=fields)
    try:
        status, data, body = await _request("GET", url, headers)
    except BoardTrackerError:
        return None
    if status == 404:
        return None
    if status != 200 or not isinstance(data, dict):
        logger.warning("jira get_story_detail(%s): unexpected HTTP %s: %s", story_id, status, body)
        return None

    fields_obj = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    issuetype = fields_obj.get("issuetype") if isinstance(fields_obj.get("issuetype"), dict) else {}
    status_obj = fields_obj.get("status") if isinstance(fields_obj.get("status"), dict) else {}
    priority_obj = fields_obj.get("priority") if isinstance(fields_obj.get("priority"), dict) else {}
    assignee = fields_obj.get("assignee") if isinstance(fields_obj.get("assignee"), dict) else {}

    local = load_history(config.path, story_id)
    story_points = None
    if config.story_points_field:
        story_points = _coerce_float(fields_obj.get(config.story_points_field))

    return {
        "id": str(data.get("key") or story_id),
        "title": str(fields_obj.get("summary") or ""),
        "description": _redact(_adf_text(fields_obj.get("description"))),
        "status": str(status_obj.get("name") or ""),
        "issue_type": str(issuetype.get("name") or ""),
        "priority": _PRIORITY_NAME_TO_INT.get(str(priority_obj.get("name") or "").lower()),
        "owner": str(assignee.get("displayName") or assignee.get("name") or ""),
        "created_at": str(fields_obj.get("created") or ""),
        "updated_at": str(fields_obj.get("updated") or ""),
        "comments": _comment_dicts(fields_obj.get("comment")),
        "start_date": local.get("start_date"),
        "due_date": fields_obj.get("duedate"),
        "rank": _coerce_float(local.get("rank")),
        "story_points": story_points,
        "depends_on": _issue_links_depends_on(fields_obj, config.dependency_link_type),
    }


async def add_comment(config: ProjectConfig, story_id: str, text: str) -> bool:
    if not text.strip():
        return False
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira add_comment(%s): credential error: %s", story_id, exc)
        return False
    url = f"{_issue_url(config, story_id)}/comment"
    try:
        status, _data, body = await _request("POST", url, headers, json_body={"body": _adf_wrap_text(text)})
    except BoardTrackerError:
        return False
    if not (200 <= status < 300):
        logger.warning("jira add_comment(%s): unexpected HTTP %s: %s", story_id, status, body)
        return False
    return True


async def update_story(
    config: ProjectConfig, story_id: str, *,
    title: str | None = None, description: str | None = None,
    start_date: str | None = None, due_date: str | None = None, rank: float | None = None,
    priority: int | None = None, story_points: float | None = None,
) -> bool:
    """Edit the issue itself. title/description/due_date/priority/story_points
    (when a story_points_field is configured) go to Jira; start_date/rank have
    no Jira equivalent and are written to the local sidecar instead - see the
    module docstring."""
    jira_fields: dict[str, Any] = {}
    if title is not None:
        jira_fields["summary"] = title
    if description is not None:
        jira_fields["description"] = _adf_wrap_text(description)
    if due_date is not None:
        jira_fields["duedate"] = due_date
    if priority is not None:
        name = _PRIORITY_INT_TO_NAME.get(priority)
        if name is not None:
            jira_fields["priority"] = {"name": name}
    if story_points is not None and config.story_points_field:
        jira_fields[config.story_points_field] = story_points

    local_updates: dict[str, Any] = {}
    if start_date is not None:
        local_updates["start_date"] = start_date
    if rank is not None:
        local_updates["rank"] = rank

    if not jira_fields and not local_updates:
        return False

    if local_updates:
        from kiro_crew.board.state import load_history, save_history

        data = load_history(config.path, story_id)
        data.update(local_updates)
        save_history(config.path, story_id, data)

    if not jira_fields:
        return True

    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira update_story(%s): credential error: %s", story_id, exc)
        return False
    url = _issue_url(config, story_id)
    try:
        status, _data, body = await _request("PUT", url, headers, json_body={"fields": jira_fields})
    except BoardTrackerError:
        return False
    if status not in (200, 204):
        logger.warning("jira update_story(%s): unexpected HTTP %s: %s", story_id, status, body)
        return False
    return True


async def add_dependency(config: ProjectConfig, story_id: str, depends_on_id: str) -> bool:
    """story_id depends on (is blocked by) depends_on_id.

    For Jira's "Blocks" link type, outward="blocks" and inward="is blocked
    by". So depends_on_id - the issue that blocks - is the outward side, and
    story_id - the issue that is blocked - is the inward side."""
    depends_on_id = depends_on_id.strip()
    if not depends_on_id:
        return False
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira add_dependency(%s): credential error: %s", story_id, exc)
        return False
    url = f"{_api_base(config)}/issueLink"
    body = {
        "type": {"name": config.dependency_link_type},
        "inwardIssue": {"key": story_id},
        "outwardIssue": {"key": depends_on_id},
    }
    try:
        status, _data, resp_body = await _request("POST", url, headers, json_body=body)
    except BoardTrackerError:
        return False
    if not (200 <= status < 300):
        logger.warning("jira add_dependency(%s, %s): unexpected HTTP %s: %s", story_id, depends_on_id, status, resp_body)
        return False
    return True


async def _find_issue_link_id(
    config: ProjectConfig, headers: dict[str, str], story_id: str, depends_on_id: str
) -> str | None:
    url = _issue_url(config, story_id, fields=["issuelinks"])
    status, data, body = await _request("GET", url, headers)
    if status != 200 or not isinstance(data, dict):
        logger.warning("jira remove_dependency(%s): read HTTP %s: %s", story_id, status, body)
        return None
    fields_obj = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    for link in fields_obj.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if isinstance(other, dict) and str(other.get("key") or "") == depends_on_id:
            link_id = link.get("id")
            if link_id is not None:
                return str(link_id)
    return None


async def remove_dependency(config: ProjectConfig, story_id: str, depends_on_id: str) -> bool:
    """Jira link removal needs the link's own internal id, not the two issue
    keys - so this reads the issue's issuelinks first to find it."""
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira remove_dependency(%s): credential error: %s", story_id, exc)
        return False
    try:
        link_id = await _find_issue_link_id(config, headers, story_id, depends_on_id)
    except BoardTrackerError:
        return False
    if link_id is None:
        logger.warning("jira remove_dependency(%s, %s): no matching issue link found", story_id, depends_on_id)
        return False
    url = f"{_api_base(config)}/issueLink/{quote(link_id, safe='')}"
    try:
        status, _data, body = await _request("DELETE", url, headers)
    except BoardTrackerError:
        return False
    if not (200 <= status < 300):
        logger.warning("jira remove_dependency(%s, %s): unexpected HTTP %s: %s", story_id, depends_on_id, status, body)
        return False
    return True


async def get_phase(config: ProjectConfig, story_id: str) -> str | None:
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira get_phase(%s): credential error: %s", story_id, exc)
        return None
    url = _issue_url(config, story_id, fields=["labels"])
    try:
        status, data, body = await _request("GET", url, headers)
    except BoardTrackerError:
        return None
    if status != 200 or not isinstance(data, dict):
        if status != 404:
            logger.warning("jira get_phase(%s): unexpected HTTP %s: %s", story_id, status, body)
        return None
    fields_obj = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    return _phase_from_labels(fields_obj.get("labels"))


async def set_phase(config: ProjectConfig, story_id: str, new_phase: str) -> bool:
    """Swap the story's `phase:*` label for `phase:<new_phase>`, removing
    every existing phase:* label first - mirrors beads_client.set_phase's
    self-healing behavior for a stray duplicate label."""
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira set_phase(%s): credential error: %s", story_id, exc)
        return False
    url = _issue_url(config, story_id, fields=["labels"])
    try:
        status, data, body = await _request("GET", url, headers)
    except BoardTrackerError:
        return False
    if status != 200 or not isinstance(data, dict):
        logger.warning("jira set_phase(%s): read HTTP %s: %s", story_id, status, body)
        return False
    fields_obj = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    existing = [
        label for label in (fields_obj.get("labels") or [])
        if isinstance(label, str) and label.startswith(_LABEL_PREFIX)
    ]
    update_ops = [{"remove": label} for label in existing]
    update_ops.append({"add": f"{_LABEL_PREFIX}{new_phase}"})
    put_url = _issue_url(config, story_id)
    try:
        put_status, _data, put_body = await _request(
            "PUT", put_url, headers, json_body={"update": {"labels": update_ops}}
        )
    except BoardTrackerError:
        return False
    if put_status not in (200, 204):
        logger.warning("jira set_phase(%s): write HTTP %s: %s", story_id, put_status, put_body)
        return False
    return True


async def _issue_comments(config: ProjectConfig, headers: dict[str, str], story_id: str) -> list[dict] | None:
    url = f"{_issue_url(config, story_id)}/comment?{urlencode([('maxResults', '100')])}"
    status, data, body = await _request("GET", url, headers)
    if status != 200 or not isinstance(data, dict):
        logger.warning("jira: comments read for %s: HTTP %s: %s", story_id, status, body)
        return None
    return _comment_dicts(data)


async def last_gate_comment(config: ProjectConfig, story_id: str) -> str | None:
    """The last comment line starting with "GATE:" across every comment on
    the issue, in chronological order - same semantics as
    beads_client.last_gate_comment."""
    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira last_gate_comment(%s): credential error: %s", story_id, exc)
        return None
    try:
        comments = await _issue_comments(config, headers, story_id)
    except BoardTrackerError:
        return None
    if comments is None:
        return None
    last: str | None = None
    for comment in comments:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if line.startswith(_GATE_PREFIX):
                last = line
    return last


async def last_recommendation_comment(config: ProjectConfig, story_id: str) -> str | None:
    """The last comment line starting with "RECOMMENDATION:" - same
    semantics as beads_client.last_recommendation_comment."""
    from kiro_crew.board.state import last_recommendation_in_comments

    try:
        headers = await _headers(config)
    except ValueError as exc:
        logger.warning("jira last_recommendation_comment(%s): credential error: %s", story_id, exc)
        return None
    try:
        comments = await _issue_comments(config, headers, story_id)
    except BoardTrackerError:
        return None
    if comments is None:
        return None
    return last_recommendation_in_comments(comments)
