"""``fetch_noaa_coops_datums``: the nearest station publishing MHW AND NAVD88, and the tie it states.

The catalog is read whole and the in-reach stations' datum pages read nearest-first, so a
nearer station publishing only one of the two heights is passed over rather than substituted.
Covered with that: the pass-over itself (the Chignik seed's Sand Point is 0.1 km away and
publishes no NAVD88, while King Cove answers at 119.5 km), the refusals a point with no
station, a pair no station carries, and pages that cannot be read, the record shaped off a
resolved tie, and the offset row's best-effort resolve beside it - guards, tie, candidates
and the catalog failures it swallows rather than failing a pair VDatum could answer."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.hooks import station_tie
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers._router.transport.errors import TransportError
from trid3nt_server.tools.fetchers.ocean.fetch_noaa_coops_datums import hooks as datums

#: The Chignik seed and the stations CO-OPS keeps within 600 km of it, at the
#: coordinates the live catalog publishes. Sand Point is 0.1 km away and states
#: no NAVD88; King Cove at 119.5 km is the nearest that carries both.
_SEED = [-160.505, 55.333]

_CATALOG = [
    {"id": "9459450", "name": "Sand Point", "lng": -160.50433, "lat": 55.331722},
    {"id": "9463502", "name": "Port Moller", "lng": -160.57361, "lat": 55.985832},
    {"id": "9459881", "name": "King Cove", "lng": -162.32611, "lat": 55.05989},
    {"id": "9462620", "name": "Unalaska", "lng": -166.5403, "lat": 53.879196},
    {"id": "9457804", "name": "Alitak", "lng": -154.248, "lat": 56.89739},
    {"id": "9457292", "name": "Kodiak Island", "lng": -152.512, "lat": 57.7317},
    {"id": "9414290", "name": "San Francisco", "lng": -122.46589, "lat": 37.806305},
    {"id": "9447130", "name": "Seattle", "lng": -122.3393, "lat": 47.60264},
]

#: Each station's published heights above its STND, metres, as its own datums
#: page states them: the two no-NAVD88 pages are why the search does not stop
#: at the nearest station.
_PAGES = {
    "9459450": {"NAVD88": None, "MHW": 11.289, "MLLW": 9.301, "MSL": 10.482},
    "9463502": {"NAVD88": None, "MHW": 11.831, "MLLW": 8.9, "MSL": 10.682},
    "9459881": {"NAVD88": 1.385, "MHW": 3.089, "MLLW": 1.218, "MSL": 2.342},
    "9462620": {"NAVD88": 0.712, "MHW": 1.803, "MLLW": 0.792, "MSL": 1.427},
    "9457804": {"NAVD88": None, "MHW": 5.047, "MLLW": 1.715, "MSL": 3.597},
    "9457292": {"NAVD88": 7.97, "MHW": 10.102, "MLLW": 7.702, "MSL": 9.074},
    "9414290": {"NAVD88": 1.804, "MHW": 3.416, "MLLW": 1.263, "MSL": 2.115},
    "9447130": {"NAVD88": 2.437, "MHW": 3.44, "MLLW": 1.36, "MSL": 2.308},
}


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_noaa_coops_datums"]


@pytest.fixture(scope="module")
def offset_spec():
    return compose_specs_from_tree()["fetch_vertical_datum_offset"]


class _Coops:
    """The CO-OPS endpoints under test: which catalog rows and which pages are served, and which reads fail."""

    def __init__(self):
        self.catalog = list(_CATALOG)
        self.pages = dict(_PAGES)
        self.catalog_down = False
        self.unreadable: set[str] = set()


@pytest.fixture
def coops(monkeypatch):
    state = _Coops()

    def fake(_client, url, *, params=None, headers=None):
        if state.catalog_down:
            raise TransportError("catalog unreachable", status=503)
        if "/datums.json" in url:
            station_id = url.split("/stations/")[1].split("/")[0]
            if station_id in state.unreadable:
                raise TransportError(f"datums of {station_id} unreadable",
                                     status=503)
            heights = state.pages.get(station_id)
            if heights is None:
                raise TransportError(f"no page for {station_id}", status=404)
            body = {"epoch": "1983-2001", "units": "meters",
                    "datums": [{"name": name, "value": value}
                               for name, value in heights.items()
                               if value is not None]}
            return json.dumps(body).encode("utf-8"), "application/json", url
        body = {"stations": list(state.catalog)}
        return json.dumps(body).encode("utf-8"), "application/json", url

    monkeypatch.setattr(station_tie, "get_bytes", fake)
    return state


def _tie_params(**over) -> dict:
    asked = {"point": _SEED}
    asked.update(over)
    return asked


def test_the_spec_declares_the_tie_hooks_and_both_endpoints(spec):
    assert spec.hooks.pre_resolve == "station_tie.tie_at"
    assert spec.hooks.record == "noaa_coops_datums.record"
    assert spec.hooks.build_request is None
    assert sorted(spec.endpoints) == ["station_datums", "stations_catalog"]


def test_the_search_passes_over_nearer_stations_that_publish_no_navd88(spec, coops):
    # Sand Point stands 0.1 km from the seed and Port Moller 72.7 km, and
    # neither states NAVD88; King Cove at 119.5 km is the first page with the
    # pair, and the distance that reaches the record is its own.
    tie = station_tie.tie_at(spec, _tie_params())["tie"]
    assert tie["station_id"] == "9459881"
    assert tie["station_name"] == "King Cove"
    assert tie["distance_km"] == 119.5
    assert tie["datums"]["NAVD88"] == 1.385
    assert tie["datums"]["MHW"] == 3.089


def test_a_point_with_no_station_in_reach_refuses_by_name(spec, coops):
    with pytest.raises(RouterInputError) as caught:
        station_tie.tie_at(spec, _tie_params(point=[10.0, 45.0]))
    assert caught.value.error_code == "COOPS_DATUMS_INPUT_INVALID"
    assert "within 500 km" in str(caught.value)


def test_stations_in_reach_without_the_pair_refuse_naming_what_was_checked(spec, coops):
    # The two nearest stations of the seed publish MHW but no NAVD88, and the
    # refusal names both rather than answering with one of them - and names the
    # MSL approximation beside the pair it will not answer with.
    coops.catalog = [row for row in coops.catalog
                     if row["id"] in {"9459450", "9463502"}]
    with pytest.raises(RouterEmptyError) as caught:
        station_tie.tie_at(spec, _tie_params())
    assert caught.value.error_code == "COOPS_DATUMS_NO_TIE"
    message = str(caught.value)
    assert "9459450" in message and "9463502" in message
    assert "MHW and NAVD88" in message
    assert "MSL standing in for the geoid" in message
    assert "never taken without the user's explicit gate" in message


def test_a_catalog_that_cannot_be_read_is_upstream(spec, coops):
    coops.catalog_down = True
    with pytest.raises(RouterUpstreamError) as caught:
        station_tie.tie_at(spec, _tie_params())
    assert caught.value.error_code == "COOPS_DATUMS_UPSTREAM_ERROR"
    assert "could not be read" in str(caught.value)


def test_pages_that_cannot_be_read_at_all_are_upstream(spec, coops):
    coops.unreadable = set(coops.pages)
    with pytest.raises(RouterUpstreamError) as caught:
        station_tie.tie_at(spec, _tie_params())
    assert caught.value.error_code == "COOPS_DATUMS_UPSTREAM_ERROR"
    assert "every station in reach" in str(caught.value)


def test_the_record_shapes_the_resolved_tie(spec):
    tie = {"station_id": "9459881", "station_name": "King Cove",
           "lon": -162.32611, "lat": 55.05989, "distance_km": 119.5,
           "datums": {"NAVD88": 1.385, "MHW": 3.089, "MSL": 2.342},
           "epoch": "1983-2001", "units": "meters"}
    found = datums.record(spec, {"point": _SEED, "tie": tie}, [])
    assert found["station_id"] == "9459881"
    assert found["distance_km"] == 119.5
    assert found["mhw_above_navd88_m"] == 1.704
    assert found["datums"]["MHW"] == 3.089
    assert found["epoch"] == "1983-2001"
    assert "9459881" in found["source"]


def test_a_record_with_no_resolved_tie_refuses_rather_than_answering_thin(spec):
    with pytest.raises(RouterEmptyError) as caught:
        datums.record(spec, {"point": _SEED}, [])
    assert caught.value.error_code == "COOPS_DATUMS_NO_TIE"
    assert "not resolved" in str(caught.value)


def test_a_tie_missing_navd88_refuses_naming_what_the_page_publishes(spec):
    tie = {"station_id": "9459450", "station_name": "Sand Point",
           "lon": -160.50433, "lat": 55.331722, "distance_km": 0.1,
           "datums": {"MHW": 11.289, "MSL": 10.482},
           "epoch": "1983-2001", "units": "meters"}
    with pytest.raises(RouterEmptyError) as caught:
        datums.record(spec, {"point": _SEED, "tie": tie}, [])
    assert caught.value.error_code == "COOPS_DATUMS_NO_TIE"
    message = str(caught.value)
    assert "MHW" in message and "NAVD88" in message


def _offset_params(**over) -> dict:
    asked = {"point": _SEED, "from_frame": "egm2008", "to_frame": "mhw"}
    asked.update(over)
    return asked


def test_the_offset_resolves_a_tie_only_for_a_straddle_pair(offset_spec, coops):
    # Identity, two geodetic frames and two tidal frames carry no station tie
    # to fall back to, and each resolves to nothing without a request.
    assert station_tie.tie_for_offset(
        offset_spec, _offset_params(from_frame="mhw", to_frame="mhw")) == {}
    assert station_tie.tie_for_offset(
        offset_spec, _offset_params(from_frame="navd88",
                                    to_frame="egm2008")) == {}
    assert station_tie.tie_for_offset(
        offset_spec, _offset_params(from_frame="mhw", to_frame="mllw")) == {}
    assert station_tie.tie_for_offset(
        offset_spec, _offset_params(from_frame="igld85",
                                    to_frame="navd88")) == {}


def test_the_offset_carries_the_tie_when_one_exists(offset_spec, coops):
    resolved = station_tie.tie_for_offset(offset_spec, _offset_params())
    assert resolved["tie"]["station_id"] == "9459881"


def test_the_offset_carries_the_checked_candidates_when_none_carries(offset_spec, coops):
    coops.catalog = [row for row in coops.catalog
                     if row["id"] in {"9459450", "9463502"}]
    resolved = station_tie.tie_for_offset(offset_spec, _offset_params())
    assert "tie" not in resolved
    assert resolved["tie_candidates"] == ["9459450", "9463502"]


def test_the_offset_carries_an_empty_candidate_list_when_nothing_stands_in_reach(offset_spec, coops):
    resolved = station_tie.tie_for_offset(
        offset_spec, _offset_params(point=[10.0, 45.0]))
    assert resolved == {"tie_candidates": []}


def test_the_offset_swallows_a_catalog_it_cannot_read(offset_spec, coops):
    # The tie is a fallback, never the reason an ask fails: a catalog outage
    # leaves the pair to VDatum alone.
    coops.catalog_down = True
    assert station_tie.tie_for_offset(offset_spec, _offset_params()) == {}


def test_the_offset_swallows_pages_it_cannot_read(offset_spec, coops):
    coops.unreadable = set(coops.pages)
    assert station_tie.tie_for_offset(offset_spec, _offset_params()) == {}


def test_the_shared_frame_sets_are_the_offset_rows_own(offset_spec):
    # Two homes for either set would let a pair the offset row composes from a
    # station drift apart from the pair the search looks for.
    from trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset import hooks as vd

    assert station_tie.GEODETIC_FRAMES == vd._GEODETIC
    assert station_tie.TIDAL_FRAMES == vd._TIDAL
