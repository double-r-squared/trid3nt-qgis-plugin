"""How a fetcher with a coverage row reaches the model: the match, not a phrase.

A covered fetcher leaves the retrieval index, so no phrasing ranks it; the
model asks ``find_sources`` for a class at a place and the survivors it names
enter the turn's gate, which is where their schemas come from. A fetcher with
no row - the overlay group - stays description-routed and indexed."""

from __future__ import annotations

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.tools.search.find_sources.find_sources import FIND_SOURCES
from trid3nt_server.tools.search.match import covered_sources


@pytest.fixture(scope="module")
def registry():
    """The FULL registry, since a coded tool registers only when imported."""
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY


def test_the_match_is_a_gate_expander_beside_the_tool_search(registry):
    names = agent_server._gate_expander_tool_names()
    assert FIND_SOURCES in names
    assert "search_tools" in names


def test_the_survivors_it_names_are_what_the_gate_expands_on(registry):
    found = {"results": [{"tool_name": "fetch_bluetopo", "ask": {}},
                         {"tool_name": "fetch_ehydro_surveys", "ask": {}},
                         {"tool_name": "fetch_bluetopo", "ask": {}}]}
    assert agent_server._tool_names_from_search_result(found) == [
        "fetch_bluetopo", "fetch_ehydro_surveys"]


def test_every_covered_source_is_a_registered_fetcher(registry):
    covered = covered_sources()
    assert covered, "no source states a coverage row"
    assert not sorted(covered - set(registry))
