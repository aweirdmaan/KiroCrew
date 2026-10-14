"""Tests for kiro_crew.bd_cli.run_bd against a real subprocess (no bd CLI
mocking) - exercises the actual asyncio.create_subprocess_exec path that
knowledge/beads_ingest.py and board/state.py both patch around in their own
tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import bd_cli


@pytest.mark.asyncio
async def test_missing_binary_returns_none(tmp_path):
    with patch.object(bd_cli.shutil, "which", return_value=None):
        result = await bd_cli.run_bd(str(tmp_path), "list")
    assert result is None


@pytest.mark.asyncio
async def test_nonzero_exit_returns_none(tmp_path):
    with patch.object(bd_cli.shutil, "which", return_value="/bin/false"):
        result = await bd_cli.run_bd(str(tmp_path), "list")
    assert result is None


@pytest.mark.asyncio
async def test_non_json_stdout_returns_none(tmp_path):
    with patch.object(bd_cli.shutil, "which", return_value="/bin/echo"):
        # run_bd always appends "--json" after our args, so the real stdout
        # here is "not json --json\n" - still non-JSON either way.
        result = await bd_cli.run_bd(str(tmp_path), "not json")
    assert result is None


@pytest.mark.asyncio
async def test_valid_json_stdout_parses(tmp_path):
    # A fixture script rather than /bin/echo: run_bd always appends "--json"
    # as a trailing arg, which a real binary's argv would ignore but echo
    # would print literally, breaking the JSON. A script that ignores argv
    # entirely sidesteps that without weakening what's actually under test
    # (stdout -> parsed JSON).
    script = tmp_path / "fake_bd.sh"
    script.write_text("#!/bin/sh\necho '[1,2,3]'\n")
    script.chmod(0o755)
    with patch.object(bd_cli.shutil, "which", return_value=str(script)):
        result = await bd_cli.run_bd(str(tmp_path), "list")
    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_timeout_returns_none(tmp_path):
    with patch.object(bd_cli.shutil, "which", return_value="/bin/sleep"):
        with patch.object(bd_cli, "BD_TIMEOUT_SECS", 0.05):
            result = await bd_cli.run_bd(str(tmp_path), "5")
    assert result is None


@pytest.mark.asyncio
async def test_mutation_style_command_parses_structured_json(tmp_path):
    # label add/remove and comments add return a structured JSON result too
    # (e.g. [{"issue_id": ..., "label": ..., "status": "added"}]), not just
    # queries - confirmed against the real bd CLI. A fixture script stands in
    # for that shape here.
    script = tmp_path / "fake_bd.sh"
    script.write_text(
        '#!/bin/sh\necho \'[{"issue_id": "x-1", "label": "phase:done", "status": "added"}]\'\n'
    )
    script.chmod(0o755)
    with patch.object(bd_cli.shutil, "which", return_value=str(script)):
        result = await bd_cli.run_bd(str(tmp_path), "label", "add", "x-1", "phase:done")
    assert result == [{"issue_id": "x-1", "label": "phase:done", "status": "added"}]
