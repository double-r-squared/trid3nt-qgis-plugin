"""How a data source reaches the model: the door, not a phrase.

No source is a tool the model ranks or is told of: its phrasings index as documents
named ``find_sources``, the model asks that door for a class at a place or for words,
and calls the source through ``fetch``."""

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


def test_only_the_tool_search_is_a_gate_expander(registry):
    """find_sources hands back sources, which are called through fetch and never declared."""
    names = agent_server._gate_expander_tool_names()
    assert "search_tools" in names
    assert FIND_SOURCES not in names


def test_every_covered_source_is_a_registered_fetcher(registry):
    covered = covered_sources()
    assert covered, "no source states a row"
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


def test_no_source_is_a_tool_of_the_index_and_each_has_documents_under_the_door(index, registry):
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    assert not set(index.tool_names) & registered_spec_names()
    assert FIND_SOURCES in index.tool_names
    expected = len(_class_corpus()) + 1 + len(registered_spec_names())
    assert index.tool_names.count(FIND_SOURCES) == expected


def test_a_source_dropped_from_the_registry_takes_its_document_with_it(registry):
    import trid3nt_server.tools.search.search_tools.search_tools as dd
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    snapshot = {n: e for n, e in registry.items() if n != "fetch_fema_nfhl_zones"}
    smaller = dd._build_index(registry_snapshot=snapshot)
    assert smaller.tool_names.count(FIND_SOURCES) == (
        len(_class_corpus()) + 1 + len(registered_spec_names()) - 1)


def test_nothing_of_ours_grids_soundings(registry, index):
    """Soundings are gridded by QGIS's IDW through run_qgis_algorithm: no tool
    or index entry is a grid of our own."""
    gone = "survey_" + "surface"
    assert gone not in registry
    assert gone not in set(index.tool_names)


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
async def test_every_source_phrasing_routes_to_the_door(registry, index):
    """MODEL-FREE: a question in a source's own words ranks find_sources in the top 8."""
    import trid3nt_server.tools.search.search_tools.search_tools as dd
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    corpus = dd._load_corpus()
    missed = [(name, query) for name in sorted(registered_spec_names())
              for query in corpus.get(name, []) if FIND_SOURCES not in await _top(query)]
    assert not missed, f"source phrasings that do not route to the door: {missed}"


def test_the_model_is_given_the_door_with_the_vocabulary_on_it_and_no_source(registry):
    from trid3nt_server.model.adapters.adapter import build_tool_declarations
    from trid3nt_server.server.dispatch.helpers import _default_declarable_registry

    decls = {d.name: d for d in build_tool_declarations(_default_declarable_registry())}
    assert {FIND_SOURCES, "fetch"} <= set(decls)
    assert decls[FIND_SOURCES].schema["properties"]["need"]["enum"] == list(DATA_CLASSES)
    assert not [name for name in decls if name.startswith("fetch_")]


def test_the_door_and_the_call_stand_on_the_floor_and_no_source_does(registry):
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names
    from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR

    assert {FIND_SOURCES, "fetch"} <= CORE_FLOOR
    assert not CORE_FLOOR & registered_spec_names()


def test_no_source_is_named_in_the_prompt_or_in_any_declared_set_for_thirty_messages(registry, index):
    import trid3nt_server.tools.search.search_tools.search_tools as dd
    from trid3nt_server.model.adapters.adapter import build_tool_section
    from trid3nt_server.server.dispatch.helpers import _default_declarable_registry
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names
    from trid3nt_server.tools.search.tool_retrieval import (
        _full_registry_floor, retrieve_visible_tools)

    sources = registered_spec_names()
    corpus = dd._load_corpus()
    messages = [corpus[name][0] for name in sorted(sources) if corpus.get(name)][:24]
    messages += ["model a dye release in the Willamette", "map flood zones in Cape Coral",
                 "what is the water table here", "show nws alerts", "how deep is the bay",
                 "run a tsunami over Crescent City"]
    assert len(messages) == 30
    assert not sources & set(build_tool_section(registry).replace("- ", "\n").split(":"))
    assert "fetch_" not in build_tool_section(registry)
    assert not sources & set(_default_declarable_registry())
    assert not sources & _full_registry_floor(set())
    for message in messages:
        assert not sources & retrieve_visible_tools(message, None), message
