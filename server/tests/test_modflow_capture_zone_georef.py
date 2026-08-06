"""Tests for the georeferenced-mode MODFLOW PRT capture zone (ADR 0165).

The georeferenced fold on the existing ``modflow_capture_zone`` surface adds:

  * a DEM-derived regional gradient (planar water-table proxy) that orients the
    CHD boundary so the capture zone extends up-gradient toward recharge, with a
    LOUD fallback to the demo west->east gradient when no usable DEM slope exists;
  * per-particle backtracked PATHLINES emitted as EPSG:4326 LineStrings in the
    capture-zone FlatGeobuf (the render's legibility element);
  * Grubb uniform-flow analytic screening scalars (capture width, stagnation
    distance) for a sanity ballpark against the PRT envelope.

Hermetic: no network (the DEM path is exercised with a locally-written GeoTIFF),
no mf6 (the postprocess runs on a synthetic PRT track CSV), no storage (the
FlatGeobuf upload is monkeypatched to a ``file://`` URI).
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trid3nt_contracts.modflow_contracts import CaptureZoneLayerURI

from trid3nt_server.agent.workflows.modflow import postprocess_modflow as pp
from trid3nt_server.agent.workflows.modflow.capture_zone import capture_zone as cz_mod
from trid3nt_server.agent.workflows.modflow.capture_zone.capture_zone import (
    GRADIENT_MAX_MM,
    _fit_plane,
    _planar_gradient_from_dem,
    model_capture_zone_scenario,
)


# --------------------------------------------------------------------------- #
# 1. Pure planar fit
# --------------------------------------------------------------------------- #


def test_fit_plane_recovers_slope() -> None:
    """z = 2x + 3y + 1 is recovered exactly (a, b, c) from >= 3 points."""
    xs = [0.0, 1.0, 0.0, 1.0, 2.0, -1.0]
    ys = [0.0, 0.0, 1.0, 1.0, 3.0, 2.0]
    zs = [2.0 * x + 3.0 * y + 1.0 for x, y in zip(xs, ys)]
    a, b, c = _fit_plane(xs, ys, zs)
    assert a == pytest.approx(2.0, abs=1e-9)
    assert b == pytest.approx(3.0, abs=1e-9)
    assert c == pytest.approx(1.0, abs=1e-9)


def test_fit_plane_too_few_points_raises() -> None:
    with pytest.raises(ValueError):
        _fit_plane([0.0, 1.0], [0.0, 1.0], [0.0, 1.0])


# --------------------------------------------------------------------------- #
# 2. DEM -> gradient (local GeoTIFF, no network)
# --------------------------------------------------------------------------- #


def _write_tilted_dem(path: Path, *, lat0: float, lon0: float, slope_e: float,
                      slope_n: float) -> None:
    """Write a small EPSG:4326 GeoTIFF whose elevation tilts by a known slope.

    Elevation increases toward the +east / +north by ``slope_e`` / ``slope_n``
    (m per metre), so the down-gradient (flow) direction is -(slope_e, slope_n).
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    n = 40
    deg = 0.02
    res = (2.0 * deg) / n
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    z = np.zeros((n, n), dtype="float32")
    for r in range(n):
        for c in range(n):
            lon = lon0 - deg + (c + 0.5) * res
            lat = lat0 + deg - (r + 0.5) * res
            east = (lon - lon0) * m_per_deg_lon
            north = (lat - lat0) * m_per_deg_lat
            z[r, c] = 100.0 + slope_e * east + slope_n * north
    transform = from_origin(lon0 - deg, lat0 + deg, res, res)
    with rasterio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(z, 1)


