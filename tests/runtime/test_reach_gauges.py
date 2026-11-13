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
_SITES = {
    "02395000": _NLDI / "nwis_02395000_dm_0km.fgb",
    "02394682": _NLDI / "nwis_02394682_dm_0km.fgb",
}


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


def _env(water):
    reach = Domain(dict(water), companions={"centerline": {
        "type": "LineString", "coordinates": [_ETOWAH_RELEASE, [_ETOWAH_RELEASE[0] - 0.01, _ETOWAH_RELEASE[1]]]}})
    row = dataclasses.replace(Input(DOMAIN).need("hydrography", at="release", span_km=6.0), name="domain")
    env = fill._Env(run=dict(release=list(_ETOWAH_RELEASE), domain=reach, reach_length_km=24.0,
                             event_time="2026-09-21T23:00:00Z"),
                    data={"domain": row}, moment="event_time",
                    slot_units={LEVEL: "m", DISCHARGE: "m3/s"})
    return env, bind_domain(Bound(bbox=reach.bbox, geometry=dict(water), label="reach"))


def _read(env, slot, need):
    row = next(r for r in get_spec("fetch_usgs_nwis_gauges").coverage if r.data_class == need)
    gauge = dataclasses.replace(Input(slot).need(need), name=slot.name)
    return asyncio.run(fill._ingested(env, gauge, str(_GAUGES), row))


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
