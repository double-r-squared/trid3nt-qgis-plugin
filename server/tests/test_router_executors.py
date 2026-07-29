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
