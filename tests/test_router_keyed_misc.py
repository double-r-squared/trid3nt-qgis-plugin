"""Migrated coverage for the keyed/misc-leftovers fold (ADR 0071).

The five leftover twins (mobi / climate_normals / ebird_observations /
iucn_red_list_range / usgs_groundwater_levels) fold onto the router: mobi via the
EXISTING stac_float raster mode plus two no-op knobs (single-param asset map +
positive-only nodata gate); climate_normals + usgs_groundwater_levels via the
chained_resolution enrich phase; ebird + iucn via keyed http_json plus the new
``classify_status`` seam (401/403 -> credential-shaped AUTH, 4xx -> INPUT, 5xx ->
default upstream). This file carries the value-bearing offline coverage the deleted
twin test files held: registration parity, the param/error-code parity (incl. the
keyed missing-key credential shaping), and the hook compute primitives (tile dedup +
bbox re-clip, DD placeholder + token envelope, best-effort well-name join, station
inventory filter + drop-and-EMPTY). Live edge-matrix parity vs the twins was proven at
fold time (mobi value-identical; climate_normals / groundwater keyless offline + a
network gate); those are network gates, not offline tests.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pytest

from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.hooks import resolve_hook
from trid3nt_server.data.fetchers._router.registration import _SPEC_REGISTRY
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path
from trid3nt_server.credentials.credential_registry import is_credential_shaped_error

_F = Path("trid3nt_server/data/fetchers")
_SPECS = {
    "fetch_mobi": _F / "biodiversity/fetch_mobi/source.yaml",
    "fetch_climate_normals": _F / "climate/fetch_climate_normals/source.yaml",
    "fetch_ebird_observations": _F / "biodiversity/fetch_ebird_observations/source.yaml",
    "fetch_iucn_red_list_range": _F / "biodiversity/fetch_iucn_red_list_range/source.yaml",
    "fetch_usgs_groundwater_levels": _F / "hydrology/fetch_usgs_groundwater_levels/source.yaml",
}


def _spec(name: str):
    return load_spec_from_path(_SPECS[name])


def _hook(name: str):
    return resolve_hook(name)


def _err(fn, *a, **k):
    with pytest.raises(Exception) as exc:  # noqa: PT011
        fn(*a, **k)
    return exc.value


class _Res:
    def __init__(self, body):
        self.body = body


# --------------------------------------------------------------------------- #
# Registration parity: all five spec-served under the twin name.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", list(_SPECS))
def test_spec_served_under_twin_name(name):
    assert name in TOOL_REGISTRY
    assert name in _SPEC_REGISTRY
    assert "_router._promoted" in TOOL_REGISTRY[name].fn.__module__


# --------------------------------------------------------------------------- #
# mobi: per-param error suffixes (esri-style split) + the declarative knobs.
# --------------------------------------------------------------------------- #


def test_mobi_param_error_codes():
    spec = _spec("fetch_mobi")
    assert _err(router.validate_params, spec, {"bbox": [-100, 30, -99, 31], "layer": "nope"}).error_code == "MOBI_LAYER_INVALID"
    assert _err(router.validate_params, spec, {"bbox": [-100, 30, -100, 31]}).error_code == "MOBI_BBOX_INVALID"


def test_mobi_asset_map_and_positive_gate_declared():
    spec = _spec("fetch_mobi")
    stac = spec.ingest["stac"]
    assert stac["asset_by_param"]["param"] == "layer"
    assert stac["asset_by_param"]["map"]["species_richness"] == "SpeciesRichness_All"
    assert spec.ingest["transform"]["positive_only"] is True
    # units_by_param: richness -> a units string, RSR variants -> None (twin parity).
    m = spec.normalize.units_by_param["map"]
    assert m["species_richness"] == "imperiled-species count"
    assert "range_size_rarity" not in m


# --------------------------------------------------------------------------- #
# climate_normals: inventory filter + drop-and-EMPTY enrich.
# --------------------------------------------------------------------------- #


def test_climate_normals_inventory_filter_and_empty():
    spec = _spec("fetch_climate_normals")
    inv = ("USW00093230  37.6200 -122.3600   2.0 CA SAN FRANCISCO INTL AP" + " " * 40 + "\n").encode()
    feats = _hook("climate_normals.parse_response")(spec, router.validate_params(spec, {"bbox": [-122.5, 37.5, -122.2, 37.8]}), [inv])
    assert len(feats) == 1 and feats[0]["properties"]["sid"] == "USW00093230"
    # a bbox that matches no station -> a typed EMPTY (never an empty layer).
    empty = _err(_hook("climate_normals.parse_response"), spec, router.validate_params(spec, {"bbox": [10.0, 10.0, 10.1, 10.1]}), [inv])
    assert empty.error_code == "CLIMATE_NORMALS_EMPTY" and empty.retryable is False


def test_climate_normals_enrich_merge_and_drop():
    spec = _spec("fetch_climate_normals")
    inv = ("USW00093230  37.6200 -122.3600   2.0 CA SF" + " " * 40 + "\n").encode()
    params = router.validate_params(spec, {"bbox": [-122.5, 37.5, -122.2, 37.8]})
    feats = _hook("climate_normals.parse_response")(spec, params, [inv])
    csv = b"STATION,NAME,LATITUDE,LONGITUDE,ELEVATION,ANN-TAVG-NORMAL,ANN-PRCP-NORMAL\nUSW00093230,SF,37.62,-122.36,2.0,58.3,23.6\n"
    merged = _hook("climate_normals.enrich_merge")(spec, params, feats, {"USW00093230": _Res(csv)})
    assert merged[0]["properties"]["normal_temp_f"] == 58.3
    assert merged[0]["properties"]["normal_precip_in"] == 23.6
    assert set(merged[0]["properties"]) == {"station_id", "name", "elevation_m", "normal_temp_f", "normal_tmin_f", "normal_tmax_f", "normal_precip_in"}
    # a station whose CSV carries no annual normal is DROPPED -> all-drop is EMPTY.
    drop = _err(_hook("climate_normals.enrich_merge"), spec, params, feats, {"USW00093230": _Res(b"STATION,NAME\nX,Y\n")})
    assert drop.error_code == "CLIMATE_NORMALS_EMPTY"


# --------------------------------------------------------------------------- #
# ebird: keyed missing-key + input + status split + dedup/clip parse.
# --------------------------------------------------------------------------- #


def test_ebird_missing_key_credential_shaped(monkeypatch):
    monkeypatch.delenv("TRID3NT_EBIRD_API_KEY", raising=False)
    spec = _spec("fetch_ebird_observations")
    exc = _err(router.route, spec, {"species_code": "bewwre", "bbox": [-122.5, 37.7, -122.4, 37.8]})
    assert exc.error_code == "EBIRD_MISSING_KEY" and exc.retryable is False
    assert is_credential_shaped_error("fetch_ebird_observations", exc) is True


def test_ebird_input_and_status_split():
    spec = _spec("fetch_ebird_observations")
    assert _err(_hook("ebird_observations.build_request"), spec, router.validate_params(spec, {"species_code": "bad code!", "bbox": [-122.5, 37.7, -122.4, 37.8], "api_key": "X"})).error_code == "EBIRD_INPUT_ERROR"
    assert _err(router.validate_params, spec, {"species_code": "bewwre", "bbox": [-122.5, 37.7, -122.4, 37.8], "days_back": 99}).error_code == "EBIRD_INPUT_ERROR"
    cs = _hook("ebird_observations.classify_status")
    auth = cs(spec, 401, "x")
    assert auth.error_code == "EBIRD_AUTH_ERROR" and is_credential_shaped_error("fetch_ebird_observations", auth)
    assert cs(spec, 404, "x").error_code == "EBIRD_INPUT_ERROR"
    assert cs(spec, 500, "x") is None  # 5xx -> default retryable upstream


def test_ebird_parse_dedup_and_bbox_clip():
    spec = _spec("fetch_ebird_observations")
    params = router.validate_params(spec, {"species_code": "bewwre", "bbox": [-122.5, 37.7, -122.4, 37.8], "api_key": "X"})
    a = json.dumps([{"subId": "S1", "lng": -122.45, "lat": 37.75, "obsDt": "2026-07-01", "howMany": 3, "speciesCode": "bewwre"}]).encode()
    b = json.dumps([{"subId": "S1", "lng": -122.45, "lat": 37.75}, {"subId": "S2", "lng": -99.0, "lat": 20.0}]).encode()
    feats = _hook("ebird_observations.parse_response")(spec, params, [a, b])
    assert len(feats) == 1  # S1 deduped across tiles, S2 outside bbox dropped
    assert feats[0]["properties"]["howMany"] == 3


# --------------------------------------------------------------------------- #
# iucn: keyed missing-key (AUTH), region input, status split, parse variants.
# --------------------------------------------------------------------------- #


def test_iucn_missing_key_is_auth_credential_shaped(monkeypatch):
    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    spec = _spec("fetch_iucn_red_list_range")
    exc = _err(router.route, spec, {"species_name": "Puma concolor"})
    assert exc.error_code == "IUCN_AUTH_ERROR" and exc.retryable is False
    assert is_credential_shaped_error("fetch_iucn_red_list_range", exc) is True


def test_iucn_region_and_status_and_parse():
    spec = _spec("fetch_iucn_red_list_range")
    assert _err(_hook("iucn_red_list_range.build_request"), spec, router.validate_params(spec, {"species_name": "Puma concolor", "region": "bad region", "api_key": "X"})).error_code == "IUCN_INPUT_INVALID"
    ci = _hook("iucn_red_list_range.classify_status")
    assert ci(spec, 403, "x").error_code == "IUCN_AUTH_ERROR"
    assert ci(spec, 400, "x").error_code == "IUCN_INPUT_INVALID"
    assert ci(spec, 500, "x") is None
    params = router.validate_params(spec, {"species_name": "Puma concolor", "api_key": "X"})
    real = json.dumps({"result": [{"taxonid": 18868, "scientific_name": "Puma concolor", "category": "LC", "class": "MAMMALIA"}]}).encode()
    fr = _hook("iucn_red_list_range.parse_response")(spec, params, [real])
    assert len(fr[0]["properties"]) == 22 and fr[0]["properties"]["category"] == "LC" and fr[0]["properties"]["class_name"] == "MAMMALIA"
    fd = _hook("iucn_red_list_range.parse_response")(spec, params, [json.dumps({"result": []}).encode()])
    assert fd[0]["properties"]["category"] == "DD" and fd[0]["properties"]["is_placeholder_geometry"] is True
    tok = _err(_hook("iucn_red_list_range.parse_response"), spec, params, [json.dumps({"message": "Token not valid!"}).encode()])
    assert tok.error_code == "IUCN_AUTH_ERROR"


# --------------------------------------------------------------------------- #
# usgs_groundwater_levels: selector gate, NO_WELLS, best-effort location join.
# --------------------------------------------------------------------------- #


def test_groundwater_selector_and_no_wells_and_join():
    spec = _spec("fetch_usgs_groundwater_levels")
    assert _err(_hook("usgs_groundwater_levels.build_request"), spec, router.validate_params(spec, {})).error_code == "USGS_GROUNDWATER_INPUT_ERROR"
    assert _err(_hook("usgs_groundwater_levels.build_request"), spec, router.validate_params(spec, {"state_code": "ZZ"})).error_code == "USGS_GROUNDWATER_INPUT_ERROR"
    params = router.validate_params(spec, {"bbox": [-99.0, 38.0, -98.0, 39.0]})
    meas = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [-98.5, 38.5]},
         "properties": {"monitoring_location_id": "USGS-380", "parameter_code": "72019", "value": "42.1", "unit_of_measure": "ft", "time": "2026-07-01"}}]}).encode()
    feats = _hook("usgs_groundwater_levels.parse_response")(spec, params, [meas])
    assert feats[0]["properties"]["site_no"] == "380"
    assert feats[0]["properties"]["parameter_label"] == "depth to water (ft below land surface)"
    empty = _err(_hook("usgs_groundwater_levels.parse_response"), spec, params, [json.dumps({"type": "FeatureCollection", "features": []}).encode()])
    assert empty.error_code == "USGS_GROUNDWATER_NO_WELLS" and empty.retryable is False
    loc = json.dumps({"type": "FeatureCollection", "features": [
        {"id": "USGS-380", "properties": {"monitoring_location_name": "Test Well", "national_aquifer_code": "N100", "well_constructed_depth": "55"}}]}).encode()
    merged = _hook("usgs_groundwater_levels.enrich_merge")(spec, params, feats, {"locations": _Res(loc)})
    assert merged[0]["properties"]["site_name"] == "Test Well"
    assert merged[0]["properties"]["aquifer_code"] == "N100"
    assert merged[0]["properties"]["well_depth_ft"] == 55.0
    # a failed/absent locations ref leaves names blank but NEVER drops the reading.
    kept = _hook("usgs_groundwater_levels.enrich_merge")(spec, params, feats, {})
    assert len(kept) == 1 and kept[0]["properties"]["site_name"] == ""
