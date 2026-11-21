"""A walk from a seed point covers the stated distance measured FROM THE POINT, not from the seed flowline's start.

Offline: NLDI's recorded answers for the Missouri at Omaha, the release 6.6 km down the 10.07 km flowline 17219468."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt.inputs.domain import _clipped, _one_line, above_on, flowline_ids
from trid3nt.inputs.user_input import UserInputError
from trid3nt.tools.fetchers._router.registration import get_spec
from trid3nt.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import hooks

_NLDI = Path(__file__).resolve().parents[1] / "fixtures" / "nhdplus_nldi"
_RELEASE = (-95.921233757, 41.257659140)
_OMAHA = (-95.92325, 41.25905556)


def _recorded(name):
    return json.loads((_NLDI / name).read_text())


@pytest.fixture
def asked(monkeypatch):
    import dataretrieval.nldi as nldi

    distances: list[float] = []

    def _query(url, query_params):
        if url.endswith("/linked-data/"):
            return _recorded("linked_data_sources.json")
        if url.endswith("/comid/position"):
            return _recorded("missouri_snap.json")
        way = url.split("/")[-2].lower()
        distance = float(query_params["distance"])
        distances.append(distance)
        if distance == 0.0:
            return _recorded(f"missouri_{way}_0_0km.json")
        assert 17.0 < distance < 17.2
        return _recorded(f"missouri_{way}_17_07km.json")

    monkeypatch.setattr(nldi, "_query_nldi", _query)
    monkeypatch.setattr(nldi, "_AVAILABLE_DATA_SOURCES", None)
    return distances


def _walk(direction, km):
    found = hooks.features(get_spec("fetch_nhdplus_nldi_navigate"),
                           {"seed_point": list(_RELEASE), "direction": direction, "distance_km": km}, timeout_s=30.0)
    return {"type": "FeatureCollection", "features": found}


def test_the_walk_asks_the_stated_distance_plus_the_seed_flowlines_own_length(asked):
    _walk("DM", 7.0)
    assert asked[0] == 0.0 and asked[1] == pytest.approx(7.0 + 10.07, abs=0.05)


def test_the_cut_reaches_the_stated_length_below_the_release(asked):
    line = _one_line(_walk("DM", 7.0), "domain", "X")
    cut = _clipped(line, _RELEASE, 7.0, "domain", "X")
    from pyproj import Geod
    xs, ys = zip(*cut.coords)
    assert Geod(ellps="WGS84").line_length(xs, ys) == pytest.approx(7000.0, abs=15.0)


def test_the_upstream_walk_goes_past_the_seed_flowline_and_omaha_is_above_the_release(asked):
    walked = _walk("UM", 7.0)
    assert 17219460 in flowline_ids(walked)
    assert above_on(walked, 17219468, _OMAHA, _RELEASE)


def test_a_reach_past_the_end_of_the_walked_network_refuses_naming_both_lengths(asked):
    line = _one_line(_walk("DM", 7.0), "domain", "X")
    with pytest.raises(UserInputError) as refused:
        _clipped(line, _RELEASE, 40.0, "domain", "X")
    assert refused.value.error_code == "DOMAIN_REACH_PAST_NETWORK_END"
    assert "40 km" in str(refused.value) and "runs only" in str(refused.value)


def test_the_cut_starts_one_stated_length_above_the_release(asked):
    # The inflow face is meshed on the rim, so a source at the release stands off it: the 40 m mesh edge above.
    from pyproj import Geod
    from shapely.geometry import LineString, Point

    line = _one_line(_walk("DM", 7.0), "domain", "X")
    cut = _clipped(line, _RELEASE, 7.0, "domain", "X", 40.0)
    xs, ys = zip(*cut.coords)
    geod = Geod(ellps="WGS84")
    assert geod.line_length(xs, ys) == pytest.approx(7040.0, abs=15.0)
    to_release = LineString(cut.coords).project(Point(*_RELEASE), normalized=True) * geod.line_length(xs, ys)
    assert to_release == pytest.approx(40.0, abs=5.0)


def test_a_cut_reaching_above_the_start_of_the_walked_network_refuses(asked):
    line = _one_line(_walk("DM", 7.0), "domain", "X")
    with pytest.raises(UserInputError) as refused:
        _clipped(line, _RELEASE, 7.0, "domain", "X", 7000.0)
    assert refused.value.error_code == "DOMAIN_SEED_AT_NETWORK_START"
