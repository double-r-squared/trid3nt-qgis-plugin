"""Composer test: soil-derived aquifer K threads a LABELED pedotransfer basis.

Offline: fakes ``fetch_soilgrids`` with tiny local texture rasters and stubs the
solver run, then asserts the composer derives K from texture, threads it into the
run args, and narrates the derived (near-surface proxy) provenance loudly. Also
asserts the loud fallback to the demo default when the soil fetch yields nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_contracts.modflow_contracts import CaptureZoneLayerURI
from trid3nt_server.workflows.modflow.capture_zone import capture_zone as cz_mod
from trid3nt_server.workflows.modflow.capture_zone.capture_zone import (
    model_capture_zone_scenario,
)
from trid3nt_server.workflows.shared.soil_hydraulics import ksat_from_texture

LAT0, LON0 = 40.86, -98.40


def _write_uniform_raster(path: Path, value: float) -> None:
    """A small uniform single-band EPSG:4326 raster covering the well point."""
    d = 0.05
    transform = from_bounds(LON0 - d, LAT0 - d, LON0 + d, LAT0 + d, 20, 20)
    arr = np.full((20, 20), float(value), dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=20, width=20, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform, nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)


def _fake_layer(source: str = "dem") -> CaptureZoneLayerURI:
    # Default to a DEM-derived gradient so these tests isolate the aquifer-K seam:
    # under law 9 a demo (west->east placeholder) gradient is a physics default that
    # REFUSES, which would mask the K path. The K-fallback test asserts that refusal.
    return CaptureZoneLayerURI(
        layer_id="cz-TEST", name="Capture Zone", layer_type="vector",
        uri="file:///tmp/cz.fgb", style_preset="capture_zone", role="primary",
        capture_zone_area_km2=3.0, travel_time_years=[1.0, 5.0, 10.0],
        isochrone_areas_km2={"1.0": 0.5, "5.0": 1.5, "10.0": 3.0},
        particle_count=16, pathline_count=16, gradient_source=source,
        gradient_magnitude=0.0012, gradient_azimuth_deg=90.0,
    )


def _patch(monkeypatch: pytest.MonkeyPatch, *, sand: float | None, clay: float | None,
           tmp: Path) -> dict:
    captured: dict[str, Any] = {}

    async def _fake_run(run_args: Any, **_kw: Any) -> CaptureZoneLayerURI:
        captured["run_args"] = run_args
        return _fake_layer()

    import trid3nt_server.data.simulation.modflow.run_modflow_archetype_tool as _tool
    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    uris: dict[str, str] = {}
    if sand is not None:
        p = tmp / "sand.tif"; _write_uniform_raster(p, sand); uris["sand"] = f"file://{p}"
    if clay is not None:
        p = tmp / "clay.tif"; _write_uniform_raster(p, clay); uris["clay"] = f"file://{p}"

    def _fetch_soil(**kw: Any) -> dict:
        prop = kw.get("soil_property")
        uri = uris.get(prop)
        return {"uri": uri} if uri else {"uri": None}

    # The SoilGrids pedotransfer read lives in the shared resolver seam now; patch
    # its module-level registry (capture_zone delegates to it).
    from trid3nt_server.workflows.shared import aquifer_resolve as ar_mod
    monkeypatch.setattr(
        ar_mod, "TOOL_REGISTRY",
        {"fetch_soilgrids": SimpleNamespace(fn=_fetch_soil)},
    )
    return captured


@pytest.mark.asyncio
async def test_soil_k_threads_derived_basis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sandy-loam texture -> derived K matches the pedotransfer seam, labeled loud."""
    captured = _patch(monkeypatch, sand=65.0, clay=10.0, tmp=tmp_path)
    result = await model_capture_zone_scenario(
        aoi_latlon=(LAT0, LON0),
        well_location_latlon=(LAT0, LON0),
        use_measured_heads=False,   # isolate the soil-K path
        use_dem_gradient=False,
        use_soil_k=True,
    )
    expected = ksat_from_texture(0.65, 0.10, depth_label="5-15cm").k_m_s
    assert result.summary["aquifer_k_source"] == "soil_pedotransfer"
    assert captured["run_args"].aquifer_k_ms == pytest.approx(expected, rel=1e-6)
    assert result.derived_params["soil_k"]["sand_pct"] == pytest.approx(65.0, abs=0.5)
    caveat = result.summary["aquifer_provenance"]
    assert "DERIVED" in caveat and "pedotransfer" in caveat
    assert "NOT a measured aquifer" in caveat


@pytest.mark.asyncio
async def test_soil_k_demo_fallback_refuses_in_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty soil raster + no user K -> the demo aquifer K REFUSES in auto (law 9).

    The pre-law-9 loud fallback to DEFAULT_AQUIFER_K_MS is gone: an invented
    hydraulic conductivity would silently ruin the delineation, so the gate cancels
    with a typed PHYSICS_INPUT_REQUIRED error naming the missing input.
    """
    _patch(monkeypatch, sand=None, clay=None, tmp=tmp_path)
    with pytest.raises(cz_mod.CaptureZoneInputError) as exc:
        await model_capture_zone_scenario(
            aoi_latlon=(LAT0, LON0),
            well_location_latlon=(LAT0, LON0),
            use_measured_heads=False,
            use_dem_gradient=False,
            use_soil_k=True,
        )
    msg = str(exc.value)
    assert "PHYSICS_INPUT_REQUIRED" in msg and "aquifer_k_ms" in msg


@pytest.mark.asyncio
async def test_user_k_overrides_soil(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller-supplied K wins; the soil fetch is not consulted."""
    captured = _patch(monkeypatch, sand=65.0, clay=10.0, tmp=tmp_path)
    result = await model_capture_zone_scenario(
        aoi_latlon=(LAT0, LON0),
        well_location_latlon=(LAT0, LON0),
        aquifer_k_ms=5e-5,
        use_measured_heads=False,
        use_dem_gradient=False,
        use_soil_k=True,
    )
    assert result.summary["aquifer_k_source"] == "user_supplied"
    assert captured["run_args"].aquifer_k_ms == pytest.approx(5e-5)
