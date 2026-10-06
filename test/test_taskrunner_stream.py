"""Tests for /api/taskrunner/{task_id}/stream — Server-Sent Events of step changes.

Isolated from push_slots_update on purpose (see the handler's own docstring):
this polls the same in-memory Project api_taskrunner_status already reads and
emits one SSE frame per change. A real TestClient/TestServer round trip is
used rather than make_mocked_request, because StreamResponse.prepare()/.write()
need an actual transport - the same pattern test_file_stream.py uses for its
own streaming endpoint.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_taskrunner_stream


def _make_app(state: SimpleNamespace) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/taskrunner/{task_id}/stream", api_taskrunner_stream)
    app["state"] = state
    return app


def _parse_frames(body: bytes) -> list[dict]:
    text = body.decode()
    return [
        json.loads(chunk[len("data: ") :])
        for chunk in text.split("\n\n")
        if chunk.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_unknown_task_id_is_404():
    runner = MagicMock()
    runner._runs = {}
    state = SimpleNamespace(task_runner=runner)
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/taskrunner/nope/stream")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_no_runner_available_is_400():
    state = SimpleNamespace(task_runner=None)
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/taskrunner/t1/stream")
        assert resp.status == 400


@pytest.mark.asyncio
async def test_streams_run_step_result_then_ends():
    runner = MagicMock()
    runner._runs = {"t1": MagicMock()}
    snapshots = [
        {
            "runs": [
                {
                    "task_id": "t1",
                    "status": "running",
                    "running": True,
                    "completed": 0,
                    "tasks": 1,
                    "task_details": [{"index": 1, "title": "Step one", "status": "in_progress"}],
                }
            ]
        },
        {
            "runs": [
                {
                    "task_id": "t1",
                    "status": "running",
                    "running": True,
                    "completed": 1,
                    "tasks": 1,
                    "task_details": [
                        {"index": 1, "title": "Step one", "status": "passed", "result": "all good"}
                    ],
                }
            ]
        },
        {
            "runs": [
                {
                    "task_id": "t1",
                    "status": "completed",
                    "running": False,
                    "completed": 1,
                    "tasks": 1,
                    "task_details": [
                        {"index": 1, "title": "Step one", "status": "passed", "result": "all good"}
                    ],
                }
            ]
        },
    ]
    runner.status.side_effect = snapshots + [snapshots[-1]] * 10
    state = SimpleNamespace(task_runner=runner)
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/taskrunner/t1/stream")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        body = await resp.read()

    frames = _parse_frames(body)
    types = [f["type"] for f in frames]
    # A second "run" frame is correct here, not a duplicate: status genuinely
    # transitions running -> completed between the last two snapshots.
    assert types == ["run", "step", "step", "result", "run", "ended"]
    assert frames[0]["status"] == "running"
    assert frames[1]["status"] == "in_progress"
    assert frames[2]["status"] == "passed"
    assert frames[3]["text"] == "all good"
    assert frames[4]["status"] == "completed"
    assert frames[5]["status"] == "completed"


@pytest.mark.asyncio
async def test_redacts_credentials_and_exfil_urls_in_streamed_text():
    runner = MagicMock()
    runner._runs = {"t1": MagicMock()}
    secret = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    snapshot = {
        "runs": [
            {
                "task_id": "t1",
                "status": "completed",
                "running": False,
                "completed": 1,
                "tasks": 1,
                "task_details": [
                    {"index": 1, "title": "Step one", "status": "failed", "error": secret}
                ],
            }
        ]
    }
    runner.status.side_effect = [snapshot] * 5
    state = SimpleNamespace(task_runner=runner)
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/taskrunner/t1/stream")
        body = await resp.read()

    frames = _parse_frames(body)
    result_frame = next(f for f in frames if f["type"] == "result")
    assert "ghp_" not in result_frame["text"]
