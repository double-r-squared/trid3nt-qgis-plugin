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
    FT_TO_M,
    GRADIENT_MAX_MM,
    NGVD29_TO_NAVD88_M,
    _fit_measured_gradient,
    _fit_plane,
    _planar_gradient_from_dem,
    _usable_well_heads,
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


# --------------------------------------------------------------------------- #
# 6. Measured-head gradient: datum ladder + plane fit (ADR 0166)
# --------------------------------------------------------------------------- #


def _now() -> "Any":
    from datetime import datetime, timezone

    return datetime(2026, 8, 6, tzinfo=timezone.utc)


def _reading(
    lon: float, lat: float, *, pcode: str, value: float, datum: str = "",
    date: str = "2024-06-01", status: str = "Approved", unit: str = "ft",
) -> dict[str, Any]:
    return {
        "lon": lon, "lat": lat,
        "props": {
            "site_no": f"{lon:.4f}_{lat:.4f}_{pcode}", "parameter_code": pcode,
            "parameter_label": "", "water_level": value, "unit": unit,
            "vertical_datum": datum, "datetime": f"{date}T12:00:00Z",
            "approval_status": status,
        },
    }


def test_usable_well_heads_depth_anchored_to_dem(tmp_path: Path) -> None:
    """Depth-to-water readings -> head = DEM land surface minus depth (NAVD88 m)."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "dem.tif"
    # Flat-ish DEM at 100 m so land surface is a known constant near the wells.
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=1e-4, slope_n=1e-4)
    feats = [
        _reading(lon0 - 0.005, lat0 + 0.004, pcode="72019", value=30.0),
        _reading(lon0 + 0.006, lat0 - 0.003, pcode="72019", value=25.0),
        _reading(lon0 + 0.002, lat0 + 0.006, pcode="72019", value=28.0),
    ]
    usable, meta = _usable_well_heads(
        feats, f"file://{dem}", lat0, lon0, now=_now(), recency_years=10.0
    )
    assert meta["usable_wells"] == 3
    assert meta["by_basis"] == {"dem_minus_depth": 3}
    # head = ~100 m (DEM) - depth_ft*0.3048.
    heads = {w["site_no"]: w["head_m"] for w in usable}
    for f, depth in [(feats[0], 30.0), (feats[1], 25.0), (feats[2], 28.0)]:
        sid = f["props"]["site_no"]
        assert heads[sid] == pytest.approx(100.0 - depth * FT_TO_M, abs=1.0)


def test_usable_well_heads_datum_permutations(tmp_path: Path) -> None:
    """Mixed depth / NAVD88 / NGVD29 / artesian / stale / local-datum readings.

    Verifies the datum ladder: depth->DEM-anchored, NAVD88 direct, NGVD29 shifted,
    and the exclusions (artesian depth<=0, stale beyond recency, non-georeferenced
    'Local Assumed Datum', rejected status)."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "dem.tif"
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=1e-4, slope_n=1e-4)
    feats = [
        _reading(lon0 - 0.005, lat0 + 0.004, pcode="72019", value=30.0),          # depth
        _reading(lon0 + 0.006, lat0 - 0.003, pcode="62611", value=560.0, datum="NAVD88"),  # elev NAVD88
        _reading(lon0 + 0.002, lat0 + 0.006, pcode="62610", value=560.0, datum="NGVD29"),  # elev NGVD29
        _reading(lon0 - 0.001, lat0 - 0.006, pcode="72019", value=-2.0),          # artesian (excl)
        _reading(lon0 + 0.004, lat0 + 0.001, pcode="72019", value=27.0, date="2005-01-01"),  # stale (excl)
        _reading(lon0 + 0.003, lat0 + 0.002, pcode="62611", value=560.0, datum="Local Assumed Datum"),  # excl
        _reading(lon0 - 0.004, lat0 - 0.001, pcode="72019", value=26.0, status="Rejected"),  # excl
    ]
    usable, meta = _usable_well_heads(
        feats, f"file://{dem}", lat0, lon0, now=_now(), recency_years=10.0
    )
    assert meta["by_basis"] == {
        "dem_minus_depth": 1, "elev_navd88": 1, "elev_ngvd29_shifted": 1
    }
    assert meta["usable_wells"] == 3
    # Exclusions surfaced honestly.
    assert meta["excluded"].get("artesian_or_above_surface") == 1
    assert meta["excluded"].get("stale") == 1
    assert meta["excluded"].get("elev_unusable_datum") == 1
    assert meta["excluded"].get("rejected_status") == 1
    # NGVD29 elevation carries the nominal shift; NAVD88 does not.
    by_basis = {w["basis"]: w for w in usable}
    assert by_basis["elev_navd88"]["head_m"] == pytest.approx(560.0 * FT_TO_M, abs=1e-6)
    assert by_basis["elev_ngvd29_shifted"]["head_m"] == pytest.approx(
        560.0 * FT_TO_M + NGVD29_TO_NAVD88_M, abs=1e-6
    )


