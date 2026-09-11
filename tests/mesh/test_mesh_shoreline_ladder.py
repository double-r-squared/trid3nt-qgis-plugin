"""The shoreline ladder: which substrate the water is cut from, and when it refuses.

Offline: the OSM rung's fetch is stubbed with coastline walks written here, so
what is under test is the LADDER - the rung the ask resolves to, the land the
left-hand rule derives, and the refusals - and never the network.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh import shoreline as SH

#: A degree box a metre or two on each side of a straight north-south coast.
_BBOX = (-71.52, 41.34, -71.49, 41.37)


def _gshhg(tmp_path, name="GSHHS_i_L1.shp"):
    path = tmp_path / "shoreline" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"shp")
    return path


def _stub_fetch(monkeypatch, walks):
    """The OSM rung's fetch, answering with the walks this test declares."""
    doc = {"type": "FeatureCollection",
           "features": [{"type": "Feature", "properties": {},
                         "geometry": {"type": "LineString",
                                      "coordinates": [list(p) for p in walk]}}
                        for walk in walks]}
    monkeypatch.setattr(SH, "_coastline_walks", lambda layer: [
        [tuple(p) for p in walk] for walk in walks])

    class _Registry(dict):
        def __getitem__(self, name):
            return type("Tool", (), {"fn": staticmethod(lambda **kw: doc)})

    monkeypatch.setattr("trid3nt_server.tools.TOOL_REGISTRY", _Registry())
    return doc


#: A coastline crossing the box south to north. Walking NORTH puts the LEFT -
#: the land - on the WEST side.
_NORTHWARD = [(-71.505, 41.33), (-71.505, 41.38)]


def test_a_coarse_ask_is_served_by_the_local_file_without_a_fetch(
        monkeypatch, tmp_path):
    """The COARSEST rung that resolves the ask serves it: a shoreline far finer
    than the mesh is detail the triangulator throws away and a fetch nobody
    needed."""
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(_gshhg(tmp_path)))

    def no_fetch(*_a, **_k):
        raise AssertionError("the coarse ask must not reach the fetcher")

    monkeypatch.setattr(SH, "_coastline_walks", no_fetch)
    served = SH.resolve_shoreline(_BBOX, 2000.0, tmp_path)
    assert served.rung.startswith("gshhg")
    assert served.path.name == "GSHHS_i_L1.shp"
    assert "1000 m nominal against a 2000 m ask" in served.note


def test_a_harbour_ask_climbs_past_a_shoreline_that_cannot_describe_it(
        monkeypatch, tmp_path):
    """The intermediate GSHHG resolves 1 km. At a 25 m ask it is not a coarser
    answer, it is scraps - so the ladder climbs to the rung that can."""
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(_gshhg(tmp_path)))
    _stub_fetch(monkeypatch, [_NORTHWARD])
    served = SH.resolve_shoreline(_BBOX, 25.0, tmp_path)
    assert served.rung == "fetch_osm_coastline"
    doc = json.loads(served.path.read_text())
    assert doc["features"] and doc["features"][0]["geometry"]["type"] == "Polygon"


def test_an_ask_no_rung_resolves_refuses_naming_every_rung(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(_gshhg(tmp_path)))
    with pytest.raises(MeshToolError) as excinfo:
        SH.resolve_shoreline(_BBOX, 2.0, tmp_path)
    assert excinfo.value.error_code == "MESH_SHORELINE_UNAVAILABLE"
    message = str(excinfo.value)
    assert "fetch_osm_coastline resolves 10 m" in message
    assert "gshhg GSHHS_i_L1.shp resolves 1000 m" in message
    assert "TRID3NT_GSHHG_SHP" in message


def test_an_unset_shoreline_variable_is_named_rather_than_exported(
        monkeypatch, tmp_path):
    """Nothing sets TRID3NT_GSHHG_SHP on a caller's behalf. A rung that is not on
    the box states no resolution, so it is walked LAST and what it contributes to
    the refusal is the name of the variable that would have brought it."""
    monkeypatch.delenv("TRID3NT_GSHHG_SHP", raising=False)
    with pytest.raises(MeshToolError) as excinfo:
        SH.resolve_shoreline(_BBOX, 2.0, tmp_path)
    assert excinfo.value.error_code == "MESH_SHORELINE_UNAVAILABLE"
    assert "TRID3NT_GSHHG_SHP is unset" in str(excinfo.value)


def test_an_inland_extent_the_fetcher_maps_no_coastline_in_hands_over(
        monkeypatch, tmp_path):
    """A lake is natural=water and carries no coastline way. The rung says so and
    the ladder falls through it rather than meshing the streets as open water."""
    monkeypatch.delenv("TRID3NT_GSHHG_SHP", raising=False)
    _stub_fetch(monkeypatch, [])
    with pytest.raises(MeshToolError) as excinfo:
        SH.resolve_shoreline(_BBOX, 25.0, tmp_path)
    message = str(excinfo.value)
    assert "maps no coastline" in message
    assert "TRID3NT_GSHHG_SHP is unset" in message


def test_the_land_is_on_the_left_of_the_way_the_way_was_drawn():
    """OSM's own convention IS the classification, and reversing a way would put
    the sea where the town is."""
    from shapely.geometry import shape

    north = SH.land_polygons([_NORTHWARD], _BBOX)
    south = SH.land_polygons([list(reversed(_NORTHWARD))], _BBOX)
    assert len(north) == 1 and len(south) == 1
    # Walking north, the land is WEST: its centroid sits left of the coast line.
    assert shape(north[0]).centroid.x < -71.505
    assert shape(south[0]).centroid.x > -71.505


def test_a_way_that_ends_inside_the_extent_refuses_rather_than_guessing():
    dangling = [(-71.505, 41.33), (-71.505, 41.355)]
    with pytest.raises(MeshToolError) as excinfo:
        SH.land_polygons([dangling], _BBOX)
    assert excinfo.value.error_code == "MESH_SHORELINE_DOES_NOT_CLOSE"


def test_an_island_inside_the_extent_is_land_and_the_rest_is_water():
    """A closed way is its own ring: drawn counter-clockwise, the left of every
    segment is its interior, which is the island."""
    from shapely.geometry import shape

    island = [(-71.510, 41.350), (-71.500, 41.350), (-71.500, 41.360),
              (-71.510, 41.360), (-71.510, 41.350)]
    land = SH.land_polygons([island], _BBOX)
    assert len(land) == 1
    assert shape(land[0]).area == pytest.approx(0.0001, rel=1e-6)


def test_the_coastline_fetcher_surfaces_in_top8():
    """Corpus first: the harbour-scale rung is reachable from its own phrasings
    before anything is accepted on it."""
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    queries = dd._load_corpus().get("fetch_osm_coastline", [])
    assert queries, "fetch_osm_coastline has NO corpus queries"
    assert any("fetch_osm_coastline" in retrieve_visible_tools(q, None, 8)
               for q in queries), (
        "fetch_osm_coastline surfaces in NO top-8 for any of its corpus queries")
