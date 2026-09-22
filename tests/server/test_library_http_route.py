"""THE LIBRARY over HTTP: the listing, the search, and the route table itself.

Offline against the live registries: every registered tool reaches the listing
under a subsystem, every coverage row reaches it under its own class and kind
with its fetcher beside it, the search answers off the same BM25 corpus the
model routes on, and the dispatcher's 404/405/OPTIONS arms hold."""

from __future__ import annotations

import pytest

from door_client import drive

from trid3nt_server.server.protocol.doors import library as door


def _get(path: str) -> tuple:
    return ("GET", path, None)


def _drive(spec: tuple):
    return drive(*spec)


def _status(out) -> int:
    return out.status


def _body_json(out) -> dict:
    return out.json()


@pytest.fixture
def listing():
    return door.build_library_payload(use_cache=False)


def test_every_registered_tool_is_listed_once(listing):
    """The listing is the registry: no tool missing, none listed twice."""
    from trid3nt_server.tools import TOOL_REGISTRY

    listed = [t["name"] for sub in listing["subsystems"] for t in sub["tools"]]
    assert sorted(listed) == sorted(TOOL_REGISTRY)
    assert len(listed) == len(set(listed))
    assert listing["tool_count"] == len(TOOL_REGISTRY)


def test_a_tool_carries_the_name_a_run_takes_and_its_docstring(listing):
    """A row is enough to run the tool and to read what it does."""
    templates = next(s for s in listing["subsystems"] if s["name"] == "templates")
    row = next(t for t in templates["tools"]
               if t["name"] == "artemis_harbor_agitation")
    assert row["description"].strip()
    assert row["facts"]["tier"] == "template"


def test_subsystems_are_named_and_ordered(listing):
    """Templates lead and fetchers follow, and no tool falls to ``other``."""
    names = [s["name"] for s in listing["subsystems"]]
    assert names[:2] == ["templates", "fetchers"]
    assert "other" not in names


def test_every_coverage_row_is_listed_under_its_own_class_and_kind(listing):
    """No row without a class, and the fetcher is named beside it."""
    from trid3nt_server.tools.search.match import sources_with_coverage

    declared = sorted((name, str(row.data_class), str(row.kind))
                      for name, row in sources_with_coverage())
    listed = sorted(
        (row["fetcher"], cls["name"], kind["name"])
        for cls in listing["classes"]
        for kind in cls["kinds"]
        for row in kind["rows"]
    )
    assert listed == declared


def test_a_data_row_carries_its_cell_window_and_datum(listing):
    """The facts a slot's match sorts on are the facts the reader sees."""
    rows = [row for cls in listing["classes"] for kind in cls["kinds"]
            for row in kind["rows"]]
    tides = next(r for r in rows if r["fetcher"] == "fetch_noaa_coops_tides"
                 and r["name"] == "product=water_level")
    assert tides["facts"]["records"] == "per instant"
    assert tides["facts"]["datum"] == "MLLW"
    assert tides["facts"]["cadence"]
    grid = next(r for r in rows if r["facts"].get("cell_m"))
    assert isinstance(grid["facts"]["cell_m"], (int, float))


def test_two_rows_of_one_fetcher_are_named_by_what_they_are_asked_for(listing):
    """A fetcher serving one class twice is two rows a reader can tell apart."""
    rows = [row for cls in listing["classes"] if cls["name"] == "water level series"
            for kind in cls["kinds"] for row in kind["rows"]
            if row["fetcher"] == "fetch_noaa_coops_tides"]
    assert sorted(r["name"] for r in rows) == [
        "product=predictions", "product=water_level"]


def test_listing_route_serves_the_payload():
    out = _drive(_get("/api/library"))
    assert _status(out) == 200
    assert _body_json(out)["subsystems"]


def test_search_route_answers_both_facets_from_one_ask():
    """One query reaches the tools the model would rank and the rows of the
    class it names -- the only way a covered source is reached at all."""
    out = _drive(_get("/api/library/search?q=tide+gauge+water+level"))
    assert _status(out) == 200
    payload = _body_json(out)
    assert payload["query"] == "tide gauge water level"
    kinds = {hit["kind"] for hit in payload["hits"]}
    assert kinds == {"tool", "row"}
    rows = [h for h in payload["hits"] if h["kind"] == "row"]
    assert {h["group"] for h in rows} >= {"water level series / measured"}
    assert "fetch_noaa_coops_tides" in {h["fetcher"] for h in rows}
    for hit in payload["hits"]:
        assert hit["name"]
        if hit["kind"] == "row":
            assert hit["fetcher"]
            assert " / " in hit["group"]
            assert "score" not in hit


def test_a_class_the_ask_never_names_brings_no_rows():
    """The row half is the class the ask names, not every row there is."""
    payload = _body_json(_drive(_get("/api/library/search?q=water+level")))
    named = {h["group"].split(" / ")[0] for h in payload["hits"]
             if h["kind"] == "row"}
    assert "fuels" not in named
    assert "water level series" in named


def test_search_with_no_query_answers_no_hits():
    """An empty ask is not the whole library; the listing route serves that."""
    payload = _body_json(_drive(_get("/api/library/search")))
    assert payload == {"query": "", "hits": []}


def test_unknown_path_is_404_and_a_known_path_under_a_bad_method_is_405():
    assert _status(_drive(_get("/api/nothing-here"))) == 404
    out = _drive(("PUT", "/api/library", None))
    assert _status(out) == 405


def test_preflight_is_204_with_the_cors_headers():
    out = _drive(("OPTIONS", "/api/library", None))
    assert _status(out) == 204
    assert out.headers["Access-Control-Allow-Origin"] == "*"


def test_a_route_fault_answers_that_route_s_one_message(monkeypatch):
    """No route hand-writes a 500; the table's failure message is the body."""
    def _boom(**_kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(door, "build_library_payload", _boom)
    out = _drive(_get("/api/library"))
    assert _status(out) == 500
    assert _body_json(out) == {"error": "library listing failed"}
