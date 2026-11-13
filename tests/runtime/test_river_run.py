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
_BODIES = _FIXTURES / "nhd_waterbody_at_point"

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
    ("fetch_nhd_waterbody_at_point", (-84.72, 34.166)): _BODIES / "allatoona_lake.fgb",
}


#: A point on Henry Hagg Lake, the Hagg Lake canaries' seed.
_HAGG_LAKE = [-123.221649, 45.485595]


@pytest.mark.parametrize("seed, water, kind", [
    (_ETOWAH_RELEASE, _SURFACE / "etowah_release_2km.fgb", "flowline"),
    (_WILLAMETTE_PORTLAND, _SURFACE / "willamette_portland_1km.fgb", "flowline"),
    (_ALLATOONA_LAKE, _SURFACE / "allatoona_lake_3km.fgb", ""),
    (_ALLATOONA_LAKE, _BODIES / "allatoona_lake.fgb", "waterbody"),
    (_HAGG_LAKE, _BODIES / "hagg_lake.fgb", "waterbody"),
    (_ETOWAH_RELEASE, _BODIES / "allatoona_lake.fgb", ""),
])
def test_a_seed_is_the_kind_of_the_mapped_water_it_stands_in(seed, water, kind):
    assert kind_under(str(water), seed) == kind


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


def test_a_bare_point_on_a_reservoir_on_a_river_is_a_lake_run(asked):
    # Allatoona Lake is the Etowah dammed, and NHD maps its pool as a LakePond, not as the river's water.
    env = fill._Env(run={"release": list(_ALLATOONA_LAKE)}, data={})
    row = _domain_row(3.0)
    assert asyncio.run(fill._kind_of_the_water_under(env, row)).kind == "waterbody"
    assert [runner for runner, _ in asked] == ["fetch_nhd_water_surface", "fetch_nhd_waterbody_at_point"]


def test_a_lake_run_s_domain_is_the_waterbody_it_stands_in(asked):
    env = fill._Env(run={"release": list(_ALLATOONA_LAKE)}, data={})
    found = asyncio.run(fill._matched(env, _domain_row(30.0)))
    assert [runner for runner, _ in asked] == [
        "fetch_nhd_water_surface", "fetch_nhd_waterbody_at_point", "fetch_nhd_waterbody_at_point"]
    from shapely.geometry import Point, shape

    from trid3nt_server.inputs.geometry import read_geometry_doc

    lake = shape(read_geometry_doc(str(_BODIES / "allatoona_lake.fgb"))["features"][0]["geometry"])
    assert shape(found.geometry).covers(Point(*_ALLATOONA_LAKE))
    # The search box (30 km around the seed) holds the whole reservoir, so nothing of it is cut.
    assert shape(found.geometry).area == pytest.approx(lake.area, rel=1e-3)
    assert "centerline" not in found.companions
    assert not any(runner == "fetch_nhdplus_nldi_navigate" for runner, _ in asked)


def test_a_typed_pick_is_read_and_the_water_under_it_never_asked(asked):
    env = fill._Env(run={"release": {"lon": _ALLATOONA_LAKE[0], "lat": _ALLATOONA_LAKE[1],
                                     "place_type": "lake"}}, data={})
    row = _domain_row(3.0)
    seed = asyncio.run(fill._seed(env, row))
    assert fill._asked_of(row, seed) == "waterbody"


def _spanless_row() -> Input:
    return dataclasses.replace(Input(DOMAIN).need("hydrography", at="release"), name="domain")


@pytest.fixture
def mode(monkeypatch):
    """The AUTO setting, and the review card a person answers: what it showed, and the reach they give."""
    from trid3nt_server.inputs.gate import input_review

    held = {"mode": "auto", "answer": None, "cards": []}
    monkeypatch.setattr(input_review, "resolve_input_gate_mode", lambda forced=None: forced or held["mode"])

    async def review(*, tool_name, entries, params, present=None, **_):
        held["cards"].append(await present())
        given = {} if held["answer"] is None else {"reach_length_km": held["answer"]}
        return input_review.ReviewOutcome(proceed=True, entries=[], params={**params, **given},
                                          mode="user_gated")

    monkeypatch.setattr(input_review, "gate_input_review", review)
    return held


def test_auto_refuses_a_river_run_whose_reach_nobody_states(asked, mode):
    env = fill._Env(run={"release": list(_ETOWAH_RELEASE)}, data={})
    with pytest.raises(fill.StepFailedError) as refused:
        asyncio.run(fill._matched(env, _spanless_row()))
    assert refused.value.error_code == "REACH_LENGTH_UNSTATED"
    assert "reach_length_km is unstated" in str(refused.value)
    # NLDI's own default walk is never taken in its place.
    assert [runner for runner, _ in asked] == ["fetch_nhd_water_surface"]
    assert mode["cards"] == []


def test_a_person_reviewing_is_asked_the_reach_and_the_river_is_walked_that_far(asked, mode):
    mode.update(mode="user_gated", answer=2.0)
    env = fill._Env(run={"release": list(_ETOWAH_RELEASE)}, data={})
    found = asyncio.run(fill._matched(env, _spanless_row()))
    card, = mode["cards"]
    row, = card.param_sheet.rows
    assert row.name == "reach_length_km" and row.value is None and row.units == "km"
    assert "walked downstream" in row.desc
    assert env.run["reach_length_km"] == 2.0
    assert [kw["distance_km"] for runner, kw in asked if runner == "fetch_nhdplus_nldi_navigate"] == [2.0]
    assert "centerline" in found.companions


def test_a_card_answered_with_no_reach_refuses_naming_it(asked, mode):
    mode.update(mode="user_gated", answer=None)
    env = fill._Env(run={"release": list(_ETOWAH_RELEASE)}, data={})
    with pytest.raises(fill.StepFailedError) as refused:
        asyncio.run(fill._matched(env, _spanless_row()))
    assert refused.value.error_code == "REACH_LENGTH_UNSTATED"
    assert len(mode["cards"]) == 1


def test_a_stated_reach_or_a_template_s_own_span_asks_nothing(asked, mode):
    mode.update(mode="user_gated", answer=99.0)
    stated = fill._Env(run={"release": list(_ETOWAH_RELEASE), "reach_length_km": 2.0}, data={})
    asyncio.run(fill._matched(stated, _spanless_row()))
    asyncio.run(fill._matched(fill._Env(run={"release": list(_ETOWAH_RELEASE)}, data={}), _domain_row(2.0)))
    assert mode["cards"] == []
