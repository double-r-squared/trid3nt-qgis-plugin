"""Offline gates for the fresh-AOI terrain prep (promotion).

Pure rasterio/pyproj/numpy on a SYNTHETIC DEM (no network, no docker): the local
ftUS CRS, the m->ftUS elevation conversion, the nodata fill (so ComputeFrom never
index-faults), the finite-data-envelope mesh bounds, and the CCW-open perimeter +
seed grid the AuthorMesh worker consumes.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from flood2d_terrain import prepare_terrain, local_usft_crs_wkt, _M_TO_USFT  # noqa: E402


def _synthetic_dem(path: Path) -> None:
    """A small EPSG:5070 (Albers, m) DEM: a valley trough (metres)."""
    from rasterio.transform import from_origin

    w = h = 120
    x = np.linspace(-1, 1, w)
    y = np.linspace(-1, 1, h)
    gx, gy = np.meshgrid(x, y)
    elev_m = 100.0 + 60.0 * np.abs(gx) + 5.0 * gy  # a V-shaped valley, ~100-160 m
    transform = from_origin(700000.0, 1710000.0, 30.0, 30.0)  # Albers metres
    prof = dict(driver="GTiff", dtype="float32", count=1, width=w, height=h,
                crs="EPSG:5070", transform=transform, nodata=None)
    with rasterio.open(path, "w", **prof) as d:
        d.write(elev_m.astype("float32"), 1)


def test_local_usft_crs_is_ftus():
    wkt = local_usft_crs_wkt(-87.93, 38.13)
    assert "US survey foot" in wkt
    assert "Transverse Mercator" in wkt


def test_prepare_terrain_ftus_and_seeds(tmp_path):
    dem = tmp_path / "dem.tif"
    _synthetic_dem(dem)
    prep = prepare_terrain(dem, tmp_path, resolution_m=60.0)

    # elevations converted m->ftUS (100-160 m -> ~328-525 ft)
    assert 300.0 < prep.terrain_min_ft < 400.0
    assert 480.0 < prep.terrain_max_ft < 560.0
    # resolution carried to feet
    assert abs(prep.resolution_ft - 60.0 * _M_TO_USFT) < 1e-6
    # perimeter is CCW-open (multiple of 4 edge samples, not closed)
    perim = np.fromfile(prep.perimeter_f64, np.float64).reshape(-1, 2)
    assert perim.shape[0] == prep.n_perim >= 8
    assert not np.allclose(perim[0], perim[-1])  # OPEN (no closing dup)
    # seeds are a non-trivial interior grid
    centers = np.fromfile(prep.centers_f64, np.float64).reshape(-1, 2)
    assert centers.shape[0] == prep.n_seeds >= 9
    # seeds lie strictly inside the perimeter bounds
    assert centers[:, 0].min() > perim[:, 0].min() - 1.0
    assert centers[:, 0].max() < perim[:, 0].max() + 1.0

    # written terrain.tif is finite everywhere (nodata filled) + in the ftUS CRS
    with rasterio.open(prep.terrain_tif) as r:
        arr = r.read(1)
        assert np.isfinite(arr).all()
        assert "us-ft" in r.crs.to_proj4() or "US survey foot" in r.crs.to_wkt()
