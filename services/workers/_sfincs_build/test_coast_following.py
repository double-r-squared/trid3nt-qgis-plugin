"""ADR 0178: coast-following quadtree refinement + the sfincs_map.nc CRS stamp.

Offline unit coverage (no cht_sfincs / no docker) for the two worker-side legs:

- ``_coast_refinement_geom`` extracts the z=0 shoreline from a synthetic
  topobathy raster and buffers it into a coast-following band; returns None when
  the AOI has no land-sea interface (entirely wet or entirely dry) so the deck
  builder degrades LOUDLY to the center band.
- the entrypoint CRS stamp writes the true EPSG into a sfincs_map.nc ``crs`` var
  so the downstream reader no longer falls back to EPSG:3857.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
for p in (REPO / "server/src", REPO / "contracts/src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.workers._sfincs_build.deck_quadtree import _coast_refinement_geom  # noqa: E402


def _write_dem(path: Path, z: np.ndarray, epsg: int, x0: float, y1: float, res: float):
    import rasterio
    from rasterio.transform import from_origin

    transform = from_origin(x0, y1, res, res)  # y1 = top (north)
    with rasterio.open(
        path, "w", driver="GTiff", height=z.shape[0], width=z.shape[1],
        count=1, dtype="float32", crs=f"EPSG:{epsg}", transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(z.astype("float32"), 1)


def test_coast_following_geom_from_diagonal_shoreline(tmp_path):
    # UTM 16N synthetic tile: land (+) west, sea (-) east, diagonal shoreline.
    epsg = 32616
    x0, y1, res = 640000.0, 3303000.0, 100.0
    n = 160
    xx, yy = np.meshgrid(np.arange(n), np.arange(n))
    # elevation crosses zero along a diagonal -> a real land-sea interface
    z = (xx - yy).astype("float64") * 0.5  # negative sea (upper-left) -> positive land
    dem = tmp_path / "topobathy.tif"
    _write_dem(dem, z, epsg, x0, y1, res)
    xhi = x0 + n * res
    ylo = y1 - n * res
    geom = _coast_refinement_geom(str(dem), epsg, (x0, ylo, xhi, y1), coast_band_m=500.0)
    assert geom is not None
    assert geom.geom_type in ("Polygon", "MultiPolygon")
    # A coast-following band is a thin strip, not the whole domain.
    domain_area = (xhi - x0) * (y1 - ylo)
    assert 0.0 < geom.area < 0.9 * domain_area


def test_coast_following_geom_none_when_all_wet(tmp_path):
    epsg = 32616
    x0, y1, res = 640000.0, 3303000.0, 100.0
    n = 80
    z = np.full((n, n), -5.0)  # entirely below sea level -> no interface
    dem = tmp_path / "allwet.tif"
    _write_dem(dem, z, epsg, x0, y1, res)
    geom = _coast_refinement_geom(
        str(dem), epsg, (x0, y1 - n * res, x0 + n * res, y1), coast_band_m=500.0
    )
    assert geom is None


def test_coast_following_geom_none_when_all_dry(tmp_path):
    epsg = 32616
    x0, y1, res = 640000.0, 3303000.0, 100.0
    n = 80
    z = np.full((n, n), 12.0)  # entirely land
    dem = tmp_path / "alldry.tif"
    _write_dem(dem, z, epsg, x0, y1, res)
    geom = _coast_refinement_geom(
        str(dem), epsg, (x0, y1 - n * res, x0 + n * res, y1), coast_band_m=500.0
    )
    assert geom is None


def test_crs_stamp_roundtrip(tmp_path):
    import netCDF4

    from services.workers.sfincs.entrypoint import _stamp_sfincs_map_crs

    nc = tmp_path / "sfincs_map.nc"
    ds = netCDF4.Dataset(str(nc), "w")
    ds.createDimension("np", 3)
    crs = ds.createVariable("crs", "i4")
    crs.setncattr("EPSG", "-")  # the SFINCS placeholder
    ds.close()

    _stamp_sfincs_map_crs(nc, 32616)

    from trid3nt_server.agent.workflows.shared.cog_io import _read_crs_from_dataset
    import xarray as xr

    dsr = xr.open_dataset(str(nc))
    try:
        assert _read_crs_from_dataset(dsr) == "EPSG:32616"
    finally:
        dsr.close()


def test_crs_stamp_no_file_is_noop(tmp_path):
    from services.workers.sfincs.entrypoint import _stamp_sfincs_map_crs

    # missing file -> silent no-op, never raises
    _stamp_sfincs_map_crs(tmp_path / "does_not_exist.nc", 32616)
