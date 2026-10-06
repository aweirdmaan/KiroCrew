"""Tests for beads (bd) -> Knowledge Library sync (aggregate source model).

Follows the same real-store/real-pipeline, mocked-chunker/extractor shape as
test_knowledge_artifact_ingest.py: ingest_file is involved enough (gating,
budget, on_committed) that mocking it entirely would under-test this module.
The one seam under this module's own control is `_run_bd` (the subprocess
call), patched directly rather than mocking asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.knowledge import beads_ingest
from kiro_crew.knowledge.beads_ingest import BeadsKnowledgeSync, render_issue
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


def _one_chunk(text, **kw):
    return [{"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


@pytest.fixture()
def pipeline(kstore):
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        return_value=[{"category": "document", "summary": "s", "entities": []}]
    )
    chunker = MagicMock()
    chunker.chunk.side_effect = _one_chunk
    chunker.chunk_markdown.side_effect = _one_chunk
    chunker.chunk_code.side_effect = _one_chunk
    chunker.chunk_slides.side_effect = _one_chunk
    return IngestionPipeline(
        store=kstore, extractor=extractor, chunker=chunker,
        reader=FileReader(), embedder=None,
    )


def _issue(id_, title="Title", status="open", description="desc", comments=None):
    return {
        "id": id_, "title": title, "status": status, "issue_type": "task",
        "owner": "me", "priority": 2, "description": description,
        "comments": comments or [],
    }


def _items(kstore, source_id):
    return [r["content"] for r in kstore.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()]


class TestRenderIssue:
    def test_includes_core_fields_and_comments(self):
        issue = _issue("BEAD-1", comments=[{"author": "a", "text": "hello", "created_at": "t"}])
        text = render_issue(issue)
        assert "BEAD-1" in text
        assert "Title" in text
        assert "desc" in text
        assert "hello" in text

    def test_redacts_credentials_in_description(self):
        issue = _issue("BEAD-1", description="token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        text = render_issue(issue)
        assert "ghp_" not in text


class TestSyncProject:
    @pytest.mark.asyncio
    async def test_no_bd_binary_is_a_clean_noop(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        with patch.object(beads_ingest.shutil, "which", return_value=None):
            result = await sync.sync_project("/tmp/proj")
        assert result["error"]
        assert kstore.get_source_by_uri("beads:///tmp/proj") is None

    @pytest.mark.asyncio
    async def test_ingests_each_issue_as_its_own_item_group(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": "BEAD-1"}, {"id": "BEAD-2"}]
            if args[0] == "show":
                issue_id = args[args.index("--id") + 1]
                return [_issue(issue_id, title=f"Issue {issue_id}")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            result = await sync.sync_project("/tmp/proj")

        assert result["synced"] == 2
        source = kstore.get_source_by_uri("beads:///tmp/proj")
        assert source is not None
        assert source["source_type"] == "beads"
        contents = _items(kstore, source["id"])
        assert any("Issue BEAD-1" in c for c in contents)
        assert any("Issue BEAD-2" in c for c in contents)

    @pytest.mark.asyncio
    async def test_unchanged_issue_is_not_reingested(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        call_count = 0

        async def fake_run_bd(project_path, *args):
            nonlocal call_count
            if args[0] == "list":
                return [{"id": "BEAD-1", "updated_at": "t1"}]
            if args[0] == "show":
                call_count += 1
                return [_issue("BEAD-1")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            first = await sync.sync_project("/tmp/proj")
            second = await sync.sync_project("/tmp/proj")

        assert first["synced"] == 1
        assert second["synced"] == 0
        # `bd show` is only worth calling once per truly-new/changed issue;
        # the second sync's unchanged-hash short-circuit must not re-show it.
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_edited_issue_replaces_only_its_own_items(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        state = {"desc": "version one", "updated_at": "t1"}

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": "BEAD-1", "updated_at": state["updated_at"]}, {"id": "BEAD-2"}]
            if args[0] == "show":
                issue_id = args[args.index("--id") + 1]
                if issue_id == "BEAD-1":
                    return [_issue("BEAD-1", description=state["desc"])]
                return [_issue("BEAD-2", description="unrelated")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            await sync.sync_project("/tmp/proj")
            state["desc"] = "version two"
            state["updated_at"] = "t2"
            await sync.sync_project("/tmp/proj")

        source = kstore.get_source_by_uri("beads:///tmp/proj")
        contents = _items(kstore, source["id"])
        assert any("version two" in c for c in contents)
        assert not any("version one" in c for c in contents)
        assert any("unrelated" in c for c in contents)

    @pytest.mark.asyncio
    async def test_deleted_issue_is_dropped_from_state(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        live = {"ids": ["BEAD-1", "BEAD-2"]}

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": i} for i in live["ids"]]
            if args[0] == "show":
                issue_id = args[args.index("--id") + 1]
                return [_issue(issue_id)]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            await sync.sync_project("/tmp/proj")
            live["ids"] = ["BEAD-1"]
            result = await sync.sync_project("/tmp/proj")

        assert result["removed"] == 1
        source = kstore.get_source_by_uri("beads:///tmp/proj")
        import json
        props = json.loads(source["properties"])
        assert "BEAD-2" not in props["issues"]
        assert "BEAD-1" in props["issues"]

    @pytest.mark.asyncio
    async def test_unchanged_list_updated_at_skips_show_entirely(self, kstore, pipeline):
        # The cheap pre-filter: an issue whose `bd list` updated_at matches
        # what was recorded last sync must not cost a `bd show` subprocess at
        # all, not just skip the re-ingest after fetching it anyway - the
        # whole point is avoiding N `show` calls on a converged project.
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        show_calls = []

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": "BEAD-1", "updated_at": "same"}]
            if args[0] == "show":
                show_calls.append(args)
                return [_issue("BEAD-1")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            await sync.sync_project("/tmp/proj")
            await sync.sync_project("/tmp/proj")
            await sync.sync_project("/tmp/proj")

        assert len(show_calls) == 1

    @pytest.mark.asyncio
    async def test_bd_list_failure_is_a_clean_noop_not_an_exception(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        with patch.object(beads_ingest, "_run_bd", AsyncMock(return_value=None)):
            result = await sync.sync_project("/tmp/proj")
        assert result["error"]
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_bd_show_failure_for_one_issue_does_not_abort_the_rest(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": "BEAD-1"}, {"id": "BEAD-2"}]
            if args[0] == "show":
                issue_id = args[args.index("--id") + 1]
                if issue_id == "BEAD-1":
                    return None  # bd show failed for this one issue
                return [_issue("BEAD-2")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            result = await sync.sync_project("/tmp/proj")

        assert result["synced"] == 1
        source = kstore.get_source_by_uri("beads:///tmp/proj")
        contents = _items(kstore, source["id"])
        assert any("BEAD-2" in c for c in contents)

    @pytest.mark.asyncio
    async def test_metadata_only_touch_updates_list_updated_at_without_reingesting(self, kstore, pipeline):
        # updated_at bumps (e.g. a label/priority edit) but render_issue's
        # output is byte-identical - must not re-ingest, but must still record
        # the new updated_at so the cheap pre-filter catches it next time too.
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=["/tmp/proj"])
        show_calls = []
        state = {"updated_at": "t1"}

        async def fake_run_bd(project_path, *args):
            if args[0] == "list":
                return [{"id": "BEAD-1", "updated_at": state["updated_at"]}]
            if args[0] == "show":
                show_calls.append(args)
                return [_issue("BEAD-1")]
            raise AssertionError(args)

        with patch.object(beads_ingest, "_run_bd", side_effect=fake_run_bd):
            first = await sync.sync_project("/tmp/proj")
            state["updated_at"] = "t2"
            second = await sync.sync_project("/tmp/proj")
            third = await sync.sync_project("/tmp/proj")

        assert first["synced"] == 1
        assert second["synced"] == 0  # content unchanged despite new updated_at
        assert len(show_calls) == 2  # t1 and t2, not a third time for t2 again
        assert third["synced"] == 0


class TestRunBdSubprocess:
    """_run_bd against a real subprocess (no bd CLI mocking) - exercises the
    actual asyncio.create_subprocess_exec path other tests patch around."""

    @pytest.mark.asyncio
    async def test_missing_binary_returns_none(self, tmp_path):
        with patch.object(beads_ingest.shutil, "which", return_value=None):
            result = await beads_ingest._run_bd(str(tmp_path), "list")
        assert result is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none(self, tmp_path):
        with patch.object(beads_ingest.shutil, "which", return_value="/bin/false"):
            result = await beads_ingest._run_bd(str(tmp_path), "list")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_json_stdout_returns_none(self, tmp_path):
        with patch.object(beads_ingest.shutil, "which", return_value="/bin/echo"):
            # _run_bd always appends "--json" after our args, so the real
            # stdout here is "not json --json\n" - still non-JSON either way.
            result = await beads_ingest._run_bd(str(tmp_path), "not json")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_json_stdout_parses(self, tmp_path):
        # A fixture script rather than /bin/echo: _run_bd always appends
        # "--json" as a trailing arg, which a real binary's argv would ignore
        # but echo would print literally, breaking the JSON. A script that
        # ignores argv entirely sidesteps that without weakening what's
        # actually under test (stdout -> parsed JSON).
        script = tmp_path / "fake_bd.sh"
        script.write_text("#!/bin/sh\necho '[1,2,3]'\n")
        script.chmod(0o755)
        with patch.object(beads_ingest.shutil, "which", return_value=str(script)):
            result = await beads_ingest._run_bd(str(tmp_path), "list")
        assert result == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, tmp_path):
        with patch.object(beads_ingest.shutil, "which", return_value="/bin/sleep"):
            with patch.object(beads_ingest, "_BD_TIMEOUT_SECS", 0.05):
                result = await beads_ingest._run_bd(str(tmp_path), "5")
        assert result is None


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_is_a_noop_with_no_project_paths(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline, project_paths=[])
        await sync.start()  # returns immediately, does not hang

    @pytest.mark.asyncio
    async def test_start_runs_until_stopped(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline,
                                   project_paths=["/tmp/proj"], interval=0.01)
        sync.sync_project = AsyncMock(return_value={"synced": 0, "removed": 0, "error": None})
        task = asyncio.get_event_loop().create_task(sync.start())
        await asyncio.sleep(0.05)
        await sync.stop()
        await asyncio.wait_for(task, timeout=2)
        assert sync.sync_project.await_count >= 1

    @pytest.mark.asyncio
    async def test_a_sync_exception_does_not_kill_the_loop(self, kstore, pipeline):
        sync = BeadsKnowledgeSync(store=kstore, pipeline=pipeline,
                                   project_paths=["/tmp/proj"], interval=0.01)
        sync.sync_project = AsyncMock(side_effect=RuntimeError("boom"))
        task = asyncio.get_event_loop().create_task(sync.start())
        await asyncio.sleep(0.05)
        await sync.stop()
        await asyncio.wait_for(task, timeout=2)
        assert sync.sync_project.await_count >= 1
