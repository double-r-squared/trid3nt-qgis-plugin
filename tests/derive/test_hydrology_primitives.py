"""Unit tests for the watershed primitives, with no network.

The DEM is a SYNTHETIC south-draining V-valley passed through the override uri,
so the full D8 chain runs for real. The delineated polygon holds the snapped pour
point and the upstream axis and excludes a downstream one; the network follows
the valley centre line; the threshold, the extent clamp and bad inputs refuse
typed, and a missing DEM is a refusal rather than a fetch."""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive import _hydrology_common
from trid3nt_server.tools.derive._hydrology_common import HydrologyDemTooLargeError, HydrologyInputError
from trid3nt_server.tools.derive.delineate_watershed.delineate_watershed import WatershedLayerURI, delineate_watershed
from trid3nt_server.tools.derive.extract_stream_network.extract_stream_network import NoStreamsError, StreamNetworkLayerURI, extract_stream_network

# Synthetic geographic grid: 60x60 cells of 0.001 deg.
N = 60
RES = 0.001
WEST, SOUTH = -117.05, 34.00
EAST, NORTH = WEST + N * RES, SOUTH + N * RES
BBOX = (WEST, SOUTH, EAST, NORTH)

CENTER_COL = 30
#: lon of the valley center-line column.
CENTER_LON = WEST + (CENTER_COL + 0.5) * RES


def _lonlat(col: float, row: float) -> tuple[float, float]:
    """(col, row) cell indices -> cell-center lon/lat (row 0 = NORTH edge)."""
    return (WEST + (col + 0.5) * RES, NORTH - (row + 0.5) * RES)


@pytest.fixture()
def valley_dem(tmp_path) -> str:
    """South-draining V-valley: down-valley drop 1 m/row + cross slope 2 m/col
    funneling flow to the center column."""
    rows = np.arange(N, dtype=np.float64)
    cols = np.arange(N, dtype=np.float64)
    z = 500.0 - rows[:, None] * 1.0 + np.abs(cols[None, :] - CENTER_COL) * 2.0
    transform = from_bounds(WEST, SOUTH, EAST, NORTH, N, N)
    path = str(tmp_path / "valley.tif")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=N,
        width=N,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(z.astype("float32"), 1)
    return path


def test_registered() -> None:
    for name, fn in (
        ("delineate_watershed", delineate_watershed),
        ("extract_stream_network", extract_stream_network),
    ):
        entry = TOOL_REGISTRY[name]
        assert entry.fn is fn
        assert entry.metadata.cacheable is False
        assert entry.metadata.ttl_class == "live-no-cache"


