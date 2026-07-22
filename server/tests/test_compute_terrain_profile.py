"""Unit tests for ``compute_terrain_profile`` (QGIS Profile-tool reimplementation).

All tests use synthetic in-memory/temp-file rasters -- no network, no LLM. The
tool is ``cacheable=False`` (a chart-emission mint per call), so no S3 double is
needed.

Coverage:
- Registration + metadata (cacheable=False, ttl_class=live-no-cache).
- A synthetic west->east ramp DEM -> a known, monotonic linear elevation profile
  along a horizontal line.
- The result is a structurally-valid ChartEmissionPayload riding the
  chart-emission chat-card path.
- Line input parsing: GeoJSON LineString, Feature, FeatureCollection, and a bare
  [lon,lat] list all resolve; malformed lines -> LINE_INVALID.
- CRS correctness (the headline guard): a UTM-CRS DEM is sampled CORRECTLY from
  an EPSG:4326 lon/lat line (stations reprojected into the raster CRS). The same
  ramp sampled from a 4326 DEM and the equivalent UTM DEM yields the same
  elevations -- proving the reprojection, not a lon/lat-vs-UTM mis-sample.
- Multi-DEM overlay: two DEMs on one line -> a two-surface chart.
- Honesty floor: a line entirely off every DEM -> LINE_OUTSIDE_RASTER (never a
  fabricated profile); nodata stations surface as null.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.charts_common import is_chart_emission_result
from trid3nt_server.tools.processing.compute_terrain_profile import (
    TerrainProfileError,
    _resolve_line_coords,
    compute_terrain_profile,
)
from trid3nt_contracts.chart_contracts import (
    ChartEmissionPayload,
    is_structurally_valid_vega_lite_spec,
)


# ---------------------------------------------------------------------------
# Synthetic-fixture helpers
# ---------------------------------------------------------------------------


def _make_raster(
    tmp_path: Path,
    values: np.ndarray,
    *,
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    crs: str = "EPSG:4326",
    nodata: float | None = None,
    units: str | None = None,
    name: str = "ramp.tif",
) -> str:
    import rasterio
    from rasterio.transform import from_bounds

    height, width = values.shape
    minx, miny, maxx, maxy = bounds
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": values.dtype,
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    path = str(tmp_path / name)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        if units is not None:
            dst.update_tags(1, UNITS=units)
            try:
                dst.units = (units,)
            except Exception:  # noqa: BLE001
                pass
    return path


def _x_ramp(height: int = 64, width: int = 64, *, scale: float = 100.0) -> np.ndarray:
    """West->east ramp: value ~ scale * (column-fraction). Row-invariant."""
    col = np.linspace(0.0, scale, width, dtype=np.float32)
    return np.tile(col, (height, 1))


def _assert_valid_chart_payload(payload: dict) -> dict:
    assert isinstance(payload, dict)
    assert payload["envelope_type"] == "chart-emission"
    assert is_chart_emission_result(payload)
    assert isinstance(payload["chart_id"], str) and payload["chart_id"]
    spec = payload["vega_lite_spec"]
    assert is_structurally_valid_vega_lite_spec(spec), spec
    ChartEmissionPayload.model_validate(payload)
    return spec


def _all_rows(spec: dict) -> list[dict]:
    rows: list[dict] = []
    data = spec.get("data")
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        rows.extend(data["values"])
    for layer in spec.get("layer", []) or []:
        ldata = layer.get("data")
        if isinstance(ldata, dict) and isinstance(ldata.get("values"), list):
            rows.extend(ldata["values"])
    return rows


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_compute_terrain_profile_registered():
    assert "compute_terrain_profile" in TOOL_REGISTRY
    md = TOOL_REGISTRY["compute_terrain_profile"].metadata
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"


# ---------------------------------------------------------------------------
# Line resolution
# ---------------------------------------------------------------------------


def test_line_resolution_accepts_all_forms():
    coords = [[0.1, 0.5], [0.9, 0.5]]
    assert _resolve_line_coords(coords) == coords
    assert _resolve_line_coords({"type": "LineString", "coordinates": coords}) == coords
    feat = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}}
    assert _resolve_line_coords(feat) == coords
    fc = {"type": "FeatureCollection", "features": [feat]}
    assert _resolve_line_coords(fc) == coords


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [[0.1, 0.5]],  # single vertex
        [[0.1, 0.5], [0.1, 0.5]],  # collapses to one distinct vertex
        {"type": "Point", "coordinates": [0, 0]},
        "not-a-line",
    ],
)
def test_malformed_line_raises(bad):
    with pytest.raises(TerrainProfileError) as exc:
        _resolve_line_coords(bad)
    assert exc.value.error_code == "LINE_INVALID"


# ---------------------------------------------------------------------------
# Ramp DEM -> monotonic profile
# ---------------------------------------------------------------------------


def test_ramp_dem_monotonic_profile(tmp_path):
    dem = _make_raster(tmp_path, _x_ramp(scale=100.0), units="m")
    # Horizontal line across the middle, west -> east.
    out = compute_terrain_profile(dem, [[0.05, 0.5], [0.95, 0.5]], n_samples=50)
    spec = _assert_valid_chart_payload(out)
    rows = _all_rows(spec)
    elevs = [r["elevation"] for r in rows if r["elevation"] is not None]
    assert len(elevs) >= 40
    # Monotonic non-decreasing along the west->east ramp.
    assert all(b >= a - 1e-3 for a, b in zip(elevs, elevs[1:]))
    assert max(elevs) > min(elevs)  # there IS relief


# ---------------------------------------------------------------------------
# CRS correctness -- the headline guard against the lon/lat-vs-UTM bug
# ---------------------------------------------------------------------------


def test_utm_dem_sampled_correctly_from_lonlat_line(tmp_path):
    """A DEM in UTM must be sampled correctly from a 4326 lon/lat line.

    Build the SAME ramp twice: once tagged EPSG:4326 over a small AOI, once
    warped into the matching UTM zone. Profiling the 4326 line over BOTH must
    return matching elevations -- if the tool failed to reproject the stations
    into the UTM CRS it would sample the wrong cells (or all nodata).
    """
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    # Small AOI near (lon=-90, lat=30) so it sits cleanly in a single UTM zone.
    aoi = (-90.02, 29.99, -89.98, 30.01)
    ramp = _x_ramp(width=80, height=80, scale=50.0)
    dem_4326 = _make_raster(tmp_path, ramp, bounds=aoi, crs="EPSG:4326", units="m", name="ll.tif")

    # Warp it to UTM 15N (EPSG:32615) -- the zone for ~ -90 lon.
    dst_crs = "EPSG:32615"
    with rasterio.open(dem_4326) as src:
        transform, w, h = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        utm_path = str(tmp_path / "utm.tif")
        kwargs = src.meta.copy()
        kwargs.update({"crs": dst_crs, "transform": transform, "width": w, "height": h})
        with rasterio.open(utm_path, "w", **kwargs) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
            dst.update_tags(1, UNITS="m")

    line = [[-90.015, 30.0], [-89.985, 30.0]]
    out_ll = compute_terrain_profile(dem_4326, line, n_samples=30)
    out_utm = compute_terrain_profile(utm_path, line, n_samples=30)

    ll_rows = [r["elevation"] for r in _all_rows(out_ll["vega_lite_spec"])]
    utm_rows = [r["elevation"] for r in _all_rows(out_utm["vega_lite_spec"])]

    # The UTM profile must have real (non-null) values -- proof the lon/lat line
    # was reprojected into the UTM CRS rather than read as raw UTM metres.
    utm_valid = [v for v in utm_rows if v is not None]
    assert len(utm_valid) >= 25
    # And it must MATCH the 4326 profile (same underlying ramp), not be garbage.
    paired = [
        (a, b) for a, b in zip(ll_rows, utm_rows) if a is not None and b is not None
    ]
    assert len(paired) >= 25
    for a, b in paired:
        assert abs(a - b) < 2.0  # within bilinear-resample tolerance on a 0..50 ramp


# ---------------------------------------------------------------------------
# Multi-DEM overlay
# ---------------------------------------------------------------------------


def test_multi_dem_overlay_two_surfaces(tmp_path):
    dem1 = _make_raster(tmp_path, _x_ramp(scale=100.0), units="m", name="ground.tif")
    dem2 = _make_raster(tmp_path, _x_ramp(scale=50.0), units="m", name="bathy.tif")
    out = compute_terrain_profile(
        dem1, [[0.05, 0.5], [0.95, 0.5]], n_samples=40, extra_layer_uris=[dem2]
    )
    spec = _assert_valid_chart_payload(out)
    rows = _all_rows(spec)
    surfaces = {r["surface"] for r in rows}
    assert len(surfaces) == 2  # two distinct overlaid surfaces


# ---------------------------------------------------------------------------
# Honesty floor
# ---------------------------------------------------------------------------


def test_line_entirely_off_raster_raises(tmp_path):
    dem = _make_raster(tmp_path, _x_ramp(), bounds=(0, 0, 1, 1), units="m")
    # Line far outside [0,1]x[0,1].
    with pytest.raises(TerrainProfileError) as exc:
        compute_terrain_profile(dem, [[50.0, 50.0], [51.0, 50.0]], n_samples=20)
    assert exc.value.error_code == "LINE_OUTSIDE_RASTER"


def test_nodata_stations_surface_as_null(tmp_path):
    arr = _x_ramp(scale=100.0).copy()
    # Carve a nodata band down the middle columns.
    arr[:, 28:36] = -9999.0
    dem = _make_raster(tmp_path, arr, nodata=-9999.0, units="m")
    out = compute_terrain_profile(dem, [[0.05, 0.5], [0.95, 0.5]], n_samples=60)
    rows = _all_rows(out["vega_lite_spec"])
    elevs = [r["elevation"] for r in rows]
    # Some stations are null (the nodata band) AND some are valid -- honest.
    assert any(v is None for v in elevs)
    assert any(v is not None for v in elevs)


def test_too_many_dems_raises(tmp_path):
    dem = _make_raster(tmp_path, _x_ramp(), units="m")
    with pytest.raises(TerrainProfileError) as exc:
        compute_terrain_profile(
            dem, [[0.1, 0.5], [0.9, 0.5]], extra_layer_uris=[dem, dem, dem, dem]
        )
    assert exc.value.error_code == "TOO_MANY_LAYERS"
