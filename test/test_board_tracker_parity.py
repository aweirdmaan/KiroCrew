"""Asserts kiro_crew.board.tracker.jira_client implements the exact same
callable surface as kiro_crew.board.tracker.beads_client - mirrors
apps/builtins/issue_radar/backend tests' TestClientParity pattern. Currently
jira_client is a stub (NotImplementedError everywhere) pending a follow-up
that implements it for real against Jira's REST API; this test still proves
the *shape* matches today."""
from __future__ import annotations

import inspect

from kiro_crew.board.tracker import beads_client, jira_client

_SURFACE = (
    "has_story", "list_stories", "get_story_detail", "add_comment",
    "update_story", "add_dependency", "remove_dependency", "get_phase",
    "set_phase", "last_gate_comment", "last_recommendation_comment",
)


def test_every_surface_function_exists_on_both_clients():
    for name in _SURFACE:
        assert callable(getattr(beads_client, name, None)), f"beads_client missing {name}"
        assert callable(getattr(jira_client, name, None)), f"jira_client missing {name}"


def test_signatures_agree():
    for name in _SURFACE:
        beads_sig = inspect.signature(getattr(beads_client, name))
        jira_sig = inspect.signature(getattr(jira_client, name))
        assert beads_sig.parameters.keys() == jira_sig.parameters.keys(), (
            f"{name}: beads_client params {list(beads_sig.parameters)} != "
            f"jira_client params {list(jira_sig.parameters)}"
        )