def test_watershed_contains_pour_point(valley_dem, tmp_path) -> None:
    out_dir = tmp_path / "out_ws"
    out_dir.mkdir()
    # Pour point on the valley axis near the south (downstream) edge.
    pour = _lonlat(CENTER_COL, 54)

    result = delineate_watershed(
        pour_point=pour,
        dem_uri=valley_dem,
        _output_dir=str(out_dir),
    )

    # Typed LayerURI subclass (persists to the case record at the wrap-site).
    assert isinstance(result, WatershedLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.cell_count > 0
    assert result.area_km2 > 0.0
    assert result.pour_point == pour
    assert result.snapped_pour_point is not None
    assert any("pysheds" in note for note in result.notes)

    with open(result.uri) as f:
        fc = json.load(f)
    polygon = unary_union([shape(feat["geometry"]) for feat in fc["features"]])

    # Contains the snapped pour point...
    assert polygon.buffer(RES).contains(Point(*result.snapped_pour_point))
    # ...and the upstream valley axis (the whole center line drains here).
    for row in (10, 25, 40):
        assert polygon.buffer(RES).contains(Point(*_lonlat(CENTER_COL, row))), (
            f"upstream valley cell at row {row} not inside the watershed"
        )
    # Area bookkeeping is consistent (cells x ~cell-area).
    assert result.cell_count <= N * N


def test_watershed_excludes_downstream_cells(valley_dem, tmp_path) -> None:
    """Cells DOWNSTREAM (south) of the pour point cannot drain to it."""
    out_dir = tmp_path / "out_ws2"
    out_dir.mkdir()
    pour = _lonlat(CENTER_COL, 30)  # mid-valley outlet

    result = delineate_watershed(
        pour_point=pour,
        dem_uri=valley_dem,
        _output_dir=str(out_dir),
    )
    with open(result.uri) as f:
        fc = json.load(f)
    polygon = unary_union([shape(feat["geometry"]) for feat in fc["features"]])
    # A valley-axis point well SOUTH (downstream) of the outlet is not in the
    # upstream catchment.
    downstream = Point(*_lonlat(CENTER_COL, 50))
    assert not polygon.contains(downstream)


def test_streams_follow_valley(valley_dem, tmp_path) -> None:
    out_dir = tmp_path / "out_str"
    out_dir.mkdir()

    # The default 500-cell threshold isolates the MAIN STEM: only the valley
    # center column accumulates >= 500 cells (verified: max accumulation off
    # the center column is ~435 on this DEM), so every extracted vertex must
    # sit on the valley axis. Lower thresholds legitimately add wall
    # tributaries (real D8 convergence), which is not what this test pins.
    result = extract_stream_network(
        accumulation_threshold=500,
        dem_uri=valley_dem,
        _output_dir=str(out_dir),
    )

    assert isinstance(result, StreamNetworkLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.segment_count >= 1
    assert result.accumulation_threshold == 500
    assert result.total_length_km > 0.0
    assert any("pysheds" in note for note in result.notes)

    with open(result.uri) as f:
        fc = json.load(f)
    feats = fc["features"]
    assert len(feats) == result.segment_count

    # Every stream vertex hugs the valley center line (the V-valley funnels
    # all flow to the center column; allow +-1 cell for D8 discretization).
    for feat in feats:
        assert feat["geometry"]["type"] == "LineString"
        for lon, _lat in feat["geometry"]["coordinates"]:
            assert abs(lon - CENTER_LON) <= 1.0 * RES + 1e-9, (
                f"stream vertex at lon={lon} strays from the valley center "
                f"{CENTER_LON}"
            )


def test_no_streams_raises(valley_dem, tmp_path) -> None:
    with pytest.raises(NoStreamsError):
        extract_stream_network(
            accumulation_threshold=10 * N * N,  # impossible: > total cells
            dem_uri=valley_dem,
            _output_dir=str(tmp_path),
        )


@pytest.fixture()
def valley_dem_5070(tmp_path) -> str:
    """The same south-draining valley on a PROJECTED grid (Albers metres).

    What 3DEP hands over. The tool declares EPSG:4326 on its layer, so the trace
    has to go into this grid and the catchment has to come back out of it.
    """
    from pyproj import Transformer

    into = Transformer.from_crs(4326, 5070, always_xy=True).transform
    west_m, south_m = into(WEST, SOUTH)
    east_m, north_m = into(EAST, NORTH)
    rows = np.arange(N, dtype=np.float64)
    cols = np.arange(N, dtype=np.float64)
    z = 500.0 - rows[:, None] * 1.0 + np.abs(cols[None, :] - CENTER_COL) * 2.0
    path = str(tmp_path / "valley_5070.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=N, width=N, count=1, dtype="float32",
        crs="EPSG:5070",
        transform=from_bounds(west_m, south_m, east_m, north_m, N, N),
        nodata=-9999.0,
    ) as dst:
        dst.write(z.astype("float32"), 1)
    return path


def test_a_projected_dem_still_answers_in_the_4326_the_layer_declares(
        valley_dem_5070, tmp_path) -> None:
    """The 5070 leak: metres on a layer that says degrees is a domain nothing
    downstream can mesh - it reads as a lattice millions of cells wide."""
    out_dir = tmp_path / "out_5070"
    out_dir.mkdir()
    pour = _lonlat(CENTER_COL, 54)

    result = delineate_watershed(pour_point=pour,
                                 dem_uri=valley_dem_5070,
                                 _output_dir=str(out_dir))

    assert result.cell_count > 0
    assert any("EPSG:5070" in note for note in result.notes)
    west, south, east, north = result.bbox
    assert -180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0
    # The catchment stands where the pour point does, not a continent away. The
    # slack is the projected grid's own edge: a rectangle in Albers metres is not
    # a rectangle in degrees, so it overhangs the lon/lat AOI slightly.
    assert WEST - 0.02 <= west and east <= EAST + 0.02
    assert SOUTH - 0.02 <= south and north <= NORTH + 0.02
    lon_snap, lat_snap = result.snapped_pour_point
    assert abs(lon_snap - pour[0]) < 0.01 and abs(lat_snap - pour[1]) < 0.01
    with open(result.uri) as f:
        polygon = unary_union([shape(feat["geometry"])
                               for feat in json.load(f)["features"]])
    assert polygon.buffer(RES).contains(Point(lon_snap, lat_snap))


@pytest.fixture()
def wide_dem(tmp_path) -> str:
    """A ten-cell DEM spanning a full degree: EXTENT is not what the clamp reads."""
    transform = from_bounds(-118.0, 34.0, -117.0, 34.05, 10, 10)
    path = str(tmp_path / "wide.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=-9999.0,
    ) as dst:
        dst.write(np.zeros((10, 10), dtype="float32"), 1)
    return path


@pytest.fixture()
def dense_dem(tmp_path) -> str:
    """A DEM past the cell clamp, written without materializing its band.

    The clamp is read off the raster's declared shape, so the header alone
    settles it and nothing here allocates sixteen million cells."""
    side = int(_hydrology_common._MAX_DEM_CELLS ** 0.5) + 1
    transform = from_bounds(-117.6, 34.0, -117.5, 34.1, side, side)
    path = str(tmp_path / "dense.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=side, width=side, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform, nodata=-9999.0,
        tiled=True, sparse_ok=True,
    ) as dst:
        pass
    return path


def test_wide_extent_is_not_a_clamp(wide_dem, tmp_path) -> None:
    """A full degree of longitude over ten cells costs ten cells of D8."""
    result = delineate_watershed(pour_point=(-117.5, 34.02), dem_uri=wide_dem,
                                 _output_dir=str(tmp_path))
    assert isinstance(result, WatershedLayerURI) and result.cell_count > 0


def test_cell_clamp_raises(dense_dem) -> None:
    with pytest.raises(HydrologyDemTooLargeError):
        extract_stream_network(dem_uri=dense_dem)
    with pytest.raises(HydrologyDemTooLargeError):
        delineate_watershed(pour_point=(-117.55, 34.05), dem_uri=dense_dem)


def _bowl_dem(path: str, origin_lon: float, top_lat: float, dx: float, n: int) -> tuple:
    """A convergent paraboloid bowl (lowest at the centre) at a given origin.

    Every cell drains inward, so the centre outlet captures the whole convergent
    interior -- a large, non-degenerate basin. Returns (pour_point, bbox)."""
    rows, cols = np.mgrid[0:n, 0:n]
    oi = oj = n // 2
    z = ((rows - oi) ** 2 + (cols - oj) ** 2 + 1.0).astype("float32")
    from rasterio.transform import from_origin

    with rasterio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
        crs="EPSG:4326", nodata=-9999.0,
        transform=from_origin(origin_lon, top_lat, dx, dx),
    ) as ds:
        ds.write(z, 1)
    pour = (origin_lon + oj * dx, top_lat - oi * dx)
    bbox = (origin_lon, top_lat - n * dx, origin_lon + n * dx, top_lat)
    return pour, bbox


def test_index_space_beats_coordinate_path_on_convergent_dem(tmp_path) -> None:
    """Index-space delineation, not the coordinate path.

    Rounding an outlet coordinate to a neighbour cell collapses a convergent basin to
    a sliver, where the index-space delineation captures the whole interior."""
    from trid3nt_server.tools.derive._hydrology_common import _condition_dem

    dem = str(tmp_path / "bowl.tif")
    pour, _bbox = _bowl_dem(dem, -83.50, 35.10, 0.001, 40)

    # OLD path: coordinate-space catchment at the exact outlet coordinate.
    grid, fdir, _acc = _condition_dem(dem)
    catch_coord = np.asarray(
        grid.catchment(
            x=pour[0], y=pour[1], fdir=fdir, xytype="coordinate",
            nodata_out=np.bool_(False)),
        dtype=bool,
    )
    coord_cells = int(catch_coord.sum())

    # FIXED path: the tool traces in index space off the max-accumulation cell.
    out = tmp_path / "out"
    out.mkdir()
    result = delineate_watershed(
        pour_point=pour, dem_uri=dem, _output_dir=str(out))

    assert coord_cells < 30, (
        f"expected the coordinate path to sliver; got {coord_cells} cells")
    assert result.cell_count >= 100, (
        f"index-space delineation collapsed ({result.cell_count} cells)")
    assert result.cell_count > 5 * coord_cells
    assert result.area_km2 > 0.0


def test_delineation_alignment_invariant(tmp_path) -> None:
    """The delineated basin size must not swing with sub-cell shifts of the grid
    origin -- the property the coordinate path violated (1-14 vs 33.7-34.0k cells
    across box quantizations for the same outlet)."""
    counts: list[int] = []
    for i, shift in enumerate((0.0, 0.0003, 0.0007, 0.00013, 0.0005)):
        dem = str(tmp_path / f"bowl_{i}.tif")
        pour, _bbox = _bowl_dem(dem, -83.50 + shift, 35.10 + shift, 0.001, 40)
        out = tmp_path / f"out_{i}"
        out.mkdir()
        result = delineate_watershed(
            pour_point=pour, dem_uri=dem, _output_dir=str(out))
        counts.append(result.cell_count)

    assert min(counts) >= 100, f"a shift collapsed the basin: {counts}"
    # alignment-invariant: the spread across sub-cell shifts is tight.
    assert max(counts) - min(counts) <= 5, f"basin size swung with alignment: {counts}"


def test_bad_inputs_raise(valley_dem, tmp_path) -> None:
    # Bad pour point shapes / values.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-117.0,), dem_uri=valley_dem)  # wrong arity
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=("a", 34.0), dem_uri=valley_dem)  # non-numeric
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-500.0, 34.0), dem_uri=valley_dem)  # out of range
    # Pour point outside the DEM.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-116.0, 34.02), dem_uri=valley_dem)
    # Bad snap threshold.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(
            pour_point=_lonlat(CENTER_COL, 50), dem_uri=valley_dem, snap_threshold=0
        )
    # No DEM: the tool never fetches one.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(dem_uri="")
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=_lonlat(CENTER_COL, 50), dem_uri=None)
    # Bad accumulation threshold.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(dem_uri=valley_dem, accumulation_threshold="lots")
    with pytest.raises(HydrologyInputError):
        extract_stream_network(dem_uri=valley_dem, accumulation_threshold=1)
    # Missing local DEM path.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(
            dem_uri=str(tmp_path / "missing.tif"),
            _output_dir=str(tmp_path),
        )
