"""Tests for kiro_crew.board.tracker.jira_client - the Jira-backed
BoardTrackerClient implementation. Mocks aiohttp.ClientSession the same way
test_source_providers_refactor_jira.py does for the dashboard's own Jira
reader, and patches jira_client._headers directly to skip credential
resolution (already covered by test_source_providers_jira.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.board.tracker import jira_client
from kiro_crew.board.tracker.types import ProjectConfig


def _config(tmp_path, **overrides) -> ProjectConfig:
    kwargs = {
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
    """Serves one response per call, in order, from *responses*. Each entry
    is either a _Response or a callable(method, url, **kwargs) -> _Response,
    so a test can branch on the request shape (e.g. GET then DELETE)."""

    def __init__(self, responses: list[Any], seen: list[dict[str, Any]]) -> None:
        self._responses = responses  # shared queue reference - see _serve
        self._seen = seen

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self._seen.append({"method": method, "url": url, **kwargs})
        entry = self._responses.pop(0) if self._responses else _Response(500, b"")
        if callable(entry) and not isinstance(entry, _Response):
            return entry(method, url, **kwargs)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _serve(monkeypatch, *responses: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    queue = list(responses)

    def session(*_args: Any, **_kwargs: Any) -> _Session:
        # One shared queue across every aiohttp.ClientSession(...) call - a
        # fresh session is constructed per _request() invocation (and per
        # retry attempt), but responses are consumed in call order, not
        # reset per session.
        return _Session(queue, seen)

    monkeypatch.setattr(jira_client.aiohttp, "ClientSession", session)
    monkeypatch.setattr(jira_client, "_headers", AsyncMock(return_value={"Authorization": "Basic x"}))
    return seen


def _issue_body(**overrides) -> bytes:
    import json

    payload = {
        "key": "PROJ-1",
        "fields": {
            "summary": "Story one",
            "description": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "the why/what"}]}],
            },
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Ash"},
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-01-02T00:00:00.000+0000",
            "duedate": "2024-02-01",
            "comment": {"comments": [
                {"id": "10", "author": {"displayName": "Misty"}, "created": "t3",
                 "body": {"type": "doc", "version": 1,
                          "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}},
            ]},
            "issuelinks": [],
            "labels": [],
        },
    }
    payload["fields"].update(overrides)
    return json.dumps(payload).encode()


class TestHasStory:
    @pytest.mark.asyncio
    async def test_true_on_200(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(200, b'{"key": "PROJ-1"}'))
        assert await jira_client.has_story(_config(tmp_path), "PROJ-1") is True

    @pytest.mark.asyncio
    async def test_false_on_404(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(404, b""))
        assert await jira_client.has_story(_config(tmp_path), "PROJ-1") is False

    @pytest.mark.asyncio
    async def test_false_on_403(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(403, b"forbidden"))
        assert await jira_client.has_story(_config(tmp_path), "PROJ-1") is False


class TestListStories:
    @pytest.mark.asyncio
    async def test_happy_path_and_phase_label_parsing(self, monkeypatch, tmp_path):
        import json

        search_body = {
            "issues": [
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Story one",
                        "status": {"name": "Open"},
                        "assignee": {"displayName": "Ash"},
                        "priority": {"name": "High"},
                        "labels": ["phase:pipeline", "team:x"],
                        "duedate": "2024-02-01",
                        "comment": {"comments": []},
                        "issuelinks": [],
                        "parent": {"key": "PROJ-0", "fields": {"summary": "Epic zero"}},
                    },
                }
            ],
        }
        _serve(monkeypatch, _Response(200, json.dumps(search_body).encode()))
        with patch("kiro_crew.board.engine.reconcile_stale_running", return_value={"current_run": None}):
            summaries = await jira_client.list_stories(_config(tmp_path))
        assert len(summaries) == 1
        s = summaries[0]
        assert s.id == "PROJ-1"
        assert s.phase == "pipeline"
        assert s.phase_label == "Pipeline"
        assert s.epic_id == "PROJ-0"
        assert s.epic_title == "Epic zero"
        assert s.priority == 1
        assert s.owner == "Ash"
        assert s.due_date == "2024-02-01"

    @pytest.mark.asyncio
    async def test_credential_error_returns_empty_list(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jira_client, "_headers", AsyncMock(side_effect=ValueError("jira_no_credentials")))
        assert await jira_client.list_stories(_config(tmp_path)) == []


class TestGetStoryDetail:
    @pytest.mark.asyncio
    async def test_happy_path_and_adf_description_decoding(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(200, _issue_body()))
        detail = await jira_client.get_story_detail(_config(tmp_path), "PROJ-1")
        assert detail["id"] == "PROJ-1"
        assert detail["title"] == "Story one"
        assert "the why/what" in detail["description"]
        assert detail["status"] == "In Progress"
        assert detail["issue_type"] == "Story"
        assert detail["priority"] == 1
        assert detail["owner"] == "Ash"
        assert detail["due_date"] == "2024-02-01"
        assert len(detail["comments"]) == 1
        assert "hello" in detail["comments"][0]["text"]
        assert detail["comments"][0]["author"] == "Misty"

    @pytest.mark.asyncio
    async def test_404_returns_none(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(404, b""))
        assert await jira_client.get_story_detail(_config(tmp_path), "PROJ-1") is None

    @pytest.mark.asyncio
    async def test_reads_local_start_date_and_rank(self, monkeypatch, tmp_path):
        from kiro_crew.board.state import save_history

        save_history(str(tmp_path), "PROJ-1", {"start_date": "2024-01-15", "rank": 2.5})
        _serve(monkeypatch, _Response(200, _issue_body()))
        detail = await jira_client.get_story_detail(_config(tmp_path), "PROJ-1")
        assert detail["start_date"] == "2024-01-15"
        assert detail["rank"] == 2.5


class TestAddComment:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(201, b'{"id": "99"}'))
        assert await jira_client.add_comment(_config(tmp_path), "PROJ-1", "hi") is True

    @pytest.mark.asyncio
    async def test_blank_text_returns_false(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(201, b"{}"))
        assert await jira_client.add_comment(_config(tmp_path), "PROJ-1", "   ") is False

    @pytest.mark.asyncio
    async def test_401_returns_false(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(401, b"unauthorized"))
        assert await jira_client.add_comment(_config(tmp_path), "PROJ-1", "hi") is False


class TestUpdateStory:
    @pytest.mark.asyncio
    async def test_jira_field_path(self, monkeypatch, tmp_path):
        seen = _serve(monkeypatch, _Response(204, b""))
        ok = await jira_client.update_story(_config(tmp_path), "PROJ-1", title="New title", priority=1)
        assert ok is True
        assert seen[0]["method"] == "PUT"
        assert seen[0]["json"]["fields"]["summary"] == "New title"
        assert seen[0]["json"]["fields"]["priority"] == {"name": "High"}

    @pytest.mark.asyncio
    async def test_local_only_fields_no_jira_call(self, monkeypatch, tmp_path):
        seen = _serve(monkeypatch)
        ok = await jira_client.update_story(_config(tmp_path), "PROJ-1", start_date="2024-01-01", rank=3.0)
        assert ok is True
        assert seen == []
        from kiro_crew.board.state import load_history

        data = load_history(str(tmp_path), "PROJ-1")
        assert data["start_date"] == "2024-01-01"
        assert data["rank"] == 3.0

    @pytest.mark.asyncio
    async def test_nothing_to_update_returns_false(self, monkeypatch, tmp_path):
        seen = _serve(monkeypatch)
        ok = await jira_client.update_story(_config(tmp_path), "PROJ-1")
        assert ok is False
        assert seen == []

    @pytest.mark.asyncio
    async def test_jira_failure_returns_false(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(403, b"forbidden"))
        ok = await jira_client.update_story(_config(tmp_path), "PROJ-1", title="x")
        assert ok is False


class TestAddDependency:
    @pytest.mark.asyncio
    async def test_success_direction(self, monkeypatch, tmp_path):
        seen = _serve(monkeypatch, _Response(201, b"{}"))
        ok = await jira_client.add_dependency(_config(tmp_path), "PROJ-1", "PROJ-2")
        assert ok is True
        body = seen[0]["json"]
        assert body["inwardIssue"] == {"key": "PROJ-1"}
        assert body["outwardIssue"] == {"key": "PROJ-2"}
        assert body["type"] == {"name": "Blocks"}

    @pytest.mark.asyncio
    async def test_401_returns_false(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(401, b"unauthorized"))
        assert await jira_client.add_dependency(_config(tmp_path), "PROJ-1", "PROJ-2") is False


class TestRemoveDependency:
    @pytest.mark.asyncio
    async def test_finds_link_id_and_deletes(self, monkeypatch, tmp_path):
        import json

        read_body = json.dumps({
            "fields": {"issuelinks": [
                {"id": "555", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-2"}},
            ]}
        }).encode()
        seen = _serve(monkeypatch, _Response(200, read_body), _Response(204, b""))
        ok = await jira_client.remove_dependency(_config(tmp_path), "PROJ-1", "PROJ-2")
        assert ok is True
        assert seen[0]["method"] == "GET"
        assert seen[1]["method"] == "DELETE"
        assert seen[1]["url"].endswith("/issueLink/555")

    @pytest.mark.asyncio
    async def test_no_matching_link_returns_false(self, monkeypatch, tmp_path):
        import json

        read_body = json.dumps({"fields": {"issuelinks": []}}).encode()
        _serve(monkeypatch, _Response(200, read_body))
        ok = await jira_client.remove_dependency(_config(tmp_path), "PROJ-1", "PROJ-2")
        assert ok is False

    @pytest.mark.asyncio
    async def test_403_on_read_returns_false(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(403, b"forbidden"))
        ok = await jira_client.remove_dependency(_config(tmp_path), "PROJ-1", "PROJ-2")
        assert ok is False


class TestGetSetPhase:
    @pytest.mark.asyncio
    async def test_get_phase_reads_label(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(200, b'{"fields": {"labels": ["team:x", "phase:review"]}}'))
        assert await jira_client.get_phase(_config(tmp_path), "PROJ-1") == "review"

    @pytest.mark.asyncio
    async def test_get_phase_no_label_is_none(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(200, b'{"fields": {"labels": ["team:x"]}}'))
        assert await jira_client.get_phase(_config(tmp_path), "PROJ-1") is None

    @pytest.mark.asyncio
    async def test_set_phase_removes_stale_and_adds_new(self, monkeypatch, tmp_path):
        read_body = b'{"fields": {"labels": ["phase:pipeline", "team:x"]}}'
        seen = _serve(monkeypatch, _Response(200, read_body), _Response(204, b""))
        ok = await jira_client.set_phase(_config(tmp_path), "PROJ-1", "review")
        assert ok is True
        put_body = seen[1]["json"]
        assert {"remove": "phase:pipeline"} in put_body["update"]["labels"]
        assert {"add": "phase:review"} in put_body["update"]["labels"]

    @pytest.mark.asyncio
    async def test_set_phase_write_failure_returns_false(self, monkeypatch, tmp_path):
        read_body = b'{"fields": {"labels": []}}'
        _serve(monkeypatch, _Response(200, read_body), _Response(403, b"forbidden"))
        ok = await jira_client.set_phase(_config(tmp_path), "PROJ-1", "review")
        assert ok is False


class TestGateAndRecommendationComments:
    @pytest.mark.asyncio
    async def test_last_gate_comment(self, monkeypatch, tmp_path):
        import json

        body = json.dumps({"comments": [
            {"id": "1", "author": {"displayName": "a"}, "created": "t1", "body": "GATE: FAIL\nmore"},
            {"id": "2", "author": {"displayName": "a"}, "created": "t2", "body": "GATE: PASS"},
        ]}).encode()
        _serve(monkeypatch, _Response(200, body))
        assert await jira_client.last_gate_comment(_config(tmp_path), "PROJ-1") == "GATE: PASS"

    @pytest.mark.asyncio
    async def test_last_recommendation_comment(self, monkeypatch, tmp_path):
        import json

        body = json.dumps({"comments": [
            {"id": "1", "author": {"displayName": "a"}, "created": "t1", "body": "RECOMMENDATION: QUICK_FIX"},
        ]}).encode()
        _serve(monkeypatch, _Response(200, body))
        result = await jira_client.last_recommendation_comment(_config(tmp_path), "PROJ-1")
        assert result == "RECOMMENDATION: QUICK_FIX"

    @pytest.mark.asyncio
    async def test_403_returns_none(self, monkeypatch, tmp_path):
        _serve(monkeypatch, _Response(403, b"forbidden"))
        assert await jira_client.last_gate_comment(_config(tmp_path), "PROJ-1") is None
