"""``fetch_usgs_model_head_targets`` over the real target tables of the Des Moines MF6 data release.

The excerpt holds the archive's own three tables byte-identical, so the join, the period each target
belongs to, the control file's value standing where its source table differs, the box and the refusals
are read off the published files themselves."""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.hooks import resolve_hook

_SPEC = reg.get_spec("fetch_usgs_model_head_targets")
_ZIP = (Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_head_targets"
        / "dm_targets_excerpt.zip").read_bytes()
#: The Des Moines model grid's own lon/lat envelope (the K row's coverage ring), every target inside it.
_GRID = [-93.854, 41.5869, -93.5753, 41.8193]


def _parse(**params):
    return resolve_hook("usgs_model_head_targets.parse_response")(
        _SPEC, router.validate_params(_SPEC, params), [_ZIP])


def test_the_row_is_served():
    assert "fetch_usgs_model_head_targets" in TOOL_REGISTRY
    assert _SPEC is not None


def test_every_elevation_target_of_the_control_file_is_one_point_on_navd88():
    targets = _parse(bbox=_GRID)
    assert len(targets) == 120
    props = [t["properties"] for t in targets]
    assert {p["group"] for p in props} == {"wl_alt_obs"}
    assert {(p["unit"], p["quantity"], p["vertical_datum"]) for p in props} == {("m", "groundwater_head", "NAVD88")}
    assert {p["database"] for p in props} == {"geosam", "wetland", "USGS"}
    first = next(p for p in props if p["target"] == "wl_geosam_00182_19390601")
    assert (first["site_id"], first["value"], first["datetime"], first["model_layer"]) == (
        "w_00001", 257.33, "1939-06-01", 6)
    assert first["weight"] == pytest.approx(0.022158312249902102)


def test_a_reading_before_1980_is_the_steady_periods_and_only_those_are_asked_for():
    steady = _parse(bbox=_GRID, period="steady")
    assert len(steady) == 42
    assert all(t["properties"]["datetime"] < "1980-01-01" for t in steady)
    transient = _parse(bbox=_GRID, period="transient")
    assert len(steady) + len(transient) == 120


def test_the_control_files_value_stands_where_its_source_table_differs():
    # DMWW_gw_wl_obs.csv states 245.26 for this target; the control file PEST read states 292.26.
    held = next(t for t in _parse(bbox=_GRID) if t["properties"]["target"] == "wl_geosam_32152_19881024")
    assert held["properties"]["value"] == 292.26


def test_the_box_keeps_only_its_own_targets():
    box = [-93.63, 41.64, -93.61, 41.65]
    held = _parse(bbox=box, period="steady")
    assert {t["properties"]["site_id"] for t in held} == {"w_00044", "w_00045", "w_00046", "w_00047", "w_00048", "w_00049"}
    for t in held:
        lon, lat = t["geometry"]["coordinates"]
        assert box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def test_a_box_with_no_target_and_an_unknown_period_refuse_typed():
    with pytest.raises(RouterEmptyError):
        _parse(bbox=[-93.84, 41.60, -93.835, 41.605])
    with pytest.raises(RouterInputError):
        _parse(bbox=_GRID, period="pre-1980")


def test_a_box_outside_the_model_grid_refuses_before_the_download():
    build = resolve_hook("usgs_model_head_targets.build_request")
    with pytest.raises(RouterInputError):
        build(_SPEC, router.validate_params(_SPEC, {"bbox": [-122.7, 45.4, -122.5, 45.6]}))
    with pytest.raises(RouterInputError):
        build(_SPEC, router.validate_params(_SPEC, {"bbox": [-70.62, 41.59, -70.50, 41.68]}))
    plan = build(_SPEC, router.validate_params(_SPEC, {"bbox": [-93.73, 41.64, -93.61, 41.70]}))
    import httpx

    (one,) = plan
    sent = httpx.Request(one.method, one.url, params=one.params)
    assert str(sent.url) == _SPEC.endpoints["des_moines_ancillary"].url
