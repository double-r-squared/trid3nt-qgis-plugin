"""A bare point in a river is a river run.

Offline, on recorded real answers: the water surface NHD maps around a seed decides
whether the domain is the reach walked downstream from it (the seed stands IN a
StreamRiver area) or the domain the match finds as before (a lake beside a river)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from trid3nt_server.inputs.bound_domain import bind_domain, reset_domain
from trid3nt_server.inputs.domain import kind_under
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import DOMAIN
from trid3nt_server.workflows.runtime import fill

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SURFACE = _FIXTURES / "nhd_water_surface"

#: The Etowah release, 224 m above USGS 02395000 Kingston.
_ETOWAH_RELEASE = [-84.978229605, 34.207328998]
#: A point on Allatoona Lake, 1.03 km above the dam from the Etowah's own StreamRiver area.
_ALLATOONA_LAKE = [-84.72, 34.166]
#: The Willamette at Portland, a hundred metres off USGS 14211720.
_WILLAMETTE_PORTLAND = [-122.669784, 45.518485]

#: What each row answered for the asks below, recorded.
_ANSWERS = {
    ("fetch_nhd_water_surface", (-84.978229605, 34.207328998)): _SURFACE / "etowah_release_2km.fgb",
    ("fetch_nhd_water_surface", (-84.72, 34.166)): _SURFACE / "allatoona_lake_3km.fgb",
    ("fetch_nhdplus_nldi_navigate", (-84.978229605, 34.207328998)):
        _FIXTURES / "nhdplus_nldi" / "etowah_release_dm_2km.fgb",
}


@pytest.mark.parametrize("seed, water, kind", [
    (_ETOWAH_RELEASE, "etowah_release_2km.fgb", "flowline"),
    (_WILLAMETTE_PORTLAND, "willamette_portland_1km.fgb", "flowline"),
    (_ALLATOONA_LAKE, "allatoona_lake_3km.fgb", ""),
])
def test_a_seed_is_a_place_on_a_flowline_only_inside_a_streamriver_area(seed, water, kind):
    assert kind_under(str(_SURFACE / water), seed) == kind


@pytest.fixture
def asked(monkeypatch):
    """The rows the fill called, answered from the recordings."""
    calls: list[tuple[str, dict]] = []

    async def _runner(runner, kwargs, label):
        calls.append((runner, dict(kwargs)))
        seed = tuple(float(v) for v in kwargs.get("seed_point") or ())
        found = _ANSWERS.get((runner, seed))
        if found is None:
            raise AssertionError(f"no recorded answer for {runner} {kwargs}")
        return str(found)

    monkeypatch.setattr(fill, "_call_runner", _runner)
    token = bind_domain(None)
    try:
        yield calls
    finally:
        reset_domain(token)


def _domain_row(span_km: float) -> Input:
    return dataclasses.replace(
        Input(DOMAIN).need("hydrography", at="release", span_km=span_km), name="domain")


def test_a_bare_release_in_a_river_meshes_the_reach_walked_downstream(asked):
    env = fill._Env(run={"release": list(_ETOWAH_RELEASE)}, data={})
    found = asyncio.run(fill._matched(env, _domain_row(2.0)))
    assert [runner for runner, _ in asked] == [
        "fetch_nhd_water_surface", "fetch_nhd_water_surface", "fetch_nhdplus_nldi_navigate"]
    assert asked[2][1]["distance_km"] == 2.0
    assert "centerline" in found.companions
    assert sorted(run.type for run in found.runs) == ["inflow", "outflow"]

    from pyproj import Geod
    from shapely.geometry import shape

    line = shape(found.companions["centerline"])
    length_m = Geod(ellps="WGS84").geometry_length(line)
    # NLDI counts its 2 km from the head of the release's reach, so the line from the release ends a little short.
    assert 1900.0 < length_m <= 2005.0
    assert line.coords[0] == pytest.approx(tuple(_ETOWAH_RELEASE), abs=1e-3)


def test_a_bare_point_on_a_lake_beside_a_river_is_not_made_a_river_run(asked):
    env = fill._Env(run={"release": list(_ALLATOONA_LAKE)}, data={})
    row = _domain_row(3.0)
    assert asyncio.run(fill._kind_of_the_water_under(env, row)) is row
    assert [runner for runner, _ in asked] == ["fetch_nhd_water_surface"]


def test_a_typed_pick_is_read_and_the_water_under_it_never_asked(asked):
    env = fill._Env(run={"release": {"lon": _ALLATOONA_LAKE[0], "lat": _ALLATOONA_LAKE[1],
                                     "place_type": "lake"}}, data={})
    row = _domain_row(3.0)
    seed = asyncio.run(fill._seed(env, row))
    assert fill._asked_of(row, seed) == "waterbody"
