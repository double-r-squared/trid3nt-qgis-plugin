"""The keyed and miscellaneous sources, through the chained-resolution enrich phase.

The value-bearing offline coverage: registration parity and the hook compute
primitives - the station inventory filter with its drop-and-empty, the selector
gate, the no-wells refusal and the best-effort name join. The live edge-matrix
parity ran at fold time on a network gate rather than here."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.hooks import resolve_hook
from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

_F = Path("trid3nt_server/tools/fetchers")
_SPECS = {
    "fetch_climate_normals": _F / "climate/fetch_climate_normals/source.yaml",
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




@pytest.mark.parametrize("name", list(_SPECS))
def test_spec_served_under_twin_name(name):
    assert name in TOOL_REGISTRY
    assert name in _SPEC_REGISTRY
    assert "_router._promoted" in TOOL_REGISTRY[name].fn.__module__




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
