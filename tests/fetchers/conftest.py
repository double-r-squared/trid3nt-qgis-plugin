"""Shared checks for the fetchers slice.

A fetcher with a row is FOUND, never ranked by phrase: it carries no
corpus of its own, so what a test of its retrieval asserts is that its CLASS's
phrasings reach ``find_sources`` and that the source stands under that class.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


@pytest.fixture(scope="session")
def class_routes_to_the_match():
    from trid3nt_server.tools.search import find_sources as pkg
    from trid3nt_server.tools.search.find_sources.find_sources import FIND_SOURCES
    from trid3nt_server.tools.search.match import covered_sources
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    path = pathlib.Path(pkg.__file__).resolve().parent / "corpus.yaml"
    corpus = yaml.safe_load(path.read_text()) or {}

    def check(data_class: str, fetcher: str) -> None:
        dd._get_index()
        assert fetcher in covered_sources(), (
            f"{fetcher} states no row, so no class finds it")
        queries = corpus.get(data_class)
        assert queries, f"the class {data_class!r} carries no phrasings"
        missed = [q for q in queries
                  if FIND_SOURCES not in retrieve_visible_tools(q, None, 8)]
        assert not missed, (
            f"{data_class!r} phrasings that reach no match, so {fetcher} is "
            f"unreachable: {missed}")

    return check


_GRIDS = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "proj_grids"


@pytest.fixture
def proj_grids(monkeypatch):
    """Point PROJ at one excerpt directory of real grids ("capecod" or "alaska") for a test.

    The grid download is replaced by a failure, so a test that reaches the network fails."""
    import pyproj

    from trid3nt_server.tools.fetchers import _proj_grids

    original = pyproj.datadir.get_data_dir()

    def use(place: str) -> None:
        pyproj.datadir.set_data_dir(original)
        pyproj.datadir.append_data_dir(str(_GRIDS / place))

    def no_network(name, url):
        raise AssertionError(f"the grid {name} was fetched from {url}")

    monkeypatch.setattr(_proj_grids, "_download", no_network)
    yield use
    pyproj.datadir.set_data_dir(original)
