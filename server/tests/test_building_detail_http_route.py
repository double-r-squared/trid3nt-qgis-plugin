"""HTTP-route wiring tests for /api/building-detail on the catalog listener.

Click-to-enrich (NATE 2026-06-27): the building footprint inline GeoJSON is now
SLIM (id-only props); the popup fetches the full tag bag on demand by
``(osm_type, osm_id)`` here. The route reads the cached ``<key>.tags.json``
sidecar; on a miss it falls back to a live Overpass-by-id query. Returns
``{fid, tags:{...}}`` or a typed 404 / 400.

Exercises ``tool_catalog_http._handle_http`` dispatch for ``/api/building-detail``:
  - sidecar HIT -> 200 {fid, tags};
  - sidecar miss -> live Overpass fallback HIT -> 200;
  - both miss -> typed 404;
  - malformed input (bad osm_type / non-numeric osm_id) -> typed 400;
  - the existing /api/tool-catalog path stays unaffected.
"""

from __future__ import annotations

import asyncio
import json

from grace2_agent import tool_catalog_http


class _FakeReader:
    """Feed a single HTTP/1.1 GET request, then EOF."""

    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in self._lines]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""


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


def _request(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: agent.local\r\n"
        "\r\n"
    ).encode()


def _run(coro):
    return asyncio.run(coro)


def _body(out: bytes) -> dict:
    """Split an HTTP response on the header/body boundary, parse the JSON body."""
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def test_building_detail_sidecar_hit(monkeypatch):
    """A sidecar carrying the fid returns 200 {fid, tags} without a live query."""
    tags = {"building": "house", "name": "Maison", "height": "8"}
    monkeypatch.setattr(
        tool_catalog_http, "_read_tags_from_sidecars", lambda fid: dict(tags)
    )

    def _no_live(osm_type, osm_id):  # pragma: no cover -- must not be reached
        raise AssertionError("live Overpass must not run on a sidecar hit")

    monkeypatch.setattr(tool_catalog_http, "_read_tags_from_overpass", _no_live)

    reader = _FakeReader(_request("/api/building-detail?osm_type=way&osm_id=777"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"200 OK" in out
    payload = _body(out)
    assert payload["fid"] == "w777"
    assert payload["tags"] == tags
    assert writer.closed is True


def test_building_detail_falls_back_to_live_overpass(monkeypatch):
    """Sidecar miss -> live Overpass-by-id HIT -> 200 {fid, tags}."""
    monkeypatch.setattr(
        tool_catalog_http, "_read_tags_from_sidecars", lambda fid: None
    )
    live_called: list[tuple] = []

    def _live(osm_type, osm_id):
        live_called.append((osm_type, osm_id))
        return {"building": "commercial"}

    monkeypatch.setattr(tool_catalog_http, "_read_tags_from_overpass", _live)

    reader = _FakeReader(
        _request("/api/building-detail?osm_type=relation&osm_id=222")
    )
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"200 OK" in out
    payload = _body(out)
    assert payload["fid"] == "r222"
    assert payload["tags"] == {"building": "commercial"}
    assert live_called == [("relation", "222")]


def test_building_detail_404_when_both_miss(monkeypatch):
    """Sidecar AND live Overpass both empty -> typed 404 (no fabricated success)."""
    monkeypatch.setattr(
        tool_catalog_http, "_read_tags_from_sidecars", lambda fid: None
    )
    monkeypatch.setattr(
        tool_catalog_http, "_read_tags_from_overpass", lambda t, i: None
    )

    reader = _FakeReader(_request("/api/building-detail?osm_type=way&osm_id=999"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"404 Not Found" in out
    assert writer.closed is True


def test_building_detail_400_on_bad_osm_type(monkeypatch):
    """An unknown osm_type yields a typed 400 (validation, not a 500)."""

    def _no_sidecar(fid):  # pragma: no cover -- validation runs first
        raise AssertionError("sidecar must not run for an invalid request")

    monkeypatch.setattr(tool_catalog_http, "_read_tags_from_sidecars", _no_sidecar)

    reader = _FakeReader(_request("/api/building-detail?osm_type=banana&osm_id=1"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"400 Bad Request" in out


def test_building_detail_400_on_non_numeric_osm_id(monkeypatch):
    """A non-numeric osm_id yields a typed 400."""
    reader = _FakeReader(
        _request("/api/building-detail?osm_type=way&osm_id=notanumber")
    )
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"400 Bad Request" in out


def test_building_detail_does_not_perturb_catalog(monkeypatch):
    """The new route must not break the sibling tool-catalog route."""
    reader = _FakeReader(_request("/api/tool-catalog"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"200 OK" in out
