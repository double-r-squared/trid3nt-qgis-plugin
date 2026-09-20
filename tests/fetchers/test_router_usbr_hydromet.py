"""``fetch_usbr_hydromet``: a station name resolved through RISE's own catalog.

The location search decides the station and refuses an ambiguous or datum-less
one; the catalog-record -> catalog-item walk finds the Lake/Reservoir Elevation
item over recorded RISE bodies; the result page becomes one Point feature, and a
truncated or empty page is refused rather than silently short."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_usbr_hydromet import hooks as uh

#: Recorded from ``GET /rise/api/location?search=Scoggins`` (trimmed to the
#: fields the hook reads).
_LOCATION_BODY = json.dumps({"data": [{
    "id": "/rise/api/location/3651", "type": "Location",
    "attributes": {
        "_id": 3651, "locationName": "Henry Hagg Lake and Scoggins Dam (SCO)",
        "locationCoordinates": {"type": "Point", "coordinates": [-123.19965, 45.4724]},
        "horizontalDatum": {"_id": "WGS84", "definition": "World Geodetic System of 1984"},
        "verticalDatum": {"_id": "NGVD29", "definition": "National Geodetic Vertical Datum of 1929"},
        "timezone": "PT", "locationRegionNames": ["Pacific Northwest"],
    },
    "relationships": {"catalogRecords": {"data": [
        {"type": "CatalogRecord", "id": "/rise/api/catalog-record/4557"},
        {"type": "CatalogRecord", "id": "/rise/api/catalog-record/8430"},
    ]}},
}]}).encode()

#: Recorded from ``GET /rise/api/catalog-record/4557`` (a sedimentation-survey
#: record carrying no elevation item).
_RECORD_4557 = json.dumps({"data": {
    "id": "/rise/api/catalog-record/4557",
    "attributes": {"recordTitle": "Henry Hagg Lake (Oregon) Sedimentation Survey Data"},
    "relationships": {"catalogItems": {"data": [
        {"type": "CatalogItem", "id": "/rise/api/catalog-item/11334"}]}},
}}).encode()

#: Recorded from ``GET /rise/api/catalog-record/8430`` (water-operations
#: monitoring: storage, elevation, wind).
_RECORD_8430 = json.dumps({"data": {
    "id": "/rise/api/catalog-record/8430",
    "attributes": {"recordTitle": "Henry Hagg Lake (SCO) Water Operations Monitoring Data"},
    "relationships": {"catalogItems": {"data": [
        {"type": "CatalogItem", "id": "/rise/api/catalog-item/132997"},
        {"type": "CatalogItem", "id": "/rise/api/catalog-item/132998"},
        {"type": "CatalogItem", "id": "/rise/api/catalog-item/132999"},
    ]}},
}}).encode()

_ITEM_11334 = json.dumps({"data": {"attributes": {
    "_id": 11334, "parameterName": "Sedimentation Survey", "parameterUnit": "af"}}}).encode()
_ITEM_132997 = json.dumps({"data": {"attributes": {
    "_id": 132997, "parameterName": "Lake/Reservoir Storage", "parameterUnit": "af"}}}).encode()
_ITEM_132998 = json.dumps({"data": {"attributes": {
    "_id": 132998, "parameterName": "Lake/Reservoir Elevation", "parameterUnit": "ft"}}}).encode()
_ITEM_132999 = json.dumps({"data": {"attributes": {
    "_id": 132999, "parameterName": "Wind Direction", "parameterUnit": "degrees"}}}).encode()

_CATALOG_BODIES = {
    "https://data.usbr.gov/rise/api/catalog-record/4557": _RECORD_4557,
    "https://data.usbr.gov/rise/api/catalog-record/8430": _RECORD_8430,
    "https://data.usbr.gov/rise/api/catalog-item/11334": _ITEM_11334,
    "https://data.usbr.gov/rise/api/catalog-item/132997": _ITEM_132997,
    "https://data.usbr.gov/rise/api/catalog-item/132998": _ITEM_132998,
    "https://data.usbr.gov/rise/api/catalog-item/132999": _ITEM_132999,
}

#: Recorded from ``GET /rise/api/result?itemId=132998&dateTime[after]=2024-06-01
#: &dateTime[before]=2024-06-05`` verbatim, byte-identical to the legacy PN CGI's
#: ``2024-06-04,303.36``.
_RESULT_BODY = json.dumps({"meta": {"totalItems": 2, "itemsPerPage": 2000}, "data": [
    {"attributes": {"itemId": 132998, "dateTime": "2024-06-04T19:00:00+00:00", "result": 303.36}},
    {"attributes": {"itemId": 132998, "dateTime": "2024-06-03T19:00:00+00:00", "result": 303.34}},
]}).encode()


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_usbr_hydromet"]


def test_the_source_states_no_datum_of_its_own_because_each_station_states_one(spec):
    assert spec.normalize.datum is None
    assert spec.vertical_datum is None
    row = spec.coverage[0]
    assert row.datum == "record"
    assert row.extent.kind == "stations"
    assert row.extent.read_from == "usbr_hydromet.stations"
    assert row.reach_km == 20.0


def test_a_code_match_wins_over_an_ambiguous_name(spec):
    coded = {"attributes": {"locationName": "Henry Hagg Lake and Scoggins Dam (SCO)"}}
    other = {"attributes": {"locationName": "Some Other Scoggins Creek Gauge"}}
    assert uh._pick_location(spec, "SCO", [other, coded]) is coded


def test_a_station_that_names_its_own_code_matches_on_it(spec):
    """A station stated in full carries the code RISE spells: the parenthetical
    is read out of it rather than the whole name matched against locationName."""
    coded = {"attributes": {"locationName": "Henry Hagg Lake and Scoggins Dam (SCO)"}}
    other = {"attributes": {"locationName": "Hungry Horse Reservoir (HGH)"}}
    assert uh._pick_location(
        spec, "Henry Hagg Lake and Scoggins Dam (SCO)", [other, coded]) is coded


def test_more_than_one_uncoded_match_refuses_by_name(spec):
    a = {"attributes": {"locationName": "Alpha Reservoir"}}
    b = {"attributes": {"locationName": "Beta Reservoir"}}
    with pytest.raises(RouterInputError) as excinfo:
        uh._pick_location(spec, "reservoir", [a, b])
    assert excinfo.value.error_code == "USBR_HYDROMET_INPUT_INVALID"
    assert "Alpha Reservoir" in str(excinfo.value)


def test_the_elevation_item_is_found_over_the_record_and_item_walk(spec, monkeypatch):
    monkeypatch.setattr(uh, "get_client", lambda: object())
    monkeypatch.setattr(
        uh, "get_bytes",
        lambda client, url, headers=None: (_CATALOG_BODIES[url], "application/vnd.api+json", url))
    item_id, name, unit = uh._find_elevation_item(
        spec, "https://data.usbr.gov",
        ["/rise/api/catalog-record/4557", "/rise/api/catalog-record/8430"],
        "Henry Hagg Lake and Scoggins Dam (SCO)")
    assert (item_id, name, unit) == ("132998", "Lake/Reservoir Elevation", "ft")


def test_a_location_with_no_elevation_item_refuses_by_name(spec, monkeypatch):
    monkeypatch.setattr(uh, "get_client", lambda: object())
    monkeypatch.setattr(
        uh, "get_bytes",
        lambda client, url, headers=None: (_CATALOG_BODIES[url], "application/vnd.api+json", url))
    with pytest.raises(RouterEmptyError) as excinfo:
        uh._find_elevation_item(spec, "https://data.usbr.gov",
                                ["/rise/api/catalog-record/4557"], "No Elevation Reservoir")
    assert excinfo.value.error_code == "USBR_HYDROMET_NO_RECORD"


def test_resolve_parse_merges_the_resolved_item_and_the_stations_own_datums(spec, monkeypatch):
    monkeypatch.setattr(uh, "get_client", lambda: object())
    monkeypatch.setattr(
        uh, "get_bytes",
        lambda client, url, headers=None: (_CATALOG_BODIES[url], "application/vnd.api+json", url))
    merged = uh.resolve_parse(spec, {"station": "SCO"}, [_LOCATION_BODY])
    assert merged["_item_id"] == "132998"
    assert merged["_vertical_datum"] == "NGVD29"
    assert merged["_horizontal_datum"] == "WGS84"
    assert merged["_lon"] == pytest.approx(-123.19965)
    assert merged["_lat"] == pytest.approx(45.4724)
    assert merged["_station_name"] == "Henry Hagg Lake and Scoggins Dam (SCO)"


def test_resolve_parse_refuses_a_location_stating_no_vertical_datum(spec):
    no_datum = json.dumps({"data": [{
        "attributes": {"_id": 1, "locationName": "No Datum Reservoir",
                       "locationCoordinates": {"coordinates": [-100.0, 40.0]},
                       "horizontalDatum": {"_id": "WGS84"}},
        "relationships": {"catalogRecords": {"data": []}},
    }]}).encode()
    with pytest.raises(RouterInputError) as excinfo:
        uh.resolve_parse(spec, {"station": "ND"}, [no_datum])
    assert "No Datum Reservoir" in str(excinfo.value)


def test_no_matching_location_refuses_as_empty(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        uh.resolve_parse(spec, {"station": "NOPE"}, [json.dumps({"data": []}).encode()])
    assert excinfo.value.error_code == "USBR_HYDROMET_NO_RECORD"


def test_build_request_asks_the_resolved_item_over_the_window(spec):
    plans = uh.build_request(
        spec, {"_item_id": "132998", "start_date": "2024-06-01", "end_date": "2024-06-05"})
    assert len(plans) == 1
    assert plans[0].params["itemId"] == "132998"
    assert plans[0].params["dateTime[after]"] == "2024-06-01"
    assert plans[0].params["dateTime[before]"] == "2024-06-05T23:59:59"


def test_parse_response_builds_one_point_feature_with_the_window_inline(spec):
    params = {
        "_lon": -123.19965, "_lat": 45.4724, "_station_id": "3651",
        "_station_name": "Henry Hagg Lake and Scoggins Dam (SCO)",
        "_region": "Pacific Northwest", "_vertical_datum": "NGVD29",
        "_horizontal_datum": "WGS84", "_timezone": "PT",
        "_parameter_name": "Lake/Reservoir Elevation", "start_date": "2024-06-01",
        "end_date": "2024-06-05",
    }
    features = uh.parse_response(spec, params, [_RESULT_BODY])
    assert len(features) == 1
    props = features[0]["properties"]
    assert features[0]["geometry"]["coordinates"] == [-123.19965, 45.4724]
    assert props["vertical_datum"] == "NGVD29"
    assert props["forebay_elevation_ft"] == 303.36
    assert props["forebay_elevation_min_ft"] == 303.34
    assert props["n_timesteps"] == 2
    assert props["time_start"] == "2024-06-03T19:00:00+00:00"
    assert "303.36" in props["time_series_csv"]


def test_a_truncated_page_refuses_rather_than_silently_short(spec):
    body = json.dumps({"meta": {"totalItems": 500}, "data": [
        {"attributes": {"dateTime": "2024-06-01T00:00:00+00:00", "result": 300.0}}]}).encode()
    with pytest.raises(RouterInputError) as excinfo:
        uh.parse_response(spec, {"start_date": "2024-01-01", "end_date": "2024-06-01"}, [body])
    assert excinfo.value.error_code == "USBR_HYDROMET_INPUT_INVALID"


def test_an_empty_window_refuses_as_empty(spec):
    body = json.dumps({"meta": {"totalItems": 0}, "data": []}).encode()
    with pytest.raises(RouterEmptyError) as excinfo:
        uh.parse_response(spec, {"station": "SCO", "start_date": "2024-06-01",
                                 "end_date": "2024-06-05"}, [body])
    assert excinfo.value.error_code == "USBR_HYDROMET_NO_RECORD"


def test_the_source_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("water level series", "fetch_usbr_hydromet")
