"""Offline tests for the router executors + transforms (B1 -- router-core).

Each executor is exercised against LOCAL synthetic fixtures (tiny in-memory
rasters / GeoJSON features / station records) with the network layer
monkeypatched -- NO live calls. Coverage:

- raster_cog: ``array_to_cog_bytes`` roundtrip (band/dtype/crs/nodata/bounds);
  ``execute`` with a monkeypatched ``fetch_source_array``.
- vector_fgb: ``features_to_fgb_bytes`` non-empty + honest-empty header-only FGB;
  ``fetch_features`` pagination + ``max_features`` cap via a fake page source.
- station_timeseries: ``stations_to_point_fgb`` scalars + inline time_series_csv;
  typed ``*_EMPTY`` on all-empty; ``fetch_station_records`` via monkeypatch.
- tiled_mosaic: ``plan_tile_grid`` math; ``mosaic_tile_files`` over 2 synthetic
  tiles (bounds cover both); single-tile fast path; hard-ceiling redirect error.
- join: ``compute_value`` (value + pct + null floor); ``join_on_key`` missing ->
  null (never fabricated); ``execute`` via monkeypatched geometry/values fetch.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
import rasterio
import rasterio.transform as rtransform

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterNotAvailableError,
    RouterUpstreamError,
)
from trid3nt_server.agent.tools.fetchers._router.executors import (
    raster_cog,
    station_timeseries,
    vector_fgb,
)
from trid3nt_server.agent.tools.fetchers._router.transforms import join, tiled_mosaic


# --------------------------------------------------------------------------- #
# Spec factories (validated SourceSpec objects).
# --------------------------------------------------------------------------- #


def _raster_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_raster",
        "source_class": "demo_raster",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "http://example.test/data.nc"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"access": "opendap"},
        "normalize": {"crs": "EPSG:4326", "units": "Percent"},
        "output": {"layer_type": "raster", "ext": "tif", "style_preset": "demo"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01},
    })


def _vector_spec(properties=None, page_size=2, max_features=5) -> SourceSpec:
    ingest = {"pagination": {"page_size": page_size}, "query_template": {"f": "geojson"}}
    if properties is not None:
        ingest["properties"] = properties
    return SourceSpec.model_validate({
        "name": "fetch_demo_vector",
        "source_class": "demo_vector",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": "http://example.test/FeatureServer/0/query"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "gates": {"max_features": max_features},
        "ingest": ingest,
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "demo_vec"},
        "cache": {"ttl_class": "semi-static-7d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 1.0},
    })


def _station_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_station",
        "source_class": "demo_station",
        "shape": "station-timeseries-fgb",
        "endpoints": {
            "catalog": {"url": "http://example.test/stations.json"},
            "data": {"url_template": "http://example.test/datagetter"},
        },
        "params": {"bbox": {"type": "bbox", "required": True}},
        "gates": {"max_stations": 10},
        "ingest": {
            "station_catalog": {"lat_key": "lat", "lon_key": "lng", "id_key": "id", "name_key": "name"},
            "per_station": {"rows_key": ["data"], "time_key": "t", "value_key": "v"},
        },
        "normalize": {"datum": "MLLW", "units": "m (MLLW)"},
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "coops"},
        "cache": {"ttl_class": "dynamic-1h"},
        "payload_estimate": {"model": "per_station", "kb_per_station_per_day": 2.0},
    })


def _mosaic_spec(max_bbox_deg2=8.0, tile_deg2=0.5) -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_mosaic",
        "source_class": "demo_mosaic",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "http://example.test/stac"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "gates": {"max_bbox_deg2": max_bbox_deg2},
        "ingest": {
            "access": "stac_search",
            "tile_deg2": tile_deg2,
            "mosaic": {"method": "first", "resampling": "nearest"},
        },
        "output": {"layer_type": "raster", "ext": "tif", "style_preset": "demo_lc"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "tiled", "mb_per_tile": 0.05, "tile_deg2": tile_deg2},
    })


def _join_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_join",
        "source_class": "demo_join",
        "shape": "vector-fgb",
        "endpoints": {
            "geometry": {"url": "http://example.test/tracts/query"},
            "values": {"url": "http://example.test/acs"},
        },
        "params": {"bbox": {"type": "bbox", "required": True}, "variable": {"type": "str", "default": "median_income"}},
        "join": {
            "geometry": {"endpoint": "geometry", "key_field": "GEOID", "keep": ["NAME", "STATE", "COUNTY"]},
            "values": {
                "endpoint": "values",
                "scope_by": ["STATE", "COUNTY"],
                "null_sentinel_below": -666666000.0,
                "variables": {
                    "median_income": {"code": "B19013_001E", "kind": "value", "units": "usd"},
                    "poverty_rate": {"num": ["B17001_002E"], "denom": "B17001_001E", "kind": "pct", "units": "percent"},
                },
            },
        },
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "acs"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 2.0},
    })


# --------------------------------------------------------------------------- #
# raster_cog
# --------------------------------------------------------------------------- #


def _synthetic_raster(bbox=(-117.5, 33.5, -116.5, 34.5), n=64):
    arr = (np.arange(n * n, dtype="float32").reshape(n, n) % 37.0) + 1.0
    transform = rtransform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], n, n)
    return arr, transform, "EPSG:4326"


def test_array_to_cog_bytes_roundtrips():
    arr, transform, crs = _synthetic_raster()
    data = raster_cog.array_to_cog_bytes(arr, transform, crs)
    assert isinstance(data, bytes) and len(data) > 0
    with rasterio.open(io.BytesIO(data)) as src:
        assert src.count == 1
        assert src.dtypes[0] == "float32"
        assert src.crs.to_string() == "EPSG:4326"
        assert np.isnan(src.nodata)
        band = src.read(1)
        assert float(np.nanmin(band)) == pytest.approx(1.0, abs=1e-3)
        # north-up: bounds top > bottom
        assert src.bounds.top > src.bounds.bottom


def test_raster_execute_uses_fetch_source_array(monkeypatch):
    spec = _raster_spec()
    monkeypatch.setattr(raster_cog, "fetch_source_array", lambda s, p: _synthetic_raster())
    data = raster_cog.execute(spec, {"bbox": [-117.5, 33.5, -116.5, 34.5]})
    with rasterio.open(io.BytesIO(data)) as src:
        assert src.count == 1 and src.crs.to_string() == "EPSG:4326"


# --------------------------------------------------------------------------- #
# raster_cog: imageserver_export mode (fold wave-7, ADR 0053; landfire/usfs)
# --------------------------------------------------------------------------- #


def _imageserver_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_imgsrv",
        "source_class": "demo_imgsrv",
        "error_prefix": "DEMO_IMG",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://img.test/rest/services/Fam"}},
        "params": {
            "bbox": {"type": "bbox", "required": True, "error_suffix": "BBOX_INVALID"},
            "layer": {"type": "enum", "default": "a", "values": ["a", "b"], "error_suffix": "LAYER_INVALID"},
        },
        "ingest": {
            "access": "imageserver_export",
            "imageserver": {
                "service_by_param": {"param": "layer", "map": {"a": "SVC_A", "b": "SVC_B"}},
                "native_cell_m": 30.0, "px_min": 16, "px_max": 4096,
                "export_query": {"bboxSR": "4326", "format": "tiff", "pixelType": "S16",
                                 "imageSR": "4326", "f": "image"},
                "nodata_sentinel": -32768, "zero_is_nodata": True,
            },
        },
        "normalize": {"crs": "EPSG:4326",
                      "units_by_param": {"param": "layer", "map": {"b": "m * 10"}}},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                   "emit_bbox": False, "style_preset": "categorical_landcover",
                   "style_preset_by_param": {"param": "layer",
                                             "map": {"a": "categorical_landcover", "b": "continuous_dem"}}},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.5, "floor_mb": 0.05, "ceil_mb": 50.0},
    })


def _s16_tiff(value: int, n: int = 8) -> bytes:
    prof = dict(driver="GTiff", height=n, width=n, count=1, dtype="int16",
                crs="EPSG:4326", nodata=-32768,
                transform=rtransform.from_bounds(-112.0, 34.5, -111.9, 34.6, n, n))
    buf = io.BytesIO()
    with rasterio.open(buf, "w", **prof) as dst:
        dst.write(np.full((n, n), value, dtype="int16"), 1)
    return buf.getvalue()


def test_imageserver_size_formula_and_clamp():
    # ~0.06 deg lon at ~34.5N -> a few thousand metres / 30 m, well inside clamp.
    w, h = raster_cog._imageserver_size((-112.02, 34.50, -111.96, 34.56),
                                        {"native_cell_m": 30.0, "px_min": 16, "px_max": 4096})
    assert 16 <= w <= 4096 and 16 <= h <= 4096
    # A degenerate-tiny bbox clamps UP to px_min; a huge one clamps DOWN to px_max.
    assert raster_cog._imageserver_size((-112.0, 34.5, -111.9999, 34.5001),
                                        {"native_cell_m": 30.0, "px_min": 16, "px_max": 4096}) == (16, 16)
    wg, hg = raster_cog._imageserver_size((-125.0, 25.0, -67.0, 49.0),
                                          {"native_cell_m": 30.0, "px_min": 16, "px_max": 4096})
    assert wg == 4096 and hg == 4096


def _patch_transport(monkeypatch, body: bytes, ct: str = "image/tiff"):
    tp = "trid3nt_server.agent.tools.fetchers._router.transport"
    monkeypatch.setattr(f"{tp}.get_client", lambda: object())
    monkeypatch.setattr(f"{tp}.get_bytes", lambda *a, **k: (body, ct, "x"))


def test_imageserver_export_passthrough_bytes_unchanged(monkeypatch):
    spec = _imageserver_spec()
    body = _s16_tiff(121)
    _patch_transport(monkeypatch, body)
    out = raster_cog.execute(spec, {"bbox": [-112.02, 34.50, -111.96, 34.56], "layer": "a"})
    assert out == body  # the exportImage body IS the artifact -- no reserialize


def test_imageserver_export_json_envelope_is_upstream(monkeypatch):
    spec = _imageserver_spec()
    _patch_transport(monkeypatch, b'{"error":{"code":400}}', ct="application/json")
    with pytest.raises(Exception) as ei:
        raster_cog.execute(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "a"})
    assert ei.value.error_code == "DEMO_IMG_UPSTREAM_ERROR"


def test_imageserver_export_non_tiff_is_upstream(monkeypatch):
    spec = _imageserver_spec()
    _patch_transport(monkeypatch, b"<html>gateway timeout</html>", ct="text/html")
    with pytest.raises(Exception) as ei:
        raster_cog.execute(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "a"})
    assert ei.value.error_code == "DEMO_IMG_UPSTREAM_ERROR"


def test_imageserver_export_all_nodata_is_empty(monkeypatch):
    spec = _imageserver_spec()
    _patch_transport(monkeypatch, _s16_tiff(-32768))
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog.execute(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "a"})
    assert ei.value.error_code == "DEMO_IMG_EMPTY"


def test_imageserver_export_all_zero_is_empty(monkeypatch):
    spec = _imageserver_spec()  # zero_is_nodata -> all-zero over water is EMPTY
    _patch_transport(monkeypatch, _s16_tiff(0))
    with pytest.raises(RouterEmptyError):
        raster_cog.execute(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "a"})


def test_imageserver_layer_uri_param_keyed_style_and_units():
    from trid3nt_server.agent.tools.fetchers._router import router as rmod
    spec = _imageserver_spec()
    # layer "a": categorical preset, units None (absent from units map), no bbox.
    la = rmod.build_layer_uri(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "a"}, "s3://x.tif")
    assert la.style_preset == "categorical_landcover" and la.units is None
    assert la.role == "primary" and la.bbox is None
    # layer "b": continuous preset, mapped units.
    lb = rmod.build_layer_uri(spec, {"bbox": [-112.0, 34.5, -111.9, 34.6], "layer": "b"}, "s3://x.tif")
    assert lb.style_preset == "continuous_dem" and lb.units == "m * 10"


# --------------------------------------------------------------------------- #
# raster_cog: stac_float continuous-float mode (fold wave-7, ADR 0053; modis_lst)
# --------------------------------------------------------------------------- #


def _stac_float_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_float",
        "source_class": "demo_float",
        "error_prefix": "DEMO_FLOAT",
        "empty_error_suffix": "NO_DATA",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://pc.test/stac"}},
        "params": {
            "bbox": {"type": "bbox", "required": True, "error_suffix": "BBOX_INVALID"},
            "product": {"type": "str", "default": "11A2"},
            "daynight": {"type": "str", "default": "day"},
        },
        "gates": {"max_bbox_deg2": 6.0},
        "ingest": {
            "access": "stac_float", "native_cell_m": 1000.0,
            "stac": {"root": "https://pc.test/stac", "select": "latest",
                     "param_error_suffix": "PARAM_INVALID",
                     "collection_by_param": {"param": "product",
                                             "map": {"11A2": "modis-11A2-061", "21A2": "modis-21A2-061"}},
                     "asset_by_params": {"params": ["product", "daynight"],
                                         "map": {"11A2": {"day": "LST_Day_1km", "night": "LST_Night_1km"},
                                                 "21A2": {"day": "LST_Day_1KM", "night": "LST_Night_1KM"}}},
                     "product_aliases": {"mod11a2": "11A2", "11a2": "11A2", "21a2": "21A2"},
                     "daynight_aliases": {"day": "day", "d": "day", "night": "night", "n": "night"}},
            "transform": {"scale": 0.02, "offset": -273.15, "fill_dn": 0, "src_nodata": 0},
        },
        "normalize": {"crs": "EPSG:4326", "units": "deg C"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                   "emit_bbox": False, "style_preset": "land_surface_temp_c"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.5, "floor_mb": 0.1},
    })


def test_normalize_via_aliases_maps_and_validates():
    spec = _stac_float_spec()
    al = {"mod11a2": "11A2", "11a2": "11A2"}
    assert raster_cog._normalize_via_aliases(spec, "MOD11A2", al, ["11A2", "21A2"], "PARAM_INVALID") == "11A2"
    # canonical passthrough via upper() fallback.
    assert raster_cog._normalize_via_aliases(spec, "21A2", al, ["11A2", "21A2"], "PARAM_INVALID") == "21A2"
    with pytest.raises(RouterInputError) as ei:
        raster_cog._normalize_via_aliases(spec, "not_real", al, ["11A2", "21A2"], "PARAM_INVALID")
    assert ei.value.error_code == "DEMO_FLOAT_PARAM_INVALID" and ei.value.retryable is False


def test_stac_float_bad_product_is_param_invalid():
    spec = _stac_float_spec()
    with pytest.raises(RouterInputError) as ei:
        raster_cog._stac_float_to_array(spec, {"bbox": [-112.3, 33.3, -111.8, 33.6],
                                               "product": "not_real", "daynight": "day"})
    assert ei.value.error_code == "DEMO_FLOAT_PARAM_INVALID"


def test_stac_float_bad_daynight_is_param_invalid():
    spec = _stac_float_spec()
    with pytest.raises(RouterInputError) as ei:
        raster_cog._stac_float_to_array(spec, {"bbox": [-112.3, 33.3, -111.8, 33.6],
                                               "product": "11A2", "daynight": "dusk"})
    assert ei.value.error_code == "DEMO_FLOAT_PARAM_INVALID"


def test_fetch_source_array_dispatches_stac_float(monkeypatch):
    spec = _stac_float_spec()
    monkeypatch.setattr(raster_cog, "_stac_float_to_array",
                        lambda s, p: ("SENTINEL", "T", "EPSG:4326"))
    assert raster_cog.fetch_source_array(spec, {"bbox": [-112.3, 33.3, -111.8, 33.6]})[0] == "SENTINEL"


def test_payload_ceil_mb_clips():
    spec = _imageserver_spec()
    from trid3nt_server.agent.tools.fetchers._router import router as rmod
    est = rmod.synthesize_payload_estimator(spec)
    # 0.5 MB/deg^2 * huge bbox would exceed 50 but ceil clips to 50.
    assert est(bbox=[-125.0, 25.0, -67.0, 49.0]) == 50.0
    # tiny bbox floors at 0.05.
    assert est(bbox=[-112.0, 34.5, -111.99, 34.51]) == pytest.approx(0.05)


# --------------------------------------------------------------------------- #
# vector_fgb
# --------------------------------------------------------------------------- #


def _feature(lon, lat, name, kind):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"name": name, "kind": kind},
    }


def test_features_to_fgb_nonempty_roundtrips():
    spec = _vector_spec(properties=["name", "kind"])
    feats = [_feature(-100.0, 40.0, "a", "hospital"), _feature(-100.1, 40.1, "b", "clinic")]
    data = vector_fgb.features_to_fgb_bytes(feats, spec)
    import geopandas as gpd

    gdf = gpd.read_file(io.BytesIO(data))
    assert len(gdf) == 2
    assert set(["name", "kind"]).issubset(set(gdf.columns))
    assert gdf.crs.to_string() == "EPSG:4326"
    assert gdf.geometry.geom_type.iloc[0] == "Point"


def test_features_to_fgb_empty_is_header_only_not_error():
    spec = _vector_spec(properties=["name", "kind"])
    data = vector_fgb.features_to_fgb_bytes([], spec)
    assert isinstance(data, bytes) and len(data) > 0  # valid FGB, not an error
    import geopandas as gpd

    gdf = gpd.read_file(io.BytesIO(data))
    assert len(gdf) == 0


def test_fetch_features_paginates_and_caps(monkeypatch):
    spec = _vector_spec(page_size=2, max_features=5)
    pages = [
        [_feature(-100.0, 40.0, "a", "x"), _feature(-100.1, 40.1, "b", "x")],  # full page
        [_feature(-100.2, 40.2, "c", "x"), _feature(-100.3, 40.3, "d", "x")],  # full page
        [_feature(-100.4, 40.4, "e", "x"), _feature(-100.5, 40.5, "f", "x")],  # would exceed cap
    ]
    calls = {"n": 0}

    def _fake_page(s, url, params):
        i = calls["n"]
        calls["n"] += 1
        return pages[i] if i < len(pages) else []

    monkeypatch.setattr(vector_fgb, "_fetch_one_page", _fake_page)
    feats = vector_fgb.fetch_features(spec, {"bbox": [-101, 39, -100, 41]})
    assert len(feats) == 5  # capped at max_features


# --------------------------------------------------------------------------- #
# station_timeseries
# --------------------------------------------------------------------------- #


def _station_record(sid, lon, lat, values):
    return {
        "station_id": sid,
        "station_name": f"Station {sid}",
        "lon": lon,
        "lat": lat,
        "rows": [{"t": f"2026-07-0{i+1}", "v": v} for i, v in enumerate(values)],
    }


def test_stations_to_point_fgb_scalars_and_csv():
    spec = _station_spec()
    recs = [_station_record("8725520", -81.87, 26.65, [0.1, 0.5, 0.3])]
    data = station_timeseries.stations_to_point_fgb(recs, spec, product="water_level")
    import geopandas as gpd

    gdf = gpd.read_file(io.BytesIO(data))
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row["station_id"] == "8725520"
    assert row["n_timesteps"] == 3
    assert float(row["wl_min_m"]) == pytest.approx(0.1, abs=1e-6)
    assert float(row["wl_max_m"]) == pytest.approx(0.5, abs=1e-6)
    assert float(row["wl_mean_m"]) == pytest.approx(0.3, abs=1e-6)
    # inline time_series_csv carries iso,value rows
    csv_text = row["time_series_csv"]
    assert "2026-07-01,0.100000" in csv_text
    assert csv_text.strip().count("\n") == 2  # 3 rows


def test_stations_all_empty_raises_typed_empty():
    spec = _station_spec()
    recs = [{"station_id": "x", "station_name": "x", "lon": -80, "lat": 26, "rows": []}]
    with pytest.raises(RouterEmptyError) as ei:
        station_timeseries.stations_to_point_fgb(recs, spec)
    assert ei.value.error_code == "DEMO_STATION_EMPTY"


def test_fetch_station_records_via_monkeypatch(monkeypatch):
    spec = _station_spec()
    monkeypatch.setattr(
        station_timeseries, "_discover_stations",
        lambda s, bbox: [{"station_id": "1", "station_name": "S1", "lon": -80.0, "lat": 26.0}],
    )
    monkeypatch.setattr(
        station_timeseries, "_fetch_station_series",
        lambda s, st, p: [{"t": "2026-07-01", "v": 1.0}, {"t": "2026-07-02", "v": 2.0}],
    )
    recs = station_timeseries.fetch_station_records(spec, {"bbox": [-81, 25, -79, 27]})
    assert len(recs) == 1 and len(recs[0]["rows"]) == 2


# --------------------------------------------------------------------------- #
# tiled_mosaic transform
# --------------------------------------------------------------------------- #


def test_plan_tile_grid_single_tile_when_small():
    tiles = tiled_mosaic.plan_tile_grid((-100.0, 40.0, -99.8, 40.2), tile_deg2=0.5)
    assert tiles == [(-100.0, 40.0, -99.8, 40.2)]


def test_plan_tile_grid_splits_large_bbox_covering_full_extent():
    bbox = (-100.0, 40.0, -98.0, 42.0)  # 4 deg^2 -> multiple 0.5 tiles
    tiles = tiled_mosaic.plan_tile_grid(bbox, tile_deg2=0.5)
    assert len(tiles) > 1
    # union of tiles covers the full bbox exactly
    assert min(t[0] for t in tiles) == pytest.approx(bbox[0])
    assert min(t[1] for t in tiles) == pytest.approx(bbox[1])
    assert max(t[2] for t in tiles) == pytest.approx(bbox[2])
    assert max(t[3] for t in tiles) == pytest.approx(bbox[3])
    # each tile area <= cap
    for w, s, e, n in tiles:
        assert (e - w) * (n - s) <= 0.5 + 1e-9


def test_mosaic_tile_files_merges_two_tiles(tmp_path):
    a_arr, a_tf, crs = _synthetic_raster(bbox=(-100.0, 40.0, -99.5, 40.5), n=32)
    b_arr, b_tf, _ = _synthetic_raster(bbox=(-99.5, 40.0, -99.0, 40.5), n=32)
    pa = tiled_mosaic.array_to_tempfile(a_arr, a_tf, crs)
    pb = tiled_mosaic.array_to_tempfile(b_arr, b_tf, crs)
    data = tiled_mosaic.mosaic_tile_files([pa, pb], method="first", resampling="nearest")
    with rasterio.open(io.BytesIO(data)) as src:
        # merged extent spans both tiles' longitudes
        assert src.bounds.left == pytest.approx(-100.0, abs=1e-3)
        assert src.bounds.right == pytest.approx(-99.0, abs=1e-3)


def test_mosaic_execute_single_tile_fast_path(monkeypatch):
    spec = _mosaic_spec()
    called = {"execute": 0}

    def _fake_execute(s, p):
        called["execute"] += 1
        arr, tf, crs = _synthetic_raster(bbox=tuple(p["bbox"]), n=16)
        return raster_cog.array_to_cog_bytes(arr, tf, crs)

    monkeypatch.setattr(raster_cog, "execute", _fake_execute)
    data = tiled_mosaic.execute(spec, {"bbox": [-100.0, 40.0, -99.8, 40.2]})  # < 0.5 deg^2
    assert called["execute"] == 1  # single-tile fast path, one executor call
    assert isinstance(data, bytes) and len(data) > 0


def test_mosaic_execute_multi_tile_merges(monkeypatch):
    spec = _mosaic_spec()

    def _fake_fetch(s, p):
        return _synthetic_raster(bbox=tuple(p["bbox"]), n=16)

    monkeypatch.setattr(raster_cog, "fetch_source_array", _fake_fetch)
    data = tiled_mosaic.execute(spec, {"bbox": [-100.0, 40.0, -99.0, 41.0]})  # ~1 deg^2 -> tiles
    with rasterio.open(io.BytesIO(data)) as src:
        assert src.bounds.left == pytest.approx(-100.0, abs=1e-2)
        assert src.bounds.right == pytest.approx(-99.0, abs=1e-2)


def test_mosaic_hard_ceiling_redirects_with_typed_error():
    spec = _mosaic_spec(max_bbox_deg2=1.0)
    with pytest.raises(RouterInputError) as ei:
        tiled_mosaic.execute(spec, {"bbox": [-100.0, 40.0, -96.0, 44.0]})  # 16 deg^2 > 1
    assert ei.value.error_code == "DEMO_MOSAIC_INPUT_ERROR"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# join transform
# --------------------------------------------------------------------------- #


def test_compute_value_kind_value():
    var = {"code": "B19013_001E", "kind": "value", "units": "usd"}
    assert join.compute_value(var, {"B19013_001E": 65000.0}) == 65000.0
    assert join.compute_value(var, None) is None
    assert join.compute_value(var, {}) is None  # missing -> None (never fabricated)


def test_compute_value_null_floor_normalizes_sentinel():
    var = {"code": "B19013_001E", "kind": "value"}
    assert join.compute_value(var, {"B19013_001E": -666666666.0}, null_floor=-666666000.0) is None


def test_compute_value_kind_pct():
    var = {"num": ["B17001_002E"], "denom": "B17001_001E", "kind": "pct"}
    assert join.compute_value(var, {"B17001_002E": 25.0, "B17001_001E": 100.0}) == 25.0
    assert join.compute_value(var, {"B17001_002E": 25.0, "B17001_001E": 0.0}) is None  # denom<=0
    assert join.compute_value(var, {"B17001_001E": 100.0}) is None  # missing num -> None


def test_join_on_key_left_join_missing_is_null():
    spec = _join_spec()
    join_block = spec.join
    geom = [
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
         "properties": {"GEOID": "48201010101", "NAME": "Tract A", "STATE": "48", "COUNTY": "201"}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[1, 1], [1, 2], [2, 2], [1, 1]]]},
         "properties": {"GEOID": "48201010102", "NAME": "Tract B", "STATE": "48", "COUNTY": "201"}},
    ]
    values = {"48201010101": {"B19013_001E": 72000.0}}  # tract B has NO value
    var_name, var_spec = "median_income", join_block["values"]["variables"]["median_income"]
    out = join.join_on_key(geom, values, join_block, var_name, var_spec, null_floor=-666666000.0)
    by_geoid = {f["properties"]["geoid"]: f["properties"] for f in out}
    assert by_geoid["48201010101"]["value"] == 72000.0
    assert by_geoid["48201010102"]["value"] is None  # honest null, not fabricated
    assert by_geoid["48201010101"]["name"] == "Tract A"
    assert by_geoid["48201010101"]["units"] == "usd"


def test_join_execute_via_monkeypatch(monkeypatch):
    spec = _join_spec()
    geom = [
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
         "properties": {"GEOID": "48201010101", "NAME": "A", "STATE": "48", "COUNTY": "201"}},
    ]
    monkeypatch.setattr(join, "fetch_geometry", lambda s, p: geom)
    monkeypatch.setattr(join, "fetch_values", lambda s, scopes, vs, p: {"48201010101": {"B19013_001E": 72000.0}})
    data = join.execute(spec, {"bbox": [-96, 29, -95, 30], "variable": "median_income"})
    import geopandas as gpd

    gdf = gpd.read_file(io.BytesIO(data))
    assert len(gdf) == 1
    assert float(gdf.iloc[0]["value"]) == pytest.approx(72000.0)


# --------------------------------------------------------------------------- #
# phase-2 wave-2 ArcGIS-family additions: WHERE builder / column_map /
# endpoint chain / int_range + date_compact params.
# --------------------------------------------------------------------------- #


def _wave2_spec(**over) -> SourceSpec:
    base = {
        "name": "fetch_demo_w2",
        "source_class": "demo_w2",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": "http://example.test/FeatureServer/0/query"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"pagination": {"page_size": 2}},
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "w2"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 1.0},
    }
    base.update(over)
    return SourceSpec.model_validate(base)


def test_build_where_conditional_clause():
    spec = _wave2_spec(
        params={"bbox": {"type": "bbox", "required": True},
                "min_voltage_kv": {"type": "float", "required": False}},
        ingest={"where_clauses": [{"template": "VOLTAGE >= {min_voltage_kv:g}", "require": ["min_voltage_kv"]}]},
    )
    # absent -> 1=1; present -> the templated floor.
    assert vector_fgb.build_where(spec, {"bbox": [-1, 0, 1, 2]}) == "1=1"
    assert vector_fgb.build_where(spec, {"bbox": [-1, 0, 1, 2], "min_voltage_kv": 345.0}) == "VOLTAGE >= 345"


def test_build_where_int_range_template():
    spec = _wave2_spec(
        ingest={"where_clauses": [{"template": "YEAR >= {year_range[0]} AND YEAR <= {year_range[1]}", "require": ["year_range"]}]},
    )
    assert vector_fgb.build_where(spec, {"year_range": [2018, 2021]}) == "YEAR >= 2018 AND YEAR <= 2021"


def test_column_map_rename_and_null_sentinel():
    spec = _wave2_spec(ingest={"column_map": {
        "fips": {"from": "FIPS"},
        "score": {"from": "RPL", "kind": "float", "null_below": -999.0},
    }})
    feats = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
         "properties": {"FIPS": "48201", "RPL": 0.82, "DROP_ME": 1}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]},
         "properties": {"FIPS": "48202", "RPL": -999.0}},
    ]
    out = vector_fgb.apply_column_map(feats, spec)
    assert set(out[0]["properties"]) == {"fips", "score"}       # projected + renamed
    assert out[0]["properties"]["score"] == 0.82
    assert out[1]["properties"]["score"] is None                # -999 sentinel -> null


def test_column_map_lookup_and_skip_feature():
    spec = _wave2_spec(ingest={"column_map": {
        "dm": {"from": "dm", "kind": "int", "default": 0, "on_error": "skip_feature"},
        "label": {"from": "dm", "kind": "lookup", "key_from": "dm",
                  "table": {0: "D0", 2: "D2 Severe"}, "default_template": "D{key}"},
    }})
    feats = [
        {"type": "Feature", "geometry": None, "properties": {"dm": 2}},
        {"type": "Feature", "geometry": None, "properties": {"dm": 7}},     # miss -> template
        {"type": "Feature", "geometry": None, "properties": {"dm": None}},  # bad int -> skip
    ]
    out = vector_fgb.apply_column_map(feats, spec)
    assert len(out) == 2                                        # third feature skipped
    assert out[0]["properties"] == {"dm": 2, "label": "D2 Severe"}
    assert out[1]["properties"] == {"dm": 7, "label": "D7"}


def test_column_map_epoch_ms_iso_and_ci():
    spec = _wave2_spec(ingest={"column_map_ci": True, "column_map": {
        "valid_date": {"from": "ddate", "kind": "epoch_ms_iso", "default": ""},
        "ftype": {"from": "FType", "kind": "int"},
    }})
    feats = [{"type": "Feature", "geometry": None,
              "properties": {"DDATE": 1659398400000, "FTYPE": "390"}}]
    out = vector_fgb.apply_column_map(feats, spec)
    assert out[0]["properties"]["valid_date"] == "2022-08-02"   # epoch-ms -> ISO
    assert out[0]["properties"]["ftype"] == 390                 # case-insensitive match


def test_resolve_endpoints_select_and_fallback():
    spec = _wave2_spec(
        endpoints={"current": {"url": "http://x/3/query"}, "archive": {"url": "http://x/2/query"}},
        params={"bbox": {"type": "bbox", "required": True},
                "date": {"type": "date_compact", "required": False}},
        ingest={"endpoint_select": {"param": "date", "absent": "current", "present": "archive"}},
    )
    assert vector_fgb.resolve_endpoints(spec, {})[0].url == "http://x/3/query"
    assert vector_fgb.resolve_endpoints(spec, {"date": "20220802"})[0].url == "http://x/2/query"
    # fallback chain
    spec2 = _wave2_spec(
        endpoints={"data": {"url": "http://a/query"}, "medium": {"url": "http://b/query"}},
        fallback=["medium"],
    )
    chain = vector_fgb.resolve_endpoints(spec2, {"bbox": [0, 0, 1, 1]})
    assert [e.url for e in chain] == ["http://a/query", "http://b/query"]


def test_validate_int_range_and_date_compact():
    from trid3nt_server.agent.tools.fetchers._router import router as router_mod
    from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError

    spec = _wave2_spec(params={
        "bbox": {"type": "bbox", "required": True},
        "year_range": {"type": "int_range", "required": False, "min": 1984, "max": 2100},
        "date": {"type": "date_compact", "required": False},
    })
    out = router_mod.validate_params(spec, {"bbox": [-1, 0, 1, 2], "year_range": [2018, 2021], "date": "2022-08-02"})
    assert out["year_range"] == [2018, 2021]
    assert out["date"] == "20220802"                           # normalized to compact
    # below the min -> typed input error
    with pytest.raises(RouterInputError):
        router_mod.validate_params(spec, {"bbox": [-1, 0, 1, 2], "year_range": [1800, 1900]})
    # not a real calendar date -> typed input error
    with pytest.raises(RouterInputError):
        router_mod.validate_params(spec, {"bbox": [-1, 0, 1, 2], "date": "2020-13-40"})


# --------------------------------------------------------------------------- #
# raster_cog: serialize nodata/dtype directive (fold wave-8, ADR 0054; copernicus)
# --------------------------------------------------------------------------- #


def _serialize_spec(serialize: dict | None) -> SourceSpec:
    ingest = {"access": "direct_window", "native_cell_m": 30.0}
    if serialize is not None:
        ingest["serialize"] = serialize
    return SourceSpec.model_validate({
        "name": "fetch_demo_dem", "source_class": "demo_dem", "error_prefix": "DEMO_DEM",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://pc.test/x.tif"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": ingest,
        "normalize": {"crs": "EPSG:4326", "units": "meters"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "input",
                   "style_preset": "continuous_dem"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 8.0, "floor_mb": 0.1},
    })


def _read_cog(b: bytes):
    with rasterio.io.MemoryFile(b) as m, m.open() as src:
        return src.read(1), src.nodata, src.dtypes[0]


def test_serialize_directive_fills_and_stamps_nodata(monkeypatch):
    """serialize.nodata=-9999 fills NaN -> sentinel and stamps the band nodata."""
    arr = np.array([[1.0, np.nan], [3.5, 4.0]], dtype="float32")
    tf = rtransform.from_bounds(0, 0, 1, 1, 2, 2)
    monkeypatch.setattr(raster_cog, "fetch_source_array", lambda s, p: (arr, tf, "EPSG:4326"))
    spec = _serialize_spec({"nodata": -9999.0, "dtype": "float32"})
    out, nodata, dtype = _read_cog(raster_cog.execute(spec, {"bbox": [0, 0, 1, 1]}))
    assert nodata == -9999.0 and dtype == "float32"
    assert out[0, 1] == -9999.0                 # NaN -> sentinel
    assert out[0, 0] == 1.0 and out[1, 0] == 3.5  # finite pixels preserved


def test_serialize_absent_is_nan_nodata_passthrough(monkeypatch):
    """No serialize block -> NaN-nodata passthrough (every prior float spec)."""
    arr = np.array([[1.0, np.nan]], dtype="float32")
    tf = rtransform.from_bounds(0, 0, 2, 1, 2, 1)
    monkeypatch.setattr(raster_cog, "fetch_source_array", lambda s, p: (arr, tf, "EPSG:4326"))
    spec = _serialize_spec(None)
    out, nodata, _ = _read_cog(raster_cog.execute(spec, {"bbox": [0, 0, 2, 1]}))
    assert nodata != nodata                      # NaN nodata (self-inequality)
    assert np.isnan(out[0, 1])


# --------------------------------------------------------------------------- #
# raster_cog: direct_window url_by_param + round_pixel + nodata_gate (wave-8; gcn250)
# --------------------------------------------------------------------------- #


class _FakeSrc:
    """Minimal windowed-COG stand-in for direct_window unit tests."""
    def __init__(self, arr, nodata):
        self._arr = arr
        self.nodata = nodata
        self.height, self.width = arr.shape
        self.crs = "EPSG:4326"
        self.transform = rtransform.from_bounds(0, 0, 1, 1, self.width, self.height)

    def read(self, _band, window=None):
        r0 = int(getattr(window, "row_off", 0)); c0 = int(getattr(window, "col_off", 0))
        h = int(getattr(window, "height", self.height)); w = int(getattr(window, "width", self.width))
        return self._arr[r0:r0 + h, c0:c0 + w]

    def window_transform(self, window):
        return rtransform.from_bounds(0, 0, 1, 1, self.width, self.height)


def _dw_spec(**ingest_extra) -> SourceSpec:
    ingest = {"access": "direct_window"}
    ingest.update(ingest_extra)
    return SourceSpec.model_validate({
        "name": "fetch_demo_dw", "source_class": "demo_dw", "error_prefix": "DEMO_DW",
        "empty_error_suffix": "EMPTY", "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://h/default.tif"}},
        "params": {"bbox": {"type": "bbox", "required": True},
                   "amc": {"type": "enum", "default": "average", "values": ["dry", "average", "wet"]}},
        "ingest": ingest,
        "normalize": {"crs": "EPSG:4326", "units": "curve_number"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                   "style_preset": "curve_number"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 2.0},
    })


def test_direct_window_url_by_param_selects_endpoint(monkeypatch):
    """url_by_param maps an enum value to the object URL (gcn250 AMC -> figshare)."""
    seen = {}
    import contextlib
    @contextlib.contextmanager
    def _fake_open(url):
        seen["url"] = url
        yield _FakeSrc(np.array([[70, 71], [72, 73]], dtype="uint8"), 255.0)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _fake_open)
    spec = _dw_spec(url_by_param={"param": "amc", "map": {"dry": "https://h/d.tif", "wet": "https://h/w.tif"}})
    raster_cog._direct_window_to_array(spec, {"bbox": [0, 0, 1, 1], "amc": "wet"})
    assert seen["url"] == "https://h/w.tif"


def test_direct_window_nodata_gate_raises_empty(monkeypatch):
    """An all-nodata window is honest no-coverage -> typed EMPTY (never fabricated)."""
    import contextlib
    @contextlib.contextmanager
    def _fake_open(url):
        yield _FakeSrc(np.full((3, 3), 255, dtype="uint8"), 255.0)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _fake_open)
    spec = _dw_spec(nodata_gate=True, default_nodata=255)
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": [0, 0, 1, 1], "amc": "average"})
    assert ei.value.error_code == "DEMO_DW_EMPTY"


def test_direct_window_nodata_gate_passes_with_data(monkeypatch):
    """A window with at least one valid pixel is NOT gated as empty."""
    import contextlib
    @contextlib.contextmanager
    def _fake_open(url):
        yield _FakeSrc(np.array([[255, 80], [255, 255]], dtype="uint8"), 255.0)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _fake_open)
    spec = _dw_spec(nodata_gate=True)
    arr, _tf, _crs = raster_cog._direct_window_to_array(spec, {"bbox": [0, 0, 1, 1], "amc": "average"})
    assert 80.0 in arr  # the one valid pixel survived; the gate did NOT fire


# --------------------------------------------------------------------------- #
# raster_cog: multi_url VRT fan-out mosaic (fold wave-9, ADR 0055; hrsl)
# --------------------------------------------------------------------------- #


class _FakeMember:
    """A windowed member COG stand-in for the multi_url mosaic unit tests."""
    def __init__(self, arr):
        self._arr = np.asarray(arr)

    def read(self, _band, window=None, out_shape=None):
        r0 = int(getattr(window, "row_off", 0)); c0 = int(getattr(window, "col_off", 0))
        h = int(getattr(window, "height", self._arr.shape[0]))
        w = int(getattr(window, "width", self._arr.shape[1]))
        return self._arr[r0:r0 + h, c0:c0 + w]


def _mu_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_mu", "source_class": "demo_mu", "error_prefix": "DEMO_MU",
        "empty_error_suffix": "EMPTY", "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://h/mosaic.vrt"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"access": "multi_url", "multi_url": {"mode": "vrt"}},
        "normalize": {"crs": "EPSG:4326", "units": "persons_per_cell"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                   "style_preset": "population_density"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 12.0},
    })


def _two_tile_grid(a, b):
    """A 4x4 mosaic split into a left (A) + right (B) 4x2 tile pair (1:1 src/dst)."""
    tf = rtransform.from_bounds(0, 0, 4, 4, 4, 4)
    srcA = raster_cog._VrtSource("A", (0, 0, 2, 4), (0, 0, 2, 4))
    srcB = raster_cog._VrtSource("B", (0, 0, 2, 4), (2, 0, 2, 4))
    return tf, 4, 4, "EPSG:4326", float("nan"), [srcA, srcB]


def test_multi_url_mosaic_pastes_members(monkeypatch):
    """The fan-out reads each intersecting member's sub-window + pastes it in place."""
    A = np.array([[1, 2]] * 4, dtype="float64")   # left half -> cols 0,1
    B = np.array([[3, 4]] * 4, dtype="float64")   # right half -> cols 2,3
    monkeypatch.setattr(raster_cog, "_resolve_multi_url_members", lambda s, p: _two_tile_grid(A, B))
    import contextlib
    @contextlib.contextmanager
    def _fake_open(url):
        yield _FakeMember(A if url == "A" else B)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _fake_open)
    arr, tf, crs = raster_cog._multi_url_to_array(_mu_spec(), {"bbox": [0, 0, 4, 4]})
    assert arr.shape == (4, 4) and str(crs) == "EPSG:4326"
    assert list(arr[0]) == [1.0, 2.0, 3.0, 4.0]        # A pasted left, B pasted right
    assert list(arr[3]) == [1.0, 2.0, 3.0, 4.0]