def test_planar_gradient_from_dem_direction_and_clamp(tmp_path: Path) -> None:
    """A DEM tilting up toward the east -> gradient points east, flow azimuth ~270."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "dem.tif"
    # Steep east slope (0.02 m/m up-east) -> within clamp; no north component.
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=0.02, slope_n=0.0)
    out = _planar_gradient_from_dem(f"file://{dem}", lat0, lon0)
    assert out is not None
    gx, gy, mag, az = out
    assert gx > 0 and abs(gy) < 1e-3
    assert mag == pytest.approx(0.02, rel=0.15)
    # Water flows down-gradient = west -> compass azimuth ~270 deg.
    assert az == pytest.approx(270.0, abs=5.0)


def test_planar_gradient_clamps_cliff(tmp_path: Path) -> None:
    """An extreme slope is clamped to GRADIENT_MAX_MM (direction preserved)."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "cliff.tif"
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=0.0, slope_n=0.5)
    out = _planar_gradient_from_dem(f"file://{dem}", lat0, lon0)
    assert out is not None
    gx, gy, mag, az = out
    assert mag == pytest.approx(GRADIENT_MAX_MM, rel=1e-6)
    assert gy > 0 and abs(gx) < 1e-6  # up-north preserved
    assert az == pytest.approx(180.0, abs=5.0)  # flows south


