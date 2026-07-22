# SWAN worker -- bathy depth_fn sampling tests.
#
# Regression cover for the live 2026-06-23 Mexico Beach "invisible wave raster"
# bug: a staged fetch_topobathy DEM in a PROJECTED CRS (CUDEM tiles arrive in
# UTM, e.g. EPSG:32616) was sampled with raw EPSG:4326 lon/lat queries, so every
# bottom node fell out of bounds and fell back to the flat 10 m demo depth -- a
# uniform bottom that silently passes the all-dry guard yet carries no real
# bathymetry. These tests require rasterio (installed in the worker image); they
# skip cleanly where it is absent.

from __future__ import annotations

from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from services.workers.swan.entrypoint import (  # noqa: E402
    SwanBathyCoverageError,
    _author_deck,
    _build_depth_fn,
)

# A Gulf-of-Mexico-ish AOI (Mexico Beach), the bbox the live run used.
AOI = (-85.55, 29.85, -85.3, 30.05)


def _write_utm_dem(
    path: Path,
    *,
    crs: str,
    # UTM 16N extent that brackets the AOI: roughly the live tile footprint.
    x0: float = 639000.0,
    y1: float = 3326000.0,
    res: float = 30.0,
    nx: int = 900,
    ny: int = 800,
    fill: float = -10.0,  # below datum -> wet seabed everywhere (elev, +up)
) -> None:
    """Write a tiny single-band float32 DEM in a PROJECTED CRS (positive-up
    elevation). ``fill`` < 0 => seabed (wet once negated to positive-down depth)."""
    transform = from_origin(x0, y1, res, res)
    data = np.full((ny, nx), fill, dtype="float32")
    with rasterio.open(
        str(path),
        "w",
        driver="GTiff",
        height=ny,
        width=nx,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=float("nan"),
    ) as ds:
        ds.write(data, 1)


def test_build_depth_fn_reprojects_lonlat_into_utm_dem(tmp_path: Path):
    """A UTM DEM must be sampled via reprojected coords, NOT raw lon/lat.

    Pre-fix, ds.index(lon, lat) on a UTM dataset sent every query out of bounds
    and returned the 10.0 fallback. Post-fix the lon/lat is reprojected to the
    DEM CRS first, so an AOI node lands inside the raster and reads the real
    (negated) elevation.
    """
    dem = tmp_path / "bathy_utm.tif"
    _write_utm_dem(dem, crs="EPSG:32616", fill=-10.0)
    fn = _build_depth_fn(dem)
    assert fn is not None

    # AOI centre -- inside the DEM footprint after reprojection.
    cx = (AOI[0] + AOI[2]) / 2.0
    cy = (AOI[1] + AOI[3]) / 2.0
    depth = fn(cx, cy)
    # elevation -10 (positive-up) -> +10 positive-down depth (wet), NOT the
    # 10.0 fallback by luck: use a distinctive fill so the values differ.
    assert depth == pytest.approx(10.0, abs=0.5)
    stats = fn.sample_stats  # type: ignore[attr-defined]
    assert stats["fallback"] == 0, "a reprojected in-bounds query must not fall back"


def test_build_depth_fn_distinct_value_proves_real_sample(tmp_path: Path):
    """Use a fill that is NOT the 10.0 fallback so a real sample is unambiguous."""
    dem = tmp_path / "bathy_utm2.tif"
    _write_utm_dem(dem, crs="EPSG:32616", fill=-3.5)  # -> +3.5 depth
    fn = _build_depth_fn(dem)
    cx = (AOI[0] + AOI[2]) / 2.0
    cy = (AOI[1] + AOI[3]) / 2.0
    assert fn(cx, cy) == pytest.approx(3.5, abs=0.5)
    assert fn.sample_stats["fallback"] == 0  # type: ignore[attr-defined]


def _min_build_spec() -> dict:
    return {
        "mode": "stationary",
        "bbox": list(AOI),
        "bottom_file": "bottom.bot",
        "mx": 20,
        "my": 20,
        "n_dir": 36,
        "n_freq": 32,
        "freq_low_hz": 0.04,
        "freq_high_hz": 1.0,
        "boundary": {
            "hs_m": 3.0,
            "tp_s": 9.0,
            "dir_deg": 180.0,
            "spread_deg": 25.0,
            "side": "S",
        },
        "output_quantities": ["HSIGN", "RTP", "DIR"],
    }


def test_author_deck_raises_when_dem_covers_none_of_aoi(tmp_path: Path):
    """A DEM that does not overlap the AOI grid (all-fallback) must FAIL LOUD.

    Otherwise the flat 10 m demo fill is written everywhere, reads as uniformly
    wet, slips past the all-dry guard, and SWAN solves a meaningless flat basin
    (the invisible-raster bug). The coverage guard converts that into a typed
    SwanBathyCoverageError.
    """
    # A UTM DEM placed far from the AOI footprint -> every reprojected query is
    # out of bounds -> all fallback.
    dem = tmp_path / "bathy_offsite.tif"
    _write_utm_dem(dem, crs="EPSG:32616", x0=200000.0, y1=4000000.0, nx=50, ny=50)
    with pytest.raises(SwanBathyCoverageError):
        _author_deck(_min_build_spec(), tmp_path, dem)


def test_author_deck_succeeds_with_covering_dem(tmp_path: Path):
    """Sanity: a DEM that covers the AOI authors a deck with real depths."""
    dem = tmp_path / "bathy_ok.tif"
    _write_utm_dem(dem, crs="EPSG:32616", fill=-5.0)
    manifest = _author_deck(_min_build_spec(), tmp_path, dem)
    assert manifest is not None
    bottom = (tmp_path / "bottom.bot").read_text()
    vals = [float(t) for line in bottom.splitlines() for t in line.split()]
    # Real seabed (-5 elev -> +5 depth), NOT the uniform 10.0 flat demo.
    assert vals, "bottom grid must be written"
    assert max(vals) == pytest.approx(5.0, abs=0.5)
    assert not all(v == 10.0 for v in vals)
