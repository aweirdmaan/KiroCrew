"""Tests for kiro_crew.board.tracker.bd_shim - the bd-compatible CLI shim
that lets crew-rocket's rocket-* skill scripts (which bare-invoke `bd` on
PATH) work against a Jira-backed board project.

HTTP is mocked the same way test_board_jira_client.py mocks it (patching
jira_client.aiohttp.ClientSession and jira_client._headers directly), since
dispatch_jira.py routes every Jira call through jira_client's own _request/
_headers helpers rather than re-implementing the transport.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kiro_crew.board.tracker import config as tracker_config
from kiro_crew.board.tracker import jira_client
from kiro_crew.board.tracker.bd_shim import cli, dispatch_jira, formatting
from kiro_crew.board.tracker.types import ProjectConfig


def _jira_config(tmp_path: Path, **overrides: Any) -> ProjectConfig:
    kwargs: dict[str, Any] = {
        "path": str(tmp_path),
        "type": "jira",
        "jira_host": "acme.atlassian.net",
        "jira_project_key": "PROJ",
    }
    kwargs.update(overrides)
    return ProjectConfig(**kwargs)


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.content = self

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def read(self, n: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    async def iter_chunked(self, _size: int):
        body, self._body = self._body, b""
        if body:
            yield body


class _Session:
    def __init__(self, responses: list[Any], seen: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._seen = seen

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self._seen.append({"method": method, "url": url, **kwargs})
        entry = self._responses.pop(0) if self._responses else _Response(500, b"")
        return entry


def _serve(monkeypatch, *responses: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    queue = list(responses)

    def session(*_args: Any, **_kwargs: Any) -> _Session:
        return _Session(queue, seen)

    monkeypatch.setattr(jira_client.aiohttp, "ClientSession", session)
    monkeypatch.setattr(jira_client, "_headers", AsyncMock(return_value={"Authorization": "Basic x"}))
    return seen


def _issue_json(**fields: Any) -> bytes:
    base = {
        "summary": "A task", "status": {"name": "Open"}, "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"}, "assignee": {"displayName": "Ash"},
        "created": "2026-01-01T10:30:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000",
        "duedate": None,
    }
    base.update(fields)
    return json.dumps({"key": "PROJ-1", "fields": base}).encode()


def _search_body(issues: list[dict[str, Any]], is_last: bool = True) -> bytes:
    return json.dumps({"issues": issues, "isLast": is_last}).encode()


# ---------------------------------------------------------------------------
# Project resolution / passthrough to the real bd binary
# ---------------------------------------------------------------------------


def _make_bd_stub(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "bd"
    stub.write_text("#!/bin/sh\necho stub-bd-ran\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


class TestPassthrough:
    def test_beads_project_execs_real_bd_with_exact_argv(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            tracker_config, "_read_project_configs_sync",
            lambda: [ProjectConfig(path=str(tmp_path), type="beads")],
        )
        monkeypatch.chdir(tmp_path)
        real_bd = _make_bd_stub(tmp_path / "realbin")
        monkeypatch.setattr(cli, "_find_real_bd", lambda: str(real_bd))
        captured: dict[str, Any] = {}

        def fake_execv(path, argv):
            captured["path"] = path
            captured["argv"] = argv

        monkeypatch.setattr(cli.os, "execv", fake_execv)
        rc = cli.main(["show", "crew-rocket-1"])
        assert captured["path"] == str(real_bd)
        assert captured["argv"] == [str(real_bd), "show", "crew-rocket-1"]
        assert rc == 1  # only reached because execv was faked, not a real replace

    def test_no_matching_project_treated_as_beads_passthrough(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tracker_config, "_read_project_configs_sync", lambda: [])
        monkeypatch.chdir(tmp_path)
        real_bd = _make_bd_stub(tmp_path / "realbin")
        monkeypatch.setattr(cli, "_find_real_bd", lambda: str(real_bd))
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli.os, "execv",
            lambda path, argv: captured.update(path=path, argv=argv),
        )
        cli.main(["ready"])
        assert captured["argv"] == [str(real_bd), "ready"]

    def test_no_real_bd_found_exits_1_with_stderr_message(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(tracker_config, "_read_project_configs_sync", lambda: [])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "_find_real_bd", lambda: None)
        with pytest.raises(SystemExit) as exc:
            cli.main(["ready"])
        assert exc.value.code == 1
        assert "no beads binary found on PATH" in capsys.readouterr().err

    def test_jira_project_does_not_exec_real_bd(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            tracker_config, "_read_project_configs_sync",
            lambda: [_jira_config(tmp_path)],
        )
        monkeypatch.chdir(tmp_path)
        _serve(monkeypatch, _Response(404, b""))
        called = []
        monkeypatch.setattr(cli, "_exec_real_bd", lambda argv: called.append(argv))
        cli.main(["show", "PROJ-1"])
        assert called == []

    def test_longest_ancestor_match_resolves_subdirectory_cwd(self, monkeypatch, tmp_path):
        sub = tmp_path / "nested" / "deeper"
        sub.mkdir(parents=True)
        monkeypatch.setattr(
            tracker_config, "_read_project_configs_sync",
            lambda: [_jira_config(tmp_path)],
        )
        resolved = cli._resolve_project_config(str(sub))
        assert resolved is not None
        assert resolved.type == "jira"


# ---------------------------------------------------------------------------
# Jira-backed dispatch - one test per table row
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_plain_task(self, monkeypatch, tmp_path, capsys):
        _serve(monkeypatch, _Response(200, _issue_json(summary="Grape 1")))
        import asyncio
        rc = asyncio.run(dispatch_jira.cmd_show(["PROJ-1"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-1" in out
        assert "Grape 1" in out

    def test_show_epic_fetches_children(self, monkeypatch, tmp_path, capsys):
        import asyncio

        epic_body = _issue_json(summary="Epic one", issuetype={"name": "Epic"})
        children_body = _search_body([
            {"key": "PROJ-2", "fields": {"summary": "Child", "status": {"name": "Open"}, "priority": {"name": "High"}}},
        ])
        _serve(monkeypatch, _Response(200, epic_body), _Response(200, children_body))
        rc = asyncio.run(dispatch_jira.cmd_show(["PROJ-1"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "CHILDREN" in out
        assert "PROJ-2" in out

    def test_show_not_found(self, monkeypatch, tmp_path, capsys):
        import asyncio

        _serve(monkeypatch, _Response(404, b""))
        rc = asyncio.run(dispatch_jira.cmd_show(["PROJ-1"], _jira_config(tmp_path)))
        assert rc == 1
        assert "not found" in capsys.readouterr().err


class TestListParent:
    def test_list_parent(self, monkeypatch, tmp_path, capsys):
        import asyncio

        body = _search_body([
            {"key": "PROJ-2", "fields": {"summary": "Child", "status": {"name": "Open"}, "priority": {"name": "Medium"}, "issuetype": {"name": "Task"}}},
        ])
        seen = _serve(monkeypatch, _Response(200, body))
        rc = asyncio.run(dispatch_jira.cmd_list_parent(["--parent", "PROJ-1"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-2" in out
        assert "parent" in seen[0]["url"] and "PROJ-1" in seen[0]["url"]


class TestListAll:
    def test_list_all(self, monkeypatch, tmp_path, capsys):
        import asyncio

        body = _search_body([
            {"key": "PROJ-3", "fields": {"summary": "Something", "status": {"name": "Open"}, "priority": {"name": "Low"}, "issuetype": {"name": "Epic"}}},
        ])
        _serve(monkeypatch, _Response(200, body))
        rc = asyncio.run(dispatch_jira.cmd_list_all([], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-3" in out
        assert "Total: 1 issues" in out


class TestListEpicJson:
    def test_structured_json_id_extraction(self, monkeypatch, tmp_path, capsys):
        import asyncio

        body = _search_body([
            {"key": "PROJ-9", "fields": {"summary": "Newest epic", "status": {"name": "Open"}, "priority": {"name": "High"}, "issuetype": {"name": "Epic"}, "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-01T00:00:00.000+0000"}},
        ])
        seen = _serve(monkeypatch, _Response(200, body))
        rc = asyncio.run(dispatch_jira.cmd_list_epic_json(
            ["--type", "epic", "--sort", "created", "--limit", "1", "--json"], _jira_config(tmp_path),
        ))
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed[0]["id"] == "PROJ-9"
        assert "ORDER BY created DESC" in seen[0]["url"].replace("%20", " ")

    def test_reverse_flag_flips_to_ascending(self, monkeypatch, tmp_path):
        import asyncio

        seen = _serve(monkeypatch, _Response(200, _search_body([])))
        asyncio.run(dispatch_jira.cmd_list_epic_json(
            ["--type", "epic", "--sort", "created", "--limit", "1", "--json", "--reverse"],
            _jira_config(tmp_path),
        ))
        assert "ORDER BY created ASC" in seen[0]["url"].replace("%20", " ")


class TestReady:
    def test_ready_excludes_blocked(self, monkeypatch, tmp_path, capsys):
        import asyncio

        blocked_issue = {
            "key": "PROJ-5",
            "fields": {
                "summary": "Blocked task", "status": {"name": "Open"}, "priority": {"name": "Medium"},
                "parent": {"key": "PROJ-0", "fields": {"summary": "Epic"}},
                "issuelinks": [{
                    "type": {"name": "Blocks"},
                    "inwardIssue": {"fields": {"status": {"statusCategory": {"key": "new"}}}},
                }],
            },
        }
        ready_issue = {
            "key": "PROJ-6",
            "fields": {
                "summary": "Ready task", "status": {"name": "Open"}, "priority": {"name": "Medium"},
                "parent": {"key": "PROJ-0", "fields": {"summary": "Epic"}},
                "issuelinks": [],
            },
        }
        _serve(monkeypatch, _Response(200, _search_body([blocked_issue, ready_issue])))
        rc = asyncio.run(dispatch_jira.cmd_ready([], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-6" in out
        assert "PROJ-5" not in out


class TestSearch:
    def test_search_builds_jql_from_query(self, monkeypatch, tmp_path, capsys):
        import asyncio

        body = _search_body([
            {"key": "PROJ-7", "fields": {"summary": "auth bug", "status": {"name": "Open"}, "priority": {"name": "High"}, "issuetype": {"name": "Task"}}},
        ])
        seen = _serve(monkeypatch, _Response(200, body))
        rc = asyncio.run(dispatch_jira.cmd_search(["auth"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-7" in out
        assert "auth" in seen[0]["url"]


class TestCreate:
    def test_create_epic(self, monkeypatch, tmp_path, capsys):
        import asyncio

        seen = _serve(monkeypatch, _Response(201, b'{"key": "PROJ-10"}'))
        rc = asyncio.run(dispatch_jira.cmd_create(
            ["--type=epic", "--title=New Epic", "--description=why/what"], _jira_config(tmp_path),
        ))
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-10" in out
        assert seen[0]["json"]["fields"]["issuetype"] == {"name": "Epic"}

    def test_create_task_with_parent(self, monkeypatch, tmp_path):
        import asyncio

        seen = _serve(monkeypatch, _Response(201, b'{"key": "PROJ-11"}'))
        rc = asyncio.run(dispatch_jira.cmd_create(
            ["--type=task", "--parent=PROJ-1", "--title=Grape 1"], _jira_config(tmp_path),
        ))
        assert rc == 0
        assert seen[0]["json"]["fields"]["parent"] == {"key": "PROJ-1"}
        assert seen[0]["json"]["fields"]["issuetype"] == {"name": "Task"}


class TestUpdateClaim:
    def test_claim_sets_assignee_and_transitions(self, monkeypatch, tmp_path):
        import asyncio

        seen = _serve(
            monkeypatch,
            _Response(200, b'{"accountId": "acc-1"}'),  # GET /myself
            _Response(204, b""),  # PUT fields (assignee)
            _Response(200, json.dumps({"transitions": [{"id": "31", "name": "In Progress"}]}).encode()),  # GET transitions
            _Response(204, b""),  # POST transition
        )
        rc = asyncio.run(dispatch_jira.cmd_update_claim(["PROJ-1", "--claim"], _jira_config(tmp_path)))
        assert rc == 0
        assert seen[1]["json"]["fields"]["assignee"] == {"accountId": "acc-1"}
        assert seen[3]["json"]["transition"] == {"id": "31"}

    def test_missing_claim_flag_is_usage_error(self, tmp_path):
        import asyncio

        rc = asyncio.run(dispatch_jira.cmd_update_claim(["PROJ-1"], _jira_config(tmp_path)))
        assert rc == 2


class TestClose:
    def test_close_with_reason_comments_and_transitions(self, monkeypatch, tmp_path):
        import asyncio

        seen = _serve(
            monkeypatch,
            _Response(201, b'{"id": "99"}'),  # POST comment (close reason)
            _Response(200, json.dumps({"transitions": [{"id": "41", "name": "Done"}]}).encode()),  # GET transitions
            _Response(204, b""),  # POST transition
        )
        rc = asyncio.run(dispatch_jira.cmd_close(["PROJ-1", "--reason=all tests pass"], _jira_config(tmp_path)))
        assert rc == 0
        assert "Close reason: all tests pass" in seen[0]["json"]["body"]["content"][0]["content"][0]["text"]
        assert seen[2]["json"]["transition"] == {"id": "41"}


class TestCommentAdd:
    def test_adds_gate_pass_comment(self, monkeypatch, tmp_path):
        import asyncio

        seen = _serve(monkeypatch, _Response(201, b'{"id": "1"}'))
        rc = asyncio.run(dispatch_jira.cmd_comment_add(["PROJ-1", "GATE: PASS all good"], _jira_config(tmp_path)))
        assert rc == 0
        text = seen[0]["json"]["body"]["content"][0]["content"][0]["text"]
        assert text == "GATE: PASS all good"


class TestComments:
    def test_renders_matching_real_bd_shape(self, monkeypatch, tmp_path, capsys):
        import asyncio

        payload = {
            "comments": [
                {"id": "1", "author": {"displayName": "Amaan Shaikh"}, "created": "2026-09-25T20:08:58.000+0000",
                 "body": {"type": "doc", "version": 1, "content": [
                     {"type": "paragraph", "content": [{"type": "text", "text": "GATE: PASS plan confirmed"}]},
                 ]}},
            ],
        }
        _serve(monkeypatch, _Response(200, json.dumps(payload).encode()))
        rc = asyncio.run(dispatch_jira.cmd_comments(["PROJ-1"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert out.startswith("\nComments on PROJ-1:\n\n[Amaan Shaikh] at 2026-09-25 20:08\n")
        assert "  GATE: PASS plan confirmed" in out

    def test_no_comments(self, monkeypatch, tmp_path, capsys):
        import asyncio

        _serve(monkeypatch, _Response(200, json.dumps({"comments": []}).encode()))
        rc = asyncio.run(dispatch_jira.cmd_comments(["PROJ-1"], _jira_config(tmp_path)))
        out = capsys.readouterr().out
        assert rc == 0
        assert out == "No comments on PROJ-1\n"


class TestRemember:
    def test_local_only_sidecar_and_stderr_note(self, tmp_path, capsys):
        config = _jira_config(tmp_path)
        rc = dispatch_jira.cmd_remember(["a cross-cutting insight"], config)
        assert rc == 0
        sidecar = tmp_path / ".kiro" / "crew" / "jira-memory.jsonl"
        assert sidecar.exists()
        entry = json.loads(sidecar.read_text().splitlines()[0])
        assert entry["text"] == "a cross-cutting insight"
        err = capsys.readouterr().err
        assert "stored locally only" in err
        assert "not synced to Jira" in err


# ---------------------------------------------------------------------------
# formatting.py directly
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_comments_empty(self):
        assert formatting.format_comments("X-1", []) == "No comments on X-1\n"

    def test_comments_nonempty_matches_real_bd_byte_shape(self):
        out = formatting.format_comments("X-1", [
            {"author": "A", "created_at": "2026-01-01 10:00", "text": "line1\nline2"},
        ])
        assert out == "\nComments on X-1:\n\n[A] at 2026-01-01 10:00\n  line1\n  line2\n\n"


# ---------------------------------------------------------------------------
# Full CLI dispatch end-to-end (cli.main) for one representative subcommand,
# proving routing (not just the handler) works.
# ---------------------------------------------------------------------------


class TestCliMainJiraRouting:
    def test_main_routes_show_to_jira_dispatch(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            tracker_config, "_read_project_configs_sync",
            lambda: [_jira_config(tmp_path)],
        )
        monkeypatch.chdir(tmp_path)
        _serve(monkeypatch, _Response(200, _issue_json(summary="Hello")))
        rc = cli.main(["show", "PROJ-1"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Hello" in out

    def test_main_list_all_routes_correctly(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            tracker_config, "_read_project_configs_sync",
            lambda: [_jira_config(tmp_path)],
        )
        monkeypatch.chdir(tmp_path)
        _serve(monkeypatch, _Response(200, _search_body([])))
        rc = cli.main(["list", "--all"])
        assert rc == 0
