"""``find_sources(query=...)``: every data source reached by name and words, ranked on none of the match's facts.

Model-free over the source index the registry builds: the retrieval floor each source's own phrasings must clear, the
name that always finds it, the two refusals of an unclear ask, and the params line a row carries."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.search.find_sources.find_sources import (
    FindSourcesAskError, find_sources, params_line)


@pytest.fixture(scope="module")
def registry():
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY


@pytest.fixture(scope="module")
def source_index(registry):
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    dd._reset_index_for_tests()
    return dd._get_source_index()


def _ranked(text, index):
    from trid3nt_server.tools.search.tool_retrieval import ranked_docs

    return [index.tool_names[at] for at, _ in ranked_docs(text, index)]


def test_the_source_index_holds_every_spec_served_fetcher_and_nothing_else(registry, source_index):
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    assert set(source_index.tool_names) == registered_spec_names()
    assert len(source_index.tool_names) == len(set(source_index.tool_names))


@pytest.mark.asyncio
async def test_flood_zones_ranks_the_fema_zones_source_in_the_top_three(registry):
    found = await find_sources(query="flood zones")
    assert "fetch_fema_nfhl_zones" in [row["source"] for row in found["results"]][:3]
    assert {row["kind"] for row in found["results"]} == {"unranked"}


@pytest.mark.asyncio
async def test_a_row_carries_what_the_source_is_what_it_takes_and_the_call_it_is_ready_for(registry):
    found = await find_sources(query="flood zones", place=[-90.2, 29.9, -90.0, 30.0])
    row = next(r for r in found["results"] if r["source"] == "fetch_fema_nfhl_zones")
    assert row["about"] and row["params"].startswith("bbox*")
    assert row["ask"] == {"bbox": [-90.2, 29.9, -90.0, 30.0]}


def test_every_source_with_phrasings_is_in_the_top_three_of_at_least_one_of_them(registry, source_index):
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    corpus = dd._load_corpus()
    unreached = [name for name in sorted(source_index.tool_names) if corpus.get(name)
                 and all(name not in _ranked(q, source_index)[:3] for q in corpus[name])]
    assert not unreached, f"sources none of whose own phrasings finds them in the top 3: {unreached}"


def test_every_source_is_among_the_rows_its_own_name_returns(registry, source_index):
    from trid3nt_server.tools.search.match import RANKED_ROWS

    wrong = [name for name in source_index.tool_names
             if name not in _ranked(name, source_index)[:RANKED_ROWS]]
    assert not wrong, f"names that do not find their source among the rows returned: {wrong}"


def test_a_source_left_out_of_the_index_is_no_longer_found_by_its_phrasings(registry):
    import trid3nt_server.tools.search.search_tools.search_tools as dd

    snapshot = {name: entry for name, entry in registry.items() if name != "fetch_fema_nfhl_zones"}
    index = dd._build_source_index(registry_snapshot=snapshot)
    assert "fetch_fema_nfhl_zones" not in _ranked("flood zones", index)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"need": "bathymetry", "query": "depth"}, {"query": "  "}])
async def test_neither_or_both_of_need_and_query_refuses_naming_both(args):
    with pytest.raises(FindSourcesAskError, match="exactly one of need"):
        await find_sources(**args)


def test_the_params_line_marks_required_enums_and_types(registry):
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    assert params_line(get_spec("fetch_fema_nfhl_zones")).startswith("bbox*, zone")
    lines = [params_line(get_spec(n)) for n in ("fetch_usgs_nwis_gauges", "fetch_nws_event")]
    assert all(lines) and "*" in lines[0] + lines[1]
