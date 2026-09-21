"""THE LIBRARY over HTTP: the listing, the search, and the route table itself.

Offline against the live registries: every registered tool reaches the listing
under a subsystem, every coverage row reaches it under its own class and kind
with its fetcher beside it, the search answers off the same BM25 corpus the
model routes on, and the dispatcher's 404/405/OPTIONS arms hold."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.server.protocol import catalog_http as door


class _FakeReader:
    """Feed one raw HTTP/1.1 request, then EOF."""

    def __init__(self, request: bytes):
        self._data = request
        self._pos = 0

    async def readline(self):
        idx = self._data.find(b"\n", self._pos)
        if idx == -1:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:idx + 1]
        self._pos = idx + 1
        return chunk

    async def readexactly(self, n: int):
        if len(self._data) - self._pos < n:
            raise asyncio.IncompleteReadError(b"", n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _drive(request: bytes) -> bytes:
    reader = _FakeReader(request)
    writer = _FakeWriter()
    asyncio.run(door._handle_http(reader, writer))
    assert writer.closed is True
    return bytes(writer.buffer)


def _get(path: str) -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n".encode()


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _body_json(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


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


def test_search_route_answers_both_facets_off_the_model_corpus():
    """One query, one index: the ranked tools and the rows they answer."""
    out = _drive(_get("/api/library/search?q=tide+gauge+water+level"))
    assert _status(out) == 200
    payload = _body_json(out)
    assert payload["query"] == "tide gauge water level"
    kinds = {hit["kind"] for hit in payload["hits"]}
    assert "tool" in kinds
    for hit in payload["hits"]:
        assert hit["name"]
        if hit["kind"] == "row":
            assert hit["fetcher"]
            assert " / " in hit["group"]


def test_search_with_no_query_answers_no_hits():
    """An empty ask is not the whole library; the listing route serves that."""
    payload = _body_json(_drive(_get("/api/library/search")))
    assert payload == {"query": "", "hits": []}


def test_unknown_path_is_404_and_a_known_path_under_a_bad_method_is_405():
    assert _status(_drive(_get("/api/nothing-here"))) == 404
    out = _drive(b"PUT /api/library HTTP/1.1\r\nHost: agent.local\r\n\r\n")
    assert _status(out) == 405


def test_preflight_is_204_with_the_cors_headers():
    out = _drive(b"OPTIONS /api/library HTTP/1.1\r\nHost: agent.local\r\n\r\n")
    assert _status(out) == 204
    assert b"Access-Control-Allow-Origin: *" in out


def test_a_route_fault_answers_that_route_s_one_message(monkeypatch):
    """No route hand-writes a 500; the table's failure message is the body."""
    def _boom(**_kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(door, "build_library_payload", _boom)
    out = _drive(_get("/api/library"))
    assert _status(out) == 500
    assert _body_json(out) == {"error": "library listing failed"}