def test_planar_gradient_flat_returns_none(tmp_path: Path) -> None:
    """A near-flat DEM returns None so the caller uses the demo gradient."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "flat.tif"
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=1e-6, slope_n=1e-6)
    assert _planar_gradient_from_dem(f"file://{dem}", lat0, lon0) is None


# --------------------------------------------------------------------------- #
# 3. Directional CHD deck manifest (flopy, write=False, no mf6 run)
# --------------------------------------------------------------------------- #


def test_prt_deck_directional_gradient_manifest(tmp_path: Path) -> None:
    """A supplied gradient vector -> gradient_source='dem' + oriented azimuth."""
    from services.workers.modflow.gwt_adapter import build_modflow_deck

    manifest = build_modflow_deck(
        spill_location_latlon=(40.86, -98.40),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.25,
        workdir=str(tmp_path / "dem"),
        write=False,
        archetype="capture_zone",
        well_location_latlon=(40.86, -98.40),
        n_particles=8,
        capture_zone_travel_time_years=[1.0, 5.0, 10.0],
        regional_gradient_x=0.003,   # up-east
        regional_gradient_y=0.0,
    )
    assert manifest.gradient_source == "dem"
    assert manifest.gradient_magnitude == pytest.approx(0.003, rel=1e-6)
    # Flow (down-gradient) is west -> azimuth ~270.
    assert manifest.gradient_azimuth_deg == pytest.approx(270.0, abs=1.0)
    assert manifest.prt_present is True


def test_prt_deck_demo_gradient_manifest(tmp_path: Path) -> None:
    """No gradient vector -> the legacy demo west->east CHD (byte-identical)."""
    from services.workers.modflow.gwt_adapter import build_modflow_deck

    manifest = build_modflow_deck(
        spill_location_latlon=(40.86, -98.40),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.25,
        workdir=str(tmp_path / "demo"),
        write=False,
        archetype="capture_zone",
        well_location_latlon=(40.86, -98.40),
        n_particles=8,
        capture_zone_travel_time_years=[1.0, 5.0, 10.0],
    )
    assert manifest.gradient_source == "demo_west_east"
    assert manifest.gradient_azimuth_deg == pytest.approx(90.0, abs=1.0)  # flows east


# --------------------------------------------------------------------------- #
# 4. Postprocess: pathline LineStrings + Grubb scalars
# --------------------------------------------------------------------------- #


_WELL_LON = -98.40
_WELL_LAT = 40.86
_UTM_EPSG = 32614  # UTM 14N
_HALF = 2050.0


def _write_synthetic_track(csv_path: Path, n_particles: int = 8) -> None:
    """Synthetic PRT track CSV: particles migrate up-gradient over time (LOCAL)."""
    t_days = [100.0, 300.0, 800.0, 2000.0, 3500.0]
    rows = ["kper,kstp,imdl,iprp,irpt,ilay,icell,izone,istatus,ireason,trelease,t,x,y,z,name"]
    for p in range(n_particles):
        a = 2.0 * math.pi * p / n_particles
        for t in t_days:
            frac = t / 3500.0
            x = _HALF - frac * 1600.0 + 30.0 * math.cos(a)
            y = _HALF + 30.0 * math.sin(a) + frac * 200.0 * math.sin(a)
            rows.append(f"1,1,0,1,{p},0,0,0,0,1,0.0,{t},{x:.3f},{y:.3f},25.0,p{p}")
    csv_path.write_text("\n".join(rows) + "\n")


@pytest.fixture()
def _prt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    csv = tmp_path / "prtmodel.trk.csv"
    _write_synthetic_track(csv)
    monkeypatch.setattr(
        pp, "_upload_fgb",
        lambda local_fgb, run_id, runs_bucket, **kw: f"file://{local_fgb}",
    )
    return tmp_path


def _well_utm() -> tuple[float, float]:
    from pyproj import Transformer

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{_UTM_EPSG}", always_xy=True)
    return to_utm.transform(_WELL_LON, _WELL_LAT)


def test_postprocess_emits_pathlines_and_grubb(_prt_dir: Path) -> None:
    """The FGB carries pathline LineStrings + the layer carries Grubb scalars."""
    import geopandas as gpd

    east, north = _well_utm()
    layer = pp.postprocess_capture_zone(
        str(_prt_dir),
        run_id="georef-001",
        model_crs=f"EPSG:{_UTM_EPSG}",
        xoffset_m=east - _HALF,
        yoffset_m=north - _HALF,
        model_utm_epsg=_UTM_EPSG,
        tier_years=[1.0, 5.0, 10.0],
        gradient_source="dem",
        gradient_magnitude=0.004,
        gradient_azimuth_deg=270.0,
        k_m_per_day=8.64,          # 1e-4 m/s
        aquifer_thickness_m=50.0,
        pumping_rate_m3_day=800.0,
    )
    assert isinstance(layer, CaptureZoneLayerURI)
    # 8 particles -> 8 pathlines.
    assert layer.pathline_count == 8
    assert layer.gradient_source == "dem"
    assert layer.gradient_magnitude == pytest.approx(0.004)
    # Grubb: B = Q/(K b i); x0 = Q/(2 pi K b i).
    expected_width = 800.0 / (8.64 * 50.0 * 0.004)
    assert layer.capture_width_m == pytest.approx(expected_width, rel=1e-6)
    assert layer.stagnation_distance_m == pytest.approx(
        expected_width / (2.0 * math.pi), rel=1e-6
    )
    # The FGB actually contains 'pathline' LineString features at the well lon/lat.
    gdf = gpd.read_file(layer.uri.replace("file://", ""))
    kinds = set(gdf["feature_type"])
    assert "pathline" in kinds and "outer_envelope" in kinds and "isochrone" in kinds
    lines = gdf[gdf["feature_type"] == "pathline"]
    assert len(lines) == 8
    assert lines.geometry.iloc[0].geom_type == "LineString"
    # Lands near the real well (not the equator).
    minx, miny, maxx, maxy = gdf.total_bounds
    assert miny > 39.0 and maxy < 42.0
    assert minx > -100.0 and maxx < -97.0


def test_postprocess_no_grubb_without_params(_prt_dir: Path) -> None:
    """Without K/b/i/Q the Grubb scalars stay None (no fabricated numbers)."""
    east, north = _well_utm()
    layer = pp.postprocess_capture_zone(
        str(_prt_dir),
        run_id="georef-002",
        model_crs=f"EPSG:{_UTM_EPSG}",
        xoffset_m=east - _HALF,
        yoffset_m=north - _HALF,
        model_utm_epsg=_UTM_EPSG,
        tier_years=[1.0, 5.0, 10.0],
    )
    assert layer.capture_width_m is None
    assert layer.stagnation_distance_m is None
    assert layer.pathline_count == 8  # pathlines still emitted


# --------------------------------------------------------------------------- #
# 5. Composer DEM-gradient threading + loud fallback
# --------------------------------------------------------------------------- #


def _fake_layer(source: str = "dem") -> CaptureZoneLayerURI:
    # Mirror the adapter contract: a fake run tool that builds the deck with the
    # threaded gradient would emit a layer whose gradient_source matches; the
    # tests set ``source`` to what the composer would have requested.
    return CaptureZoneLayerURI(
        layer_id="cz", name="cz", layer_type="vector",
        uri="file:///tmp/cz.fgb", style_preset="capture_zone", role="primary",
        capture_zone_area_km2=2.0, travel_time_years=[1.0, 5.0, 10.0],
        isochrone_areas_km2={"1": 0.1, "5": 0.5, "10": 2.0}, particle_count=16,
        pathline_count=16, gradient_source=source,
        gradient_magnitude=(0.004 if source == "dem" else None),
        gradient_azimuth_deg=(270.0 if source == "dem" else None),
    )


def _patch_dem(
    monkeypatch: pytest.MonkeyPatch, *, fetch: Any, grad: Any,
    layer_source: str = "dem",
) -> dict:
    captured: dict[str, Any] = {}

    async def _fake_run(run_args: Any, **_kw: Any) -> CaptureZoneLayerURI:
        captured["run_args"] = run_args
        return _fake_layer(layer_source)

    import trid3nt_server.agent.tools.simulation.modflow.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)
    monkeypatch.setattr(
        cz_mod, "TOOL_REGISTRY", {"fetch_dem": SimpleNamespace(fn=fetch)}
    )
    monkeypatch.setattr(cz_mod, "_planar_gradient_from_dem", grad)
    return captured


@pytest.mark.asyncio
async def test_composer_threads_dem_gradient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A usable DEM slope -> run_args carries the gradient vector; summary='dem'."""
    captured = _patch_dem(
        monkeypatch,
        fetch=lambda **kw: {"uri": "file:///fake.tif"},
        grad=lambda uri, lat, lon: (0.003, -0.001, 0.00316, 288.0),
    )
    result = await model_capture_zone_scenario(
        aoi_latlon=(40.86, -98.40),
        well_location_latlon=(40.86, -98.40),
        use_dem_gradient=True,
    )
    ra = captured["run_args"]
    assert ra.regional_gradient_x == pytest.approx(0.003)
    assert ra.regional_gradient_y == pytest.approx(-0.001)
    assert result.summary["gradient_source"] == "dem"
    assert "SCREENING proxy" in result.summary["gradient_caveat"]