def test_multi_url_all_nodata_window_is_empty(monkeypatch):
    """A window whose members are entirely NaN is honest no-coverage -> typed EMPTY."""
    nan2 = np.full((4, 2), np.nan, dtype="float64")
    monkeypatch.setattr(raster_cog, "_resolve_multi_url_members", lambda s, p: _two_tile_grid(nan2, nan2))
    import contextlib
    @contextlib.contextmanager
    def _fake_open(url):
        yield _FakeMember(nan2)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _fake_open)
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._multi_url_to_array(_mu_spec(), {"bbox": [0, 0, 4, 4]})
    assert ei.value.error_code == "DEMO_MU_EMPTY"


def test_multi_url_member_read_failure_is_upstream(monkeypatch):
    """ANY intersecting-member read failure -> typed UPSTREAM (never a silent partial)."""
    from trid3nt_server.agent.tools.fetchers._router import transport as _tp
    A = np.ones((4, 2), dtype="float64")
    monkeypatch.setattr(raster_cog, "_resolve_multi_url_members", lambda s, p: _two_tile_grid(A, A))
    import contextlib
    @contextlib.contextmanager
    def _boom_open(url):
        raise _tp.TransportNotFound("member 404")
        yield  # pragma: no cover
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.open_windowed_cog", _boom_open)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._multi_url_to_array(_mu_spec(), {"bbox": [0, 0, 4, 4]})
    assert ei.value.error_code == "DEMO_MU_UPSTREAM_ERROR"


