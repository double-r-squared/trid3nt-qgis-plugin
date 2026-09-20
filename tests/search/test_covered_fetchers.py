"""How a fetcher with a coverage row reaches the model: the match, not a phrase.

A covered fetcher leaves the retrieval index, so no phrasing ranks it; the
model asks ``find_sources`` for a class at a place and the survivors it names
enter the turn's gate, which is where their schemas come from. A fetcher with
no row - the overlay group - stays description-routed and indexed."""

from __future__ import annotations

import pytest

from trid3nt_contracts.coverage import DATA_CLASSES

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


@pytest.fixture(scope="module")
def index(registry):
    """The warm retrieval index, built once over the full registry."""
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    return dd._get_index()


def _class_corpus() -> dict[str, list[str]]:
    """The phrasings each DATA CLASS carries, read off the tool's own corpus."""
    import pathlib

    import yaml

    from trid3nt_server.tools.search import find_sources as pkg

    path = pathlib.Path(pkg.__file__).resolve().parent / "corpus.yaml"
    loaded = yaml.safe_load(path.read_text()) or {}
    return {key: queries for key, queries in loaded.items()
            if key in DATA_CLASSES}


async def _top(query: str, k: int = 8) -> list[str]:
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    found = await dd.search_tools(query, top_k=k)
    return [row["tool_name"] for row in found["results"]]


def test_a_covered_fetcher_leaves_the_index_and_an_overlay_one_stays(index):
    indexed = set(index.tool_names)
    assert not indexed & covered_sources()
    for overlay in ("fetch_naip", "fetch_opera_dswx", "fetch_nhd_waterbodies"):
        assert overlay in indexed, (
            f"{overlay} states no coverage row and stays description-routed")
    assert FIND_SOURCES in indexed


def test_the_class_corpus_covers_the_whole_vocabulary(index):
    corpus = _class_corpus()
    assert sorted(corpus) == sorted(DATA_CLASSES)
    thin = {cls: len(qs) for cls, qs in corpus.items() if len(qs) < 5}
    assert not thin, f"class corpora below the 5-query recall floor: {thin}"


@pytest.mark.asyncio
async def test_every_class_phrasing_routes_to_the_match(index):
    """MODEL-FREE: a question about a class ranks find_sources in the top 8,
    which is the only door a covered fetcher is reached through."""
    missed = [(cls, query) for cls, queries in _class_corpus().items()
              for query in queries if FIND_SOURCES not in await _top(query)]
    assert not missed, f"class phrasings that do not route to the match: {missed}"


@pytest.mark.asyncio
async def test_every_overlay_tool_is_still_reached_by_its_own_phrasings(registry,
                                                                        index):
    """A fetcher with no coverage row is routed by description, so its own
    corpus must still surface it: the class corpora never crowd it out."""
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    corpus = dd._load_corpus()
    covered = covered_sources()
    unreachable = []
    for tool in sorted(corpus):
        if tool in covered or tool not in registry or tool == FIND_SOURCES:
            continue
        hit = False
        for query in corpus[tool]:
            if tool in await _top(query):
                hit = True
                break
        if not hit:
            unreachable.append(tool)
    assert not unreachable, f"tools no phrasing of their own reaches: {unreachable}"


def test_the_model_is_given_the_match_with_the_vocabulary_on_it(registry):
    from trid3nt_server.adapters.adapter import build_tool_declarations

    decls = {d.name: d for d in build_tool_declarations(dict(registry))}
    assert FIND_SOURCES in decls
    assert decls[FIND_SOURCES].schema["properties"]["need"]["enum"] == list(
        DATA_CLASSES)
    assert "fetch_ehydro_surveys" in decls, (
        "a covered fetcher is still DECLARABLE once the gate expands on it")