def _wells_on_head_plane(
    lat0: float, lon0: float, *, slope_e: float, slope_n: float, base: float = 550.0
) -> list[dict[str, Any]]:
    """NAVD88-elevation wells whose head elevation lies on a known planar surface."""
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    offs = [(-0.006, 0.005), (0.007, -0.004), (0.003, 0.006), (-0.005, -0.006), (0.006, 0.002)]
    feats = []
    for i, (dlon, dlat) in enumerate(offs):
        lon, lat = lon0 + dlon, lat0 + dlat
        east = dlon * m_per_deg_lon
        north = dlat * m_per_deg_lat
        head_m = base + slope_e * east + slope_n * north
        feats.append(
            _reading(lon, lat, pcode="62611", value=head_m / FT_TO_M, datum="NAVD88",
                     date=f"2024-0{(i % 6) + 1}-15")
        )
    return feats


def test_fit_measured_gradient_recovers_plane() -> None:
    """A head plane sloping up-east -> gradient east, flow azimuth ~270, tiny residual."""
    lat0, lon0 = 40.86, -98.40
    feats = _wells_on_head_plane(lat0, lon0, slope_e=0.003, slope_n=0.0)
    usable, _meta = _usable_well_heads(
        feats, None, lat0, lon0, now=_now(), recency_years=10.0
    )
    fit, reason = _fit_measured_gradient(usable)
    assert fit is not None and reason == "ok"
    assert fit["magnitude"] == pytest.approx(0.003, rel=0.05)
    assert fit["azimuth"] == pytest.approx(270.0, abs=3.0)  # flows west
    assert fit["residual_m"] < 0.05
    assert fit["n"] == 5


def test_fit_measured_gradient_too_few_wells() -> None:
    lat0, lon0 = 40.86, -98.40
    feats = _wells_on_head_plane(lat0, lon0, slope_e=0.003, slope_n=0.0)[:2]
    usable, _m = _usable_well_heads(feats, None, lat0, lon0, now=_now(), recency_years=10.0)
    fit, reason = _fit_measured_gradient(usable)
    assert fit is None and "too_few_wells" in reason


def test_fit_measured_gradient_collinear_is_degenerate() -> None:
    """Wells strung along one line -> the cross-gradient is unconstrained -> None."""
    lat0, lon0 = 40.86, -98.40
    m_per_deg_lat = 110_540.0
    feats = []
    for i, d in enumerate([-0.006, -0.003, 0.0, 0.003, 0.006]):
        lat = lat0 + d
        head = 550.0 + 0.003 * (d * m_per_deg_lat)
        feats.append(_reading(lon0, lat, pcode="62611", value=head / FT_TO_M,
                              datum="NAVD88", date=f"2024-0{(i % 6) + 1}-10"))
    usable, _m = _usable_well_heads(feats, None, lat0, lon0, now=_now(), recency_years=10.0)
    fit, reason = _fit_measured_gradient(usable)
    assert fit is None and "degenerate_spread" in reason


# --------------------------------------------------------------------------- #
# 7. Composer: measured-heads threading + loud fallback to the DEM proxy
# --------------------------------------------------------------------------- #