def test_parse_vrt_reads_grid_and_members():
    """The VRT parser lifts the geotransform / size / nodata + member rects."""
    vrt = (
        b'<VRTDataset rasterXSize="8" rasterYSize="6">'
        b'<SRS>GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
        b'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0],'
        b'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]</SRS>'
        b'<GeoTransform>0.0, 1.0, 0.0, 6.0, 0.0, -1.0</GeoTransform>'
        b'<VRTRasterBand dataType="Float64" band="1"><NoDataValue>nan</NoDataValue>'
        b'<ComplexSource><SourceFilename relativeToVRT="1">tiles/a.tif</SourceFilename>'
        b'<SrcRect xOff="0" yOff="0" xSize="4" ySize="6"/>'
        b'<DstRect xOff="0" yOff="0" xSize="4" ySize="6"/></ComplexSource>'
        b'</VRTRasterBand></VRTDataset>'
    )
    tf, xs, ys, crs, nod, srcs = raster_cog._parse_vrt(vrt, "https://h/dir/mosaic.vrt")
    assert (xs, ys) == (8, 6) and nod != nod  # NaN nodata
    assert srcs[0].url == "https://h/dir/tiles/a.tif"
    assert (srcs[0].dx, srcs[0].dy, srcs[0].dw, srcs[0].dh) == (0, 0, 4, 6)


