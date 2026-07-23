"""Unit tests for the ``search_spatial_functions`` atomic tool (ADR 0019, lane-B).

Coverage:
1. Registration: tool present in ``TOOL_REGISTRY`` with the expected
   metadata (``cacheable=False``, ``ttl_class="live-no-cache"``).
2. Vendored data file loads and is non-trivially sized (the DuckDB
   ``spatial`` extension's ``ST_*`` catalog).
3. Model-free retrieval check (hard rule - corpus before acceptance): a
   handful of canonical free-text asks route to the expected ``ST_*``
   function without any LLM in the loop.
4. ``top_k`` is honored; empty/whitespace query returns ``{"results": []}``
   without raising.
5. Exact function-name query (e.g. "ST_Buffer") still resolves via the
   substring fallback even with BM25 unavailable.
6. Corpus (``tool_query_corpus.yaml``) carries a
   ``search_spatial_functions`` entry with example queries.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.discovery import search_spatial_functions as ssf


@pytest.fixture()
def fresh_index():
    ssf._reset_index_for_tests()
    yield
    ssf._reset_index_for_tests()


def test_registered_with_expected_metadata():
    entry = TOOL_REGISTRY["search_spatial_functions"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"


def test_vendored_data_file_loads_nontrivially(fresh_index):
    index = ssf._build_index()
    assert len(index.entries) >= 100, (
        f"expected the vendored DuckDB spatial function catalog to carry "
        f"100+ entries; got {len(index.entries)}"
    )
    sample = index.entries[0]
    assert sample["function"].upper().startswith("ST_")
    assert sample["signature"]


#: Model-free retrieval check (hard rule): canonical free-text asks -> the
#: ST_* function a human would expect, proven WITHOUT any LLM in the loop.
_RETRIEVAL_CASES = [
    ("distance between two points", "ST_Distance"),
    ("buffer a polygon", "ST_Buffer"),
    ("centroid of a polygon", "ST_Centroid"),
    ("area of a geometry", "ST_Area"),
    ("intersection of two geometries", "ST_Intersection"),
]


@pytest.mark.parametrize("query, expected_function", _RETRIEVAL_CASES)
def test_model_free_retrieval(fresh_index, query, expected_function):
    res = asyncio.run(ssf.search_spatial_functions(query=query, top_k=5))
    names = [r["function"] for r in res["results"]]
    assert expected_function in names, f"{query!r} -> {names}"


def test_top_k_honored(fresh_index):
    res = asyncio.run(ssf.search_spatial_functions(query="geometry", top_k=2))
    assert len(res["results"]) <= 2


def test_empty_query_returns_empty_results(fresh_index):
    res = asyncio.run(ssf.search_spatial_functions(query="   "))
    assert res == {"results": []}


def test_exact_function_name_resolves_without_bm25(fresh_index, monkeypatch):
    index = ssf._get_index()
    monkeypatch.setattr(index, "bm25", None)
    res = asyncio.run(ssf.search_spatial_functions(query="ST_Buffer", top_k=5))
    names = [r["function"] for r in res["results"]]
    assert "ST_Buffer" in names


def test_corpus_has_search_spatial_functions_entry():
    from trid3nt_server.tools.discovery.search_tools import _load_corpus

    corpus = _load_corpus()
    assert "search_spatial_functions" in corpus
    assert len(corpus["search_spatial_functions"]) >= 5