def _patch_measured(
    monkeypatch: pytest.MonkeyPatch, *, wells_feats: list[dict[str, Any]],
    dem_path: Path,
) -> dict:
    """Wire fetch_usgs_groundwater_levels + fetch_dem to local artifacts; fake the run."""
    import geopandas as gpd
    from shapely.geometry import Point

    # Write the wells FGB the composer's _read_wells_features will read back.
    props = [dict(f["props"]) for f in wells_feats]
    geom = [Point(f["lon"], f["lat"]) for f in wells_feats]
    wells_fgb = dem_path.parent / "wells.fgb"
    gpd.GeoDataFrame(props, geometry=geom, crs="EPSG:4326").to_file(
        str(wells_fgb), driver="FlatGeobuf", engine="pyogrio"
    )

    captured: dict[str, Any] = {}

    async def _fake_run(run_args: Any, **_kw: Any) -> CaptureZoneLayerURI:
        captured["run_args"] = run_args
        # The adapter would label a supplied vector "dem"; the composer relabels it.
        return _fake_layer("dem")

    import trid3nt_server.agent.tools.simulation.modflow.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)
    monkeypatch.setattr(
        pp, "_upload_fgb",
        lambda local_fgb, run_id, runs_bucket, **kw: f"file://{local_fgb}",
    )

    def _fetch_gw(**kw: Any) -> dict:
        return {"uri": f"file://{wells_fgb}"}

    def _fetch_dem(**kw: Any) -> dict:
        return {"uri": f"file://{dem_path}"}

    monkeypatch.setattr(
        cz_mod, "TOOL_REGISTRY",
        {"fetch_usgs_groundwater_levels": SimpleNamespace(fn=_fetch_gw),
         "fetch_dem": SimpleNamespace(fn=_fetch_dem)},
    )
    return captured


@pytest.mark.asyncio
async def test_composer_threads_measured_gradient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enough usable wells -> run_args carries the measured vector; source=measured_heads."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "dem.tif"
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=1e-4, slope_n=1e-4)
    feats = _wells_on_head_plane(lat0, lon0, slope_e=0.003, slope_n=0.0)
    captured = _patch_measured(monkeypatch, wells_feats=feats, dem_path=dem)

    result = await model_capture_zone_scenario(
        aoi_latlon=(lat0, lon0),
        well_location_latlon=(lat0, lon0),
        use_measured_heads=True,
        use_dem_gradient=True,
    )
    ra = captured["run_args"]
    assert ra.regional_gradient_x is not None
    assert ra.regional_gradient_x == pytest.approx(0.003, rel=0.1)
    assert abs(ra.regional_gradient_y) < 5e-4
    assert result.summary["gradient_source"] == "measured_heads"
    assert result.capture_zone_layer.gradient_source == "measured_heads"
    assert result.summary["gradient_well_count"] == 5
    assert result.summary["gradient_fit_residual_m"] is not None
    assert "MEASURED heads" in result.summary["gradient_caveat"]
    assert len(result.derived_params["used_wells"]) == 5


@pytest.mark.asyncio
async def test_composer_measured_falls_back_to_dem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Too few usable wells -> loud fall back to the DEM proxy (gradient_source='dem')."""
    lat0, lon0 = 40.86, -98.40
    dem = tmp_path / "dem.tif"
    # Real east-tilted DEM so the DEM-proxy 2nd rung yields a usable gradient.
    _write_tilted_dem(dem, lat0=lat0, lon0=lon0, slope_e=0.01, slope_n=0.0)
    feats = _wells_on_head_plane(lat0, lon0, slope_e=0.003, slope_n=0.0)[:2]  # only 2
    captured = _patch_measured(monkeypatch, wells_feats=feats, dem_path=dem)

    result = await model_capture_zone_scenario(
        aoi_latlon=(lat0, lon0),
        well_location_latlon=(lat0, lon0),
        use_measured_heads=True,
        use_dem_gradient=True,
    )
    assert result.summary["gradient_source"] == "dem"
    assert "measured heads unusable" in result.summary["gradient_caveat"]
    assert "too_few_wells" in (result.derived_params["measured_fallback_reason"] or "")
    assert captured["run_args"].regional_gradient_x is not None  # DEM vector threaded