# --------------------------------------------------------------------------- #
# raster_cog: gzip_object whole-object date-templated read (fold wave-9; chirps)
# --------------------------------------------------------------------------- #


def _gz_spec(**go_extra) -> SourceSpec:
    go = {
        "date_param": "date", "period_param": "period", "min_year": 1981,
        "nodata_sentinel": -9000.0,
        "url_templates": {
            "monthly": "{base}/m/chirps-v2.0.{year:04d}.{month:02d}.tif.gz",
            "daily": "{base}/d/{year:04d}/chirps-v2.0.{year:04d}.{month:02d}.{day:02d}.tif.gz",
        },
    }
    go.update(go_extra)
    return SourceSpec.model_validate({
        "name": "fetch_demo_gz", "source_class": "demo_gz", "error_prefix": "DEMO_GZ",
        "shape": "raster-cog", "supports_global_query": True,
        "endpoints": {"data": {"url": "https://h/base"}},
        "params": {"bbox": {"type": "bbox", "required": False, "schema_optional": True},
                   "date": {"type": "str", "required": True},
                   "period": {"type": "enum", "default": "monthly", "values": ["monthly", "daily"]}},
        "ingest": {"access": "gzip_object", "gzip_object": go,
                   "serialize": {"nodata": -9999.0, "dtype": "float32"}},
        "normalize": {"crs": "EPSG:4326", "units": "mm"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                   "style_preset": "precip_mm"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01},
    })