@pytest.mark.asyncio
async def test_composer_falls_back_when_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None gradient (flat AOI) -> run_args gradient None, source demo, no raise."""
    captured = _patch_dem(
        monkeypatch,
        fetch=lambda **kw: {"uri": "file:///fake.tif"},
        grad=lambda uri, lat, lon: None,
        layer_source="demo_west_east",
    )
    result = await model_capture_zone_scenario(
        aoi_latlon=(40.86, -98.40),
        well_location_latlon=(40.86, -98.40),
        use_dem_gradient=True,
    )
    ra = captured["run_args"]
    assert ra.regional_gradient_x is None and ra.regional_gradient_y is None
    assert result.derived_params["gradient_source"] == "demo_west_east"
    assert "placeholder" in result.summary["gradient_caveat"]


@pytest.mark.asyncio
async def test_composer_falls_back_on_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DEM fetch exception is non-fatal: demo gradient, run still succeeds."""
    def _boom(**kw: Any) -> Any:
        raise RuntimeError("3DEP unreachable")

    captured = _patch_dem(
        monkeypatch, fetch=_boom, grad=lambda uri, lat, lon: (0.01, 0.0, 0.01, 270.0),
        layer_source="demo_west_east",
    )
    result = await model_capture_zone_scenario(
        aoi_latlon=(40.86, -98.40),
        well_location_latlon=(40.86, -98.40),
        use_dem_gradient=True,
    )
    assert captured["run_args"].regional_gradient_x is None
    assert result.derived_params["gradient_source"] == "demo_west_east"
