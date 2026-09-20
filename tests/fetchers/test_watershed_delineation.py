"""The D8 delineation on fetch_watershed's ingestion, with no network.

The DEM is a SYNTHETIC south-draining V-valley handed straight to the trace, so
the full chain runs for real. The catchment holds the snapped pour point and the
upstream axis and excludes a downstream one; a projected grid still answers in
lon/lat; the basin that ran out of window says so; the trace is alignment-
invariant, and bad inputs refuse through the router's own error family."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import Point, shape

from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_watershed.hooks import _basin

# Synthetic geographic grid: 60x60 cells of 0.001 deg.
N = 60
RES = 0.001
WEST, SOUTH = -117.05, 34.00
EAST, NORTH = WEST + N * RES, SOUTH + N * RES

CENTER_COL = 30


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_watershed"]


def _lonlat(col: float, row: float) -> tuple[float, float]:
    """(col, row) cell indices -> cell-center lon/lat (row 0 = NORTH edge)."""
    return (WEST + (col + 0.5) * RES, NORTH - (row + 0.5) * RES)


def _valley(rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """South-draining V-valley: 1 m/row down-valley, 2 m/col cross slope."""
    return 500.0 - rows[:, None] * 1.0 + np.abs(cols[None, :] - CENTER_COL) * 2.0


def _write(path: str, z: np.ndarray, transform, crs: str) -> str:
    with rasterio.open(path, "w", driver="GTiff", height=z.shape[0],
                       width=z.shape[1], count=1, dtype="float32", crs=crs,
                       transform=transform, nodata=-9999.0) as dst:
        dst.write(z.astype("float32"), 1)
    return path


@pytest.fixture()
def valley_dem(tmp_path) -> str:
    z = _valley(np.arange(N, dtype=np.float64), np.arange(N, dtype=np.float64))
    return _write(str(tmp_path / "valley.tif"), z,
                  from_bounds(WEST, SOUTH, EAST, NORTH, N, N), "EPSG:4326")


@pytest.fixture()
def valley_dem_5070(tmp_path) -> str:
    """The same valley on a PROJECTED grid (Albers metres) - what 3DEP hands over.

    The row published is EPSG:4326, so the trace has to go into this grid and the
    catchment has to come back out of it."""
    from pyproj import Transformer

    into = Transformer.from_crs(4326, 5070, always_xy=True).transform
    west_m, south_m = into(WEST, SOUTH)
    east_m, north_m = into(EAST, NORTH)
    z = _valley(np.arange(N, dtype=np.float64), np.arange(N, dtype=np.float64))
    return _write(str(tmp_path / "valley_5070.tif"), z,
                  from_bounds(west_m, south_m, east_m, north_m, N, N), "EPSG:5070")


def _bowl_dem(path: str, origin_lon: float, top_lat: float, dx: float,
              n: int) -> tuple:
    """A convergent paraboloid bowl (lowest at the centre) at a given origin.

    Every cell drains inward, so the centre outlet captures the whole convergent
    interior - a large, non-degenerate basin. Returns its pour point."""
    from rasterio.transform import from_origin

    rows, cols = np.mgrid[0:n, 0:n]
    oi = oj = n // 2
    z = ((rows - oi) ** 2 + (cols - oj) ** 2 + 1.0).astype("float32")
    _write(path, z, from_origin(origin_lon, top_lat, dx, dx), "EPSG:4326")
    return (origin_lon + oj * dx, top_lat - oi * dx)


def test_the_catchment_holds_the_pour_point_and_the_valley_above_it(
        spec, valley_dem, tmp_path):
    pour = _lonlat(CENTER_COL, 54)
    geometry, measures, notes = _basin(spec, pour, valley_dem, str(tmp_path))

    assert measures["cell_count"] > 0 and measures["area_km2"] > 0.0
    assert measures["pour_point_lon"] == pour[0]
    assert any("pysheds" in note for note in notes)

    polygon = shape(geometry)
    snapped = Point(measures["snapped_lon"], measures["snapped_lat"])
    assert polygon.buffer(RES).contains(snapped)
    # The whole centre line drains here.
    for row in (10, 25, 40):
        assert polygon.buffer(RES).contains(Point(*_lonlat(CENTER_COL, row))), (
            f"upstream valley cell at row {row} not inside the watershed")
    assert measures["cell_count"] <= N * N


def test_cells_downstream_of_the_outlet_cannot_drain_to_it(
        spec, valley_dem, tmp_path):
    geometry, _measures, _notes = _basin(
        spec, _lonlat(CENTER_COL, 30), valley_dem, str(tmp_path))
    assert not shape(geometry).contains(Point(*_lonlat(CENTER_COL, 50)))


def test_a_basin_that_reaches_the_window_edge_says_so(spec, valley_dem, tmp_path):
    """The valley runs off the north edge, so its area is a LOWER BOUND - the
    measure the fetcher refuses on rather than publishing a cut as a divide."""
    _geometry, measures, _notes = _basin(
        spec, _lonlat(CENTER_COL, 54), valley_dem, str(tmp_path))
    assert measures["truncated"] is True


def test_a_projected_dem_still_answers_in_lon_lat(spec, valley_dem_5070, tmp_path):
    """The 5070 leak: metres on a row that says degrees is a domain nothing
    downstream can mesh - it reads as a lattice millions of cells wide."""
    pour = _lonlat(CENTER_COL, 54)
    geometry, measures, notes = _basin(spec, pour, valley_dem_5070, str(tmp_path))

    assert measures["cell_count"] > 0
    assert any("EPSG:5070" in note for note in notes)
    west, south, east, north = shape(geometry).bounds
    assert -180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0
    # The catchment stands where the pour point does, not a continent away. The
    # slack is the projected grid's own edge: a rectangle in Albers metres is not
    # a rectangle in degrees, so it overhangs the lon/lat window slightly.
    assert WEST - 0.02 <= west and east <= EAST + 0.02
    assert SOUTH - 0.02 <= south and north <= NORTH + 0.02
    lon_snap, lat_snap = measures["snapped_lon"], measures["snapped_lat"]
    assert abs(lon_snap - pour[0]) < 0.01 and abs(lat_snap - pour[1]) < 0.01
    assert shape(geometry).buffer(RES).contains(Point(lon_snap, lat_snap))


def test_a_wide_extent_over_few_cells_is_not_a_clamp(spec, tmp_path):
    """A full degree of longitude over ten cells costs ten cells of D8."""
    dem = _write(str(tmp_path / "wide.tif"), np.zeros((10, 10)),
                 from_bounds(-118.0, 34.0, -117.0, 34.05, 10, 10), "EPSG:4326")
    _geometry, measures, _notes = _basin(spec, (-117.5, 34.02), dem, str(tmp_path))
    assert measures["cell_count"] > 0


def test_a_dem_past_the_cell_clamp_refuses(spec, tmp_path):
    """The clamp is read off the raster's declared shape, so the header alone
    settles it and nothing here allocates sixteen million cells."""
    from trid3nt_server.tools.derive import _hydrology_common

    side = int(_hydrology_common._MAX_DEM_CELLS ** 0.5) + 1
    path = str(tmp_path / "dense.tif")
    with rasterio.open(path, "w", driver="GTiff", height=side, width=side,
                       count=1, dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_bounds(-117.6, 34.0, -117.5, 34.1, side, side),
                       tiled=True, sparse_ok=True):
        pass
    with pytest.raises(RouterInputError):
        _basin(spec, (-117.55, 34.05), path, str(tmp_path))


def test_the_trace_runs_in_index_space_not_coordinate_space(spec, tmp_path):
    """Rounding an outlet coordinate to a neighbour cell collapses a convergent
    basin to a sliver, where the index-space trace captures the whole interior."""
    from trid3nt_server.tools.derive._hydrology_common import _condition_dem

    dem = str(tmp_path / "bowl.tif")
    pour = _bowl_dem(dem, -83.50, 35.10, 0.001, 40)

    grid, fdir, _acc = _condition_dem(dem)
    coord_cells = int(np.asarray(
        grid.catchment(x=pour[0], y=pour[1], fdir=fdir, xytype="coordinate",
                       nodata_out=np.bool_(False)), dtype=bool).sum())

    _geometry, measures, _notes = _basin(spec, pour, dem, str(tmp_path))

    assert coord_cells < 30, (
        f"expected the coordinate path to sliver; got {coord_cells} cells")
    assert measures["cell_count"] >= 100, (
        f"index-space delineation collapsed ({measures['cell_count']} cells)")
    assert measures["cell_count"] > 5 * coord_cells


def test_the_basin_size_does_not_swing_with_sub_cell_shifts(spec, tmp_path):
    """The property the coordinate path violated: 1-14 vs 33.7-34.0k cells across
    box quantizations for the same outlet."""
    counts: list[int] = []
    for i, shift in enumerate((0.0, 0.0003, 0.0007, 0.00013, 0.0005)):
        dem = str(tmp_path / f"bowl_{i}.tif")
        pour = _bowl_dem(dem, -83.50 + shift, 35.10 + shift, 0.001, 40)
        _geometry, measures, _notes = _basin(spec, pour, dem, str(tmp_path))
        counts.append(measures["cell_count"])

    assert min(counts) >= 100, f"a shift collapsed the basin: {counts}"
    assert max(counts) - min(counts) <= 5, f"basin size swung with alignment: {counts}"


def test_a_pour_point_off_the_grid_refuses(spec, valley_dem, tmp_path):
    with pytest.raises(RouterInputError):
        _basin(spec, (-116.0, 34.02), valley_dem, str(tmp_path))


def test_a_missing_dem_refuses_rather_than_fetching_one(spec, tmp_path):
    with pytest.raises(RouterInputError):
        _basin(spec, _lonlat(CENTER_COL, 50), "", str(tmp_path))
