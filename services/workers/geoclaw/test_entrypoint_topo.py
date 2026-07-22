"""Worker-side topo normalization tests (entrypoint topotype-3 conversion).

These exercise the fix for the "Total mass at initial time = 0" tsunami blocker:
the agent stages the topo/bathy DEM as a GeoTIFF, but ``setrun.py`` references it
as ``[3, "topo.asc"]`` (GeoClaw topotype-3 ESRI ASCII). GeoClaw's Fortran reader
cannot parse GeoTIFF bytes, so the bathymetry never loads and the still-water IC
``h = max(0, sea_level - B)`` finds no wet cell -> Total mass 0 -> dry domain, no
tsunami. ``_normalize_topo_files`` converts the staged GeoTIFF to a genuine
topotype-3 ASCII (filling nodata so the OCEAN initializes WET for an offshore
source). Dep-guarded: skips when rasterio / clawpack are not installed (the
``test_setrun_builder`` suite stays import-free).
"""
from __future__ import annotations

from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
np = pytest.importorskip("numpy")
topotools = pytest.importorskip("clawpack.geoclaw.topotools")

from services.workers.geoclaw.entrypoint import (  # noqa: E402
    GeoClawBathymetryFlatError,
    _normalize_topo_files,
)


def _write_geotiff(path: Path, Z: "np.ndarray", *, nodata=float("nan")) -> None:
    """Write a small EPSG:4326 GeoTIFF mimicking the staged topo/bathy DEM."""
    from rasterio.transform import from_origin

    ny, nx = Z.shape
    res = 0.01  # SQUARE cells (topotype-3 needs one cellsize)
    transform = from_origin(-124.6, 42.0, res, res)
    prof = dict(
        driver="GTiff",
        height=ny,
        width=nx,
        count=1,
        dtype="float64",
        crs="EPSG:4326",
        transform=transform,
        nodata=nodata,
    )
    with rasterio.open(str(path), "w", **prof) as d:
        d.write(Z, 1)


def _still_water_mass(asc_path: Path, sea_level: float = 0.0):
    """Read a topotype-3 ASCII and compute the GeoClaw still-water IC mass."""
    T = topotools.Topography(str(asc_path), topo_type=3)
    T.read()
    B = np.asarray(T.Z, dtype="float64")
    h = np.maximum(0.0, sea_level - np.where(np.isfinite(B), B, 99999.0))
    return float(h.sum()), int((h > 0).sum()), int(B.size)


def _bathy_grid() -> "np.ndarray":
    """West = deep ocean nodata (NaN), middle = bathy to -300, east = land."""
    ny, nx = 40, 60
    Z = np.full((ny, nx), np.nan, dtype="float64")
    for i in range(ny):
        for j in range(nx):
            if j < 20:
                Z[i, j] = np.nan  # uncovered offshore (warp-corner-like nodata)
            elif j < 35:
                Z[i, j] = -300.0 + (j - 20) * 18.0  # nearshore bathy (wet)
            else:
                Z[i, j] = (j - 35) * 20.0  # land
    return Z


def test_geotiff_staged_as_asc_is_unreadable_as_topotype3(tmp_path: Path) -> None:
    """Guard: a raw GeoTIFF named topo.asc CANNOT be read as topotype-3 (the bug)."""
    p = tmp_path / "topo.asc"
    _write_geotiff(p, _bathy_grid())
    with pytest.raises(Exception):
        T = topotools.Topography(str(p), topo_type=3)
        T.read()


def test_normalize_tsunami_initializes_ocean_wet(tmp_path: Path) -> None:
    """Offshore (tsunami): nodata-filled ocean -> still-water IC mass > 0."""
    p = tmp_path / "topo.asc"
    _write_geotiff(p, _bathy_grid())
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})

    mass, wet, total = _still_water_mass(p)
    assert wet > 0, "ocean must initialize WET after conversion"
    assert mass > 0.0, f"Total mass at init must be > 0 (got {mass})"
    # all cells finite (nodata filled), so GeoClaw never sees topo_missing.
    T = topotools.Topography(str(p), topo_type=3)
    T.read()
    assert np.isfinite(T.Z).all()
    # negative = below sea level preserved (NO sign flip).
    assert T.Z.min() < 0.0 < T.Z.max()


def test_normalize_dam_break_does_not_flood_inland(tmp_path: Path) -> None:
    """Inland (dam_break): nodata becomes high LAND (dry), not ocean."""
    # A land-only DEM with a nodata warp corner; no cell is below sea level.
    ny, nx = 30, 30
    Z = np.full((ny, nx), 50.0, dtype="float64")
    Z[:5, :5] = np.nan  # warp-corner nodata
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    _normalize_topo_files(tmp_path, {"scenario": "dam_break", "topo_file": "topo.asc"})

    mass, wet, _ = _still_water_mass(p)
    assert wet == 0, "inland dam_break must NOT spuriously flood (no wet cells)"
    assert mass == 0.0