def test_gzip_url_templating_monthly_and_daily():
    """The period-selected template fills from a parsed date (monthly accepts YYYY-MM)."""
    spec = _gz_spec()
    u_m = raster_cog._resolve_gzip_url(spec, {"date": "2023-07", "period": "monthly"}, spec.ingest["gzip_object"])
    assert u_m == "https://h/base/m/chirps-v2.0.2023.07.tif.gz"
    u_d = raster_cog._resolve_gzip_url(spec, {"date": "2022-08-25", "period": "daily"}, spec.ingest["gzip_object"])
    assert u_d == "https://h/base/d/2022/chirps-v2.0.2022.08.25.tif.gz"


def test_gzip_bad_and_future_date_are_input_errors():
    """Malformed / pre-record / future dates are typed INPUT errors (pre-network)."""
    spec = _gz_spec()
    go = spec.ingest["gzip_object"]
    for bad in ({"date": "nope", "period": "monthly"},
                {"date": "1970-01", "period": "monthly"},
                {"date": "2999-01", "period": "monthly"}):
        with pytest.raises(RouterInputError) as ei:
            raster_cog._resolve_gzip_url(spec, bad, go)
        assert ei.value.error_code == "DEMO_GZ_INPUT_ERROR"


def _gz_tif(arr) -> bytes:
    import gzip as _gz
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                dtype="float32", crs="EPSG:4326",
                transform=rtransform.from_bounds(0, 0, arr.shape[1], arr.shape[0], arr.shape[1], arr.shape[0]))
    buf = io.BytesIO()
    with rasterio.open(buf, "w", **prof) as dst:
        dst.write(arr.astype("float32"), 1)
    return _gz.compress(buf.getvalue())


