"""Beads (``bd``) issue-tracker ingestion into the Knowledge Library.

Mirrors the Artifacts aggregate-source pattern (see ``artifact_ingest.py``):
one ``sources`` row per configured project path (``source_type="beads"``,
``uri="beads://<project_path>"``), with one independently-replaceable item
group per issue id -- the same item-group shape a folder source keys per file
and Artifacts keys per slug. Issue content (title, description, comments) is
rendered to a single text document and handed to the SAME chunk/embed/extract
pipeline every other source uses, so an issue becomes semantically searchable
and fused into the Knowledge Library's keyword+graph+vector ranking instead of
only reachable by an agent that already knows to run ``bd show <id>``.

Event-driven sync (the Artifacts pattern) is not available here: ``bd`` is an
external CLI over a git-backed store with no in-process write hook this
process can observe. Sync is instead periodic polling of ``bd list --all
--json``, diffed by content hash per issue against a small state blob kept in
the aggregate source's own ``properties`` column -- fine at the scale a
personal/team issue tracker runs at (tens to low thousands of issues); a
dedicated state table like Artifacts' ``artifact_item_state`` would be the
next step if that blob ever got large enough for the whole-column rewrite in
``revise_source_properties`` to matter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.knowledge.ingestion import IngestionPipeline
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

SOURCE_TYPE = "beads"
# Wall-clock budget for one `bd` invocation. `bd show` on a huge issue (many
# comments) is still a local sqlite/dolt read, not a network call -- this is a
# "the binary hung" guard, not a tuned timeout for real data volume.
_BD_TIMEOUT_SECS = 30


def render_issue(issue: dict) -> str:
    """Render one ``bd show --json`` issue object to a single text document.

    Redacted the same way every other ingested document is: a beads comment
    can legitimately contain a pasted credential or an exfiltration-flagged
    URL during incident/debug discussion, and the Knowledge Library citing it
    back verbatim in a search result would be a worse leak than the original
    comment, which at least required opening the issue to see.
    """
    lines = [
        f"# {issue.get('title') or issue.get('id') or 'untitled'}",
        "",
        f"ID: {issue.get('id', '')}",
        f"Status: {issue.get('status', '')}",
        f"Type: {issue.get('issue_type', '')}",
    ]
    if issue.get("owner"):
        lines.append(f"Owner: {issue['owner']}")
    if issue.get("priority") is not None:
        lines.append(f"Priority: {issue['priority']}")
    if issue.get("created_at"):
        lines.append(f"Created: {issue['created_at']}")
    if issue.get("updated_at"):
        lines.append(f"Updated: {issue['updated_at']}")
    lines.append("")
    if issue.get("description"):
        lines.append(str(issue["description"]))
        lines.append("")
    comments = issue.get("comments") or []
    if comments:
        lines.append("## Comments")
        for c in comments:
            author = c.get("author") or "unknown"
            when = c.get("created_at") or ""
            lines.append(f"\n### {author} ({when})\n{c.get('text', '')}")
    text = "\n".join(lines)
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


async def _run_bd(project_path: str, *args: str) -> object | None:
    """Run ``bd <args> --json`` in ``project_path``; return parsed stdout, or
    None on any failure (missing binary, non-zero exit, timeout, bad JSON).

    None is deliberately not an exception: every caller treats a failed
    attempt as "nothing to do this round" rather than a fatal sync error, so a
    transient `bd` hiccup (a Dolt lock, a momentarily-missing binary on PATH)
    does not escalate into a source the scheduler eventually quiesces to
    'error' the way three real sync failures would (see SyncScheduler).
    """
    bd = shutil.which("bd")
    if not bd:
        logger.debug("beads sync: 'bd' not on PATH, skipping %s", project_path)
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            bd, *args, "--json",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_BD_TIMEOUT_SECS)
    except (OSError, asyncio.TimeoutError):
        logger.warning("bd %s failed to run in %s", " ".join(args), project_path, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.warning(
            "bd %s exited %d in %s: %s",
            " ".join(args), proc.returncode, project_path,
            stderr.decode(errors="replace")[:500],
        )
        return None
    try:
        return json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        logger.warning("bd %s returned non-JSON stdout in %s", " ".join(args), project_path)
        return None


class BeadsKnowledgeSync:
    """Periodically mirrors one or more ``bd`` projects into the Knowledge
    Library. One aggregate "Beads" source per project path; one
    independently-replaceable item group per issue id."""

    def __init__(self, store: "KnowledgeStore", pipeline: "IngestionPipeline",
                 project_paths: list[str], interval: int = 300):
        self.store = store
        self.pipeline = pipeline
        self.project_paths = list(project_paths)
        self.interval = interval
        self._stop_event = asyncio.Event()

    @staticmethod
    def _source_uri(project_path: str) -> str:
        return f"beads://{project_path}"

    async def start(self) -> None:
        if not self.project_paths:
            return
        logger.info("Beads sync started: %d project(s), interval=%ds",
                    len(self.project_paths), self.interval)
        while not self._stop_event.is_set():
            for project_path in self.project_paths:
                try:
                    await self.sync_project(project_path)
                except Exception:
                    logger.exception("Beads sync failed for %s", project_path)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()
        logger.info("Beads sync stopped")

    def _get_or_create_source(self, project_path: str) -> tuple[str, dict]:
        """Synchronous (sqlite); callers hop this to a worker thread."""
        uri = self._source_uri(project_path)
        source = self.store.get_source_by_uri(uri)
        if source is None:
            source_id = self.store.add_source(
                name=f"Beads ({project_path})",
                source_type=SOURCE_TYPE,
                uri=uri,
                properties={"project_path": project_path, "issues": {}},
            )
            source = self.store.get_source_by_uri(uri)
        props = json.loads(source.get("properties") or "{}") if isinstance(
            source.get("properties"), str) else (source.get("properties") or {})
        return source["id"], props

    async def sync_project(self, project_path: str) -> dict:
        """Discover + (re)ingest every issue ``bd`` knows about under
        ``project_path``. Returns a small summary for logging/diagnostics."""
        result: dict[str, object] = {"synced": 0, "removed": 0, "error": None}
        listing = await _run_bd(project_path, "list", "--all", "--limit", "0")
        if not isinstance(listing, list):
            result["error"] = "bd list failed, returned no issues, or bd not found"
            return result

        source_id, props = await asyncio.to_thread(self._get_or_create_source, project_path)
        issue_state: dict = dict(props.get("issues") or {})
        live_ids = {row.get("id") for row in listing if row.get("id")}

        for row in listing:
            issue_id = row.get("id")
            if not issue_id:
                continue
            prior = issue_state.get(issue_id) or {}
            # Cheap pre-filter: `bd list`'s own `updated_at` is already on
            # hand for every issue in one call, so an unchanged issue costs
            # nothing beyond that - no per-issue `bd show` subprocess, which
            # is what would otherwise make every poll of a large, mostly-
            # converged project cost one subprocess spawn per issue it holds.
            # The content hash below (computed only when this check trips)
            # remains the correctness backstop: a metadata-only touch that
            # still bumps `updated_at` without changing renderable content
            # just costs one avoidable `show` call, not a spurious re-ingest.
            list_updated_at = row.get("updated_at")
            if list_updated_at and prior.get("list_updated_at") == list_updated_at:
                continue

            detail = await _run_bd(project_path, "show", "--id", issue_id)
            if not detail:
                continue
            issue = detail[0] if isinstance(detail, list) and detail else detail
            if not isinstance(issue, dict):
                continue
            text = render_issue(issue)
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            if prior.get("hash") == content_hash:
                # Content unchanged despite a bumped updated_at (e.g. a label
                # or priority edit not rendered into the text) - still worth
                # recording the new updated_at so the next poll's pre-filter
                # catches it again without another `show` call.
                issue_state[issue_id] = {**prior, "list_updated_at": list_updated_at}
                continue

            new_item_ids: list[str] = []

            def _record_ids(ids: list[str]) -> None:
                new_item_ids.extend(ids)

            tmp_path = await asyncio.to_thread(self._write_tmp, text)
            try:
                job_id = await self.pipeline.ingest_file(
                    tmp_path,
                    original_name=f"{issue_id}.md",
                    source_id=source_id,
                    old_item_ids=prior.get("item_ids") or [],
                    # Background sync, not a user's one-shot import - exempt
                    # from the explicit-import chunk budget, same as the
                    # folder watcher and artifact sync paths.
                    count_toward_import_budget=False,
                    on_committed=_record_ids,
                )
            finally:
                await asyncio.to_thread(self._unlink_quiet, tmp_path)

            if job_id is None:
                continue  # content-hash-unchanged path inside ingest_file itself
            issue_state[issue_id] = {
                "hash": content_hash,
                "item_ids": new_item_ids,
                "list_updated_at": list_updated_at,
            }
            result["synced"] += 1

        # Issues `bd` no longer reports at all (--all already includes closed,
        # so this means genuinely deleted) are dropped from state so a later
        # re-creation under the same id starts clean. The underlying chunks
        # are cleaned up the next time ingest_file replaces this item group;
        # nothing deletes them proactively here, same tradeoff Artifacts makes
        # for a slug that stops appearing in the store.
        gone = set(issue_state) - live_ids
        for issue_id in gone:
            issue_state.pop(issue_id, None)
            result["removed"] += 1

        def _save_state() -> None:
            def _revise(p: dict) -> str | None:
                p["issues"] = issue_state
                return None
            self.store.revise_source_properties(
                source_id, _revise, last_synced=datetime.now().isoformat())

        await asyncio.to_thread(_save_state)
        return result

    @staticmethod
    def _write_tmp(text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md", prefix="kc-beads-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            os.unlink(path)
            raise
        return path

    @staticmethod
    def _unlink_quiet(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