def test_normalize_downsamples_oversized_topo(tmp_path: Path) -> None:
    """An over-fine DEM (> cap per axis) is integer-decimated so the topotype-3
    ASCII stays bounded -- while staying a valid, wet, negative-preserving topo."""
    from services.workers.geoclaw.entrypoint import _GEOCLAW_TOPO_MAX_CELLS_PER_AXIS

    cap = _GEOCLAW_TOPO_MAX_CELLS_PER_AXIS
    # A DEM ~2.5x the cap on the long axis (stride 3 -> ~1/3 the cells).
    nx = cap * 5 // 2
    ny = cap // 2
    # West half ocean (-200 m), east half land (+30 m) -> wet for tsunami.
    Z = np.zeros((ny, nx), dtype="float64")
    Z[:, : nx // 2] = -200.0
    Z[:, nx // 2 :] = 30.0
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})

    T = topotools.Topography(str(p), topo_type=3)
    T.read()
    out_ny, out_nx = np.asarray(T.Z).shape
    # decimated under the cap on the long axis (general: a few million cells max).
    assert out_nx <= cap and out_ny <= cap
    assert out_nx < nx  # actually downsampled
    # still a valid wet, negative-preserving topo (no science regression).
    assert np.isfinite(T.Z).all()
    assert T.Z.min() < 0.0 < T.Z.max()


def test_normalize_keeps_small_topo_untouched(tmp_path: Path) -> None:
    """A DEM already under the cap is NOT decimated (full resolution kept)."""
    Z = _bathy_grid()  # 40x60, well under the cap
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})
    T = topotools.Topography(str(p), topo_type=3)
    T.read()
    assert np.asarray(T.Z).shape == Z.shape  # untouched resolution


def test_flat_ocean_gate_passes_on_real_bathymetry(tmp_path: Path) -> None:
    """P0.3: a genuinely-negative ocean topo PASSES the flat-ocean gate."""
    p = tmp_path / "topo.asc"
    _write_geotiff(p, _bathy_grid())  # bathy to -300, ~25% of cells deep
    # No raise -> gate passed; ocean still wet.
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})
    mass, wet, _ = _still_water_mass(p)
    assert wet > 0 and mass > 0.0


def test_flat_ocean_gate_fails_on_flat_land_fill(tmp_path: Path) -> None:
    """P0.3: a flat ~-0.7 m land-DEM 'ocean' FAILS loudly (GEOCLAW_BATHYMETRY_FLAT).

    This is the exact production root cause: a land-only DEM whose deepest cell is
    a near-zero coastal value. The OLD nanmin fill manufactured a flat fake ocean
    that silently passed; the gate now refuses the doomed dry solve. (P0.2 also
    means the offshore fill no longer invents ocean from this land min.)
    """
    ny, nx = 30, 30
    Z = np.full((ny, nx), -0.7, dtype="float64")  # flat near-zero 'ocean'
    Z[:5, :5] = 5.0  # a little land
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    with pytest.raises(GeoClawBathymetryFlatError) as ei:
        _normalize_topo_files(
            tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"}
        )
    assert ei.value.error_code == "GEOCLAW_BATHYMETRY_FLAT"
    assert "GEOCLAW_BATHYMETRY_FLAT" in str(ei.value)


def test_flat_ocean_gate_not_applied_to_dam_break(tmp_path: Path) -> None:
    """The gate is OFFSHORE-only: an inland dam_break (no ocean) does NOT raise."""
    ny, nx = 20, 20
    Z = np.full((ny, nx), 40.0, dtype="float64")  # all land
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    # dam_break -> no flat-ocean gate -> no raise.
    _normalize_topo_files(tmp_path, {"scenario": "dam_break", "topo_file": "topo.asc"})
    mass, wet, _ = _still_water_mass(p)
    assert wet == 0 and mass == 0.0


def test_offshore_fill_does_not_invent_ocean_from_land_min(tmp_path: Path) -> None:
    """P0.2: a land-only DEM with a nodata corner does NOT become a wet fake ocean.

    The old offshore branch filled nodata with nanmin (the deepest LAND value), so
    a land-only DEM with a tiny negative coastal cell became a flat wet ocean. Now
    the fill only uses a GENUINE below-water value when ocean exists; with no ocean
    the gate fails loudly instead.
    """
    ny, nx = 30, 30
    Z = np.full((ny, nx), 12.0, dtype="float64")  # land only, no real ocean
    Z[:6, :6] = np.nan  # warp-corner nodata
    p = tmp_path / "topo.asc"
    _write_geotiff(p, Z)
    with pytest.raises(GeoClawBathymetryFlatError):
        _normalize_topo_files(
            tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"}
        )


def test_normalize_is_idempotent(tmp_path: Path) -> None:
    """A GeoClaw ASCII is not a GDAL raster -> re-running leaves it untouched."""
    p = tmp_path / "topo.asc"
    _write_geotiff(p, _bathy_grid())
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})
    mass1, _, _ = _still_water_mass(p)
    # second pass: rasterio.open fails on the value-first GeoClaw header -> no-op.
    _normalize_topo_files(tmp_path, {"scenario": "tsunami", "topo_file": "topo.asc"})
    mass2, _, _ = _still_water_mass(p)
    assert mass1 == mass2 > 0.0
