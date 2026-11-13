"""A gauge belongs to a river run's reach only on the reach's own water.

Offline, on recorded real answers: the USGS gauge layer for the Etowah box, NLDI's
main stem walked 24 km up and down from the release, and the reach NLDI's own NWIS
index lists each gauge on. Kingston 02395000 is on the stem; Richland Creek
02394682, up a tributary, is not, so it is never taken even standing inside the domain."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from shapely.geometry import box, mapping

from trid3nt_server.inputs.bound_domain import Domain as Bound, bind_domain, reset_domain
from trid3nt_server.inputs.domain import Domain
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.observation import ObservationError, observation
from trid3nt_server.inputs.slots import DISCHARGE, DOMAIN, LEVEL
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.workflows.runtime import fill

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_NLDI = _FIXTURES / "nhdplus_nldi"
_GAUGES = _FIXTURES / "usgs_nwis_gauges" / "etowah_bbox_2026-09-21.fgb"

_ETOWAH_RELEASE = [-84.978229605, 34.207328998]
#: Where NLDI's NWIS index places each gauge, rounded: the domains below are drawn around them.
_KINGSTON = (-84.979, 34.209)
_RICHLAND = (-84.854, 34.079)

_WALKS = {
    "UM": _NLDI / "etowah_release_um_24km.fgb",
    "DM": _NLDI / "etowah_release_dm_24km.fgb",
}
#: Every site of the recorded layer: a discharge gauge outside the domain is still asked for its reach, since one
#: above the release on the UM stem is the inflow's.
_SITES = {site: _NLDI / f"nwis_{site}_dm_0km.fgb" for site in (
    "02395000", "02394682", "02394670", "02394000", "02393501", "02395120", "02388350", "02388500", "02388525",
    "02395996", "02395980", "02394820")}


def _around(*points, pad=0.01):
    return mapping(box(*[min(p[0] for p in points) - pad, min(p[1] for p in points) - pad,
                         max(p[0] for p in points) + pad, max(p[1] for p in points) + pad]))


@pytest.fixture
def asked(monkeypatch):
    calls: list[dict] = []

    async def _runner(runner, kwargs, label):
        assert runner == "fetch_nhdplus_nldi_navigate", runner
        calls.append(dict(kwargs))
        if "nwis_site" in kwargs:
            assert kwargs["distance_km"] == 0.0
            return str(_SITES[kwargs["nwis_site"]])
        assert kwargs["seed_point"] == _ETOWAH_RELEASE and kwargs["distance_km"] == 24.0
        return str(_WALKS[kwargs["direction"]])

    monkeypatch.setattr(fill, "_call_runner", _runner)
    return calls


def _env(water, release=_ETOWAH_RELEASE, reach_km=24.0, at="2026-09-21T23:00:00Z"):
    reach = Domain(dict(water), companions={"centerline": {
        "type": "LineString", "coordinates": [release, [release[0] - 0.01, release[1]]]}})
    row = dataclasses.replace(Input(DOMAIN).need("hydrography", at="release", span_km=6.0), name="domain")
    env = fill._Env(run=dict(release=list(release), domain=reach, reach_length_km=reach_km,
                             event_time=at),
                    data={"domain": row}, moment="event_time",
                    slot_units={LEVEL: "m", DISCHARGE: "m3/s"})
    return env, bind_domain(Bound(bbox=reach.bbox, geometry=dict(water), label="reach"))


def _read(env, slot, need, gauges=_GAUGES):
    row = next(r for r in get_spec("fetch_usgs_nwis_gauges").coverage if r.data_class == need)
    gauge = dataclasses.replace(Input(slot).need(need), name=slot.name)
    return asyncio.run(fill._ingested(env, gauge, str(gauges), row))


@pytest.mark.parametrize("slot, need, observed", [
    (LEVEL, "water level series", 186.836),
    (DISCHARGE, "discharge series", 15.008),
])
def test_a_gauge_on_the_main_stem_is_taken_and_one_up_a_tributary_never(asked, slot, need, observed):
    env, token = _env(_around(_KINGSTON, _RICHLAND))
    try:
        off = asyncio.run(fill._off_the_reach(env, str(_GAUGES)))
        found = _read(env, slot, need)
    finally:
        reset_domain(token)
    assert "02395000" not in off
    assert off["02394682"] == ("stands on NHDPlus reach 6498058, which is not on the main stem walked 24 km "
                               "up and down from the release")
    assert found.site_id == "02395000"
    assert found.value == pytest.approx(observed, abs=1e-3)
    walked = {(c.get("direction"), c.get("nwis_site")) for c in asked}
    assert ("UM", None) in walked and ("DM", None) in walked


def test_a_record_whose_only_gauges_are_off_the_reach_refuses_naming_each(asked):
    env, token = _env(_around(_RICHLAND))
    try:
        with pytest.raises(ObservationError) as refused:
            _read(env, DISCHARGE, "discharge series")
    finally:
        reset_domain(token)
    assert refused.value.error_code == "OBSERVATION_OFF_THE_REACH"
    said = str(refused.value)
    assert "RICHLAND CREEK" in said and "02394682" in said and "not on the main stem" in said
    assert "ETOWAH RIVER NEAR KINGSTON,GA (02395000) stands outside the run's domain" in said


def test_off_a_river_run_no_gauge_is_tested(asked):
    env, token = _env(_around(_KINGSTON, _RICHLAND))
    env.run["domain"] = Domain(env.run["domain"].geometry)
    try:
        assert asyncio.run(fill._off_the_reach(env, str(_GAUGES))) == {}
    finally:
        reset_domain(token)
    assert asked == []


def test_a_site_named_off_the_water_is_left_out_of_the_ranking():
    found = observation(str(_GAUGES), near=_ETOWAH_RELEASE, field="discharge_cfs", at="2026-09-21T23:00:00Z",
                        off={"02395000": "is not this reach's"})
    assert found.site_id != "02395000"


#: The Willamette at Portland: USGS 14211720 stands about 120 m above the calibration reach's release, and
#: NLDI lists both on the one 14 km flowline 23815040. A second release 1.1 km above the gauge on that flowline.
_WILLAMETTE_GAUGES = _FIXTURES / "usgs_nwis_gauges" / "willamette_portland_2026-11-10.fgb"
_WILLAMETTE_RELEASE = [-122.669784, 45.518485]
_WILLAMETTE_ABOVE = [-122.667, 45.508]
_WILLAMETTE_AT = "2026-11-10T18:00:00Z"
_WILLAMETTE_WALKS = {
    (tuple(_WILLAMETTE_RELEASE), "UM"): _NLDI / "willamette_release_um_3km.fgb",
    (tuple(_WILLAMETTE_RELEASE), "DM"): _NLDI / "willamette_release_dm_3km.fgb",
    (tuple(_WILLAMETTE_ABOVE), "UM"): _NLDI / "willamette_south_um_3km.fgb",
    (tuple(_WILLAMETTE_ABOVE), "DM"): _NLDI / "willamette_south_dm_3km.fgb",
}


@pytest.fixture
def asked_willamette(monkeypatch):
    calls: list[dict] = []

    async def _runner(runner, kwargs, label):
        assert runner == "fetch_nhdplus_nldi_navigate", runner
        calls.append(dict(kwargs))
        if "nwis_site" in kwargs:
            assert kwargs == {"nwis_site": "14211720", "direction": "DM", "distance_km": 0.0}
            return str(_NLDI / "nwis_14211720_dm_0km.fgb")
        assert kwargs["distance_km"] == 3.0
        return str(_WILLAMETTE_WALKS[(tuple(kwargs["seed_point"]), kwargs["direction"])])

    monkeypatch.setattr(fill, "_call_runner", _runner)
    return calls


def _willamette(release, water):
    return _env(water, release=release, reach_km=3.0, at=_WILLAMETTE_AT)


def test_an_inflow_gauge_above_the_release_is_read_for_the_discharge_alone(asked_willamette):
    # The reach is cut downstream from the release, so the gauge 120 m above it stands outside the domain.
    env, token = _willamette(_WILLAMETTE_RELEASE, box(-122.69, 45.5181, -122.65, 45.54).__geo_interface__)
    try:
        found = _read(env, DISCHARGE, "discharge series", _WILLAMETTE_GAUGES)
        level = asyncio.run(fill._off_the_reach(env, str(_WILLAMETTE_GAUGES)))
    finally:
        reset_domain(token)
    assert found.site_id == "14211720"
    assert found.value == pytest.approx(501.208, abs=1e-3)
    # The walks are the runtime's own rows, never inputs the fill accepted: those are read as engine keywords.
    assert asked_willamette and not env.inputs
    # A level or an observation is a target on the solved water: the same gauge is never one.
    assert level == {"14211720": "stands outside the run's domain, the water the reach is cut to"}


def test_a_gauge_below_the_release_outside_the_domain_is_never_the_inflow(asked_willamette):
    # The same flowline, the release 1.1 km above the gauge and the domain stopping short of it.
    env, token = _willamette(_WILLAMETTE_ABOVE, box(-122.69, 45.50, -122.65, 45.512).__geo_interface__)
    try:
        with pytest.raises(ObservationError) as refused:
            _read(env, DISCHARGE, "discharge series", _WILLAMETTE_GAUGES)
    finally:
        reset_domain(token)
    assert refused.value.error_code == "OBSERVATION_OFF_THE_REACH"
    assert ("(14211720) stands outside the run's domain and not above the release on the main stem walked 3 km "
            "upstream") in str(refused.value)


_NWS = _FIXTURES / "nws_river_forecast" / "portland_2026-11-13.fgb"


def test_another_network_s_station_inside_the_reach_is_placed_by_the_domain_alone(asked_willamette):
    # NWS forecast points are not on NLDI's index: inside the reach they stand, outside they are off, and nothing is
    # asked of NLDI for them.
    env, token = _willamette(_WILLAMETTE_ABOVE, box(-122.69, 45.50, -122.65, 45.53).__geo_interface__)
    try:
        off = asyncio.run(fill._off_the_reach(env, str(_NWS), inflow=True))
    finally:
        reset_domain(token)
    assert "PRTO3" not in off
    assert set(off) == {"KCMO3", "MIWO3", "TYCO3", "VAPW1", "FNNO3", "DURO3"}
    assert all(why == "stands outside the run's domain, the water the reach is cut to" for why in off.values())
    assert asked_willamette == []


def test_another_network_s_station_outside_the_reach_is_never_the_inflow(asked_willamette):
    # The release's reach stops short of PRTO3; with no index placing it on the stem it is never read as above it.
    env, token = _willamette(_WILLAMETTE_ABOVE, box(-122.69, 45.50, -122.65, 45.512).__geo_interface__)
    try:
        off = asyncio.run(fill._off_the_reach(env, str(_NWS), inflow=True))
    finally:
        reset_domain(token)
    assert off["PRTO3"] == "stands outside the run's domain, the water the reach is cut to"
    assert asked_willamette == []