def test_gzip_object_windows_and_collapses_sentinel(monkeypatch):
    """gunzip + window + sentinel(<=-9000)->NaN; the serialize path stamps -9999 nodata."""
    arr = np.array([[5.0, 10.0], [-9999.0, 20.0]], dtype="float32")  # one sentinel pixel
    gz = _gz_tif(arr)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_client", lambda: None)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_bytes",
                        lambda *a, **k: (gz, "application/gzip", "u"))
    out = raster_cog.execute(_gz_spec(), {"date": "2023-07", "period": "monthly"})
    got, nodata, dtype = _read_cog(out)
    assert nodata == -9999.0 and dtype == "float32"
    assert got[0, 0] == 5.0 and got[1, 1] == 20.0        # data preserved
    assert got[1, 0] == -9999.0                          # sentinel collapsed to nodata


def test_gzip_object_all_nodata_is_empty(monkeypatch):
    """A whole-nodata grid is honest no-coverage -> typed EMPTY."""
    gz = _gz_tif(np.full((2, 2), -9999.0, dtype="float32"))
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_client", lambda: None)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_bytes",
                        lambda *a, **k: (gz, "application/gzip", "u"))
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._gzip_object_to_array(_gz_spec(), {"date": "2023-07", "period": "monthly"})
    assert ei.value.error_code == "DEMO_GZ_EMPTY"


def test_gzip_object_404_is_not_available(monkeypatch):
    """A 404 for an unpublished date -> typed NOT_AVAILABLE (non-retryable)."""
    from trid3nt_server.agent.tools.fetchers._router import transport as _tp

    def _nf(*a, **k):
        raise _tp.TransportNotFound("forced 404")
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_client", lambda: None)
    monkeypatch.setattr("trid3nt_server.agent.tools.fetchers._router.transport.get_bytes", _nf)
    with pytest.raises(RouterNotAvailableError) as ei:
        raster_cog._gzip_object_to_array(_gz_spec(), {"date": "2023-07", "period": "monthly"})
    assert ei.value.error_code == "DEMO_GZ_NOT_AVAILABLE"
