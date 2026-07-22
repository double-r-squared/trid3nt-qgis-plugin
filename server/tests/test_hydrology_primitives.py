"""Unit tests for the watershed primitives (no network).

The DEM is a SYNTHETIC south-draining V-valley in EPSG:4326 passed via the
``dem_uri`` override, so the full pysheds D8 chain (fill_pits ->
fill_depressions -> resolve_flats -> flowdir -> accumulation -> catchment /
extract_river_network) runs for real without touching Copernicus.

Coverage:
1.  ``test_registered`` -- both tools in TOOL_REGISTRY with cacheable=False /
    ttl_class="live-no-cache".
2.  ``test_watershed_contains_pour_point`` -- the delineated polygon contains
    the (snapped) pour point AND the upstream valley axis; area/cell_count
    are consistent; the notes document the pysheds engine path.
3.  ``test_watershed_excludes_downstream_cells`` -- a valley-axis point
    DOWNSTREAM (south) of a mid-valley pour point is NOT inside the upstream
    catchment.
4.  ``test_streams_follow_valley`` -- at the default 500-cell threshold the
    network is the main stem: every extracted stream vertex lies on the
    valley center line (+- 1 cell).
5.  ``test_no_streams_raises`` -- an impossible threshold raises the typed
    ``NoStreamsError``.
6.  ``test_auto_bbox_is_0p1_deg`` -- the default bbox is the 0.1-degree box
    centered on the pour point.
7.  ``test_aoi_clamp_raises`` / ``test_bad_inputs_raise`` -- typed input
    validation for both tools.
"""

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
from trid3nt_server.tools.processing._hydrology_common import HydrologyAoiTooLargeError, HydrologyInputError
from trid3nt_server.tools.processing.delineate_watershed import WatershedLayerURI, _auto_bbox, delineate_watershed
from trid3nt_server.tools.processing.extract_stream_network import NoStreamsError, StreamNetworkLayerURI, extract_stream_network

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
        bbox=BBOX,
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
        bbox=BBOX,
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
        bbox=BBOX,
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
            bbox=BBOX,
            accumulation_threshold=10 * N * N,  # impossible: > total cells
            dem_uri=valley_dem,
            _output_dir=str(tmp_path),
        )


def test_auto_bbox_is_0p1_deg() -> None:
    lon, lat = -117.0, 34.0
    west, south, east, north = _auto_bbox(lon, lat)
    assert east - west == pytest.approx(0.1)
    assert north - south == pytest.approx(0.1)
    assert west + 0.05 == pytest.approx(lon)
    assert south + 0.05 == pytest.approx(lat)


def test_aoi_clamp_raises() -> None:
    with pytest.raises(HydrologyAoiTooLargeError):
        extract_stream_network(bbox=(-118.0, 34.0, -117.0, 34.05))  # 1.0 wide
    with pytest.raises(HydrologyAoiTooLargeError):
        delineate_watershed(
            pour_point=(-117.5, 34.0),
            bbox=(-118.0, 33.9, -117.0, 34.1),
        )


def test_bad_inputs_raise(valley_dem, tmp_path) -> None:
    # Bad pour point shapes / values.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-117.0,))  # wrong arity
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=("a", 34.0))  # non-numeric
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-500.0, 34.0))  # out of range
    # Pour point outside the supplied bbox.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(pour_point=(-116.0, 34.02), bbox=BBOX)
    # Bad snap threshold.
    with pytest.raises(HydrologyInputError):
        delineate_watershed(
            pour_point=_lonlat(CENTER_COL, 50), bbox=BBOX, snap_threshold=0
        )
    # Bad bbox shapes.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(bbox=(-117.0, 34.05, -117.05, 34.00))  # reversed
    with pytest.raises(HydrologyInputError):
        extract_stream_network(bbox=(-117.0, 34.0, -116.99))  # wrong arity
    # Bad accumulation threshold.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(bbox=BBOX, accumulation_threshold="lots")
    with pytest.raises(HydrologyInputError):
        extract_stream_network(bbox=BBOX, accumulation_threshold=1)
    # Missing local DEM path.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(
            bbox=BBOX,
            dem_uri=str(tmp_path / "missing.tif"),
            _output_dir=str(tmp_path),
        )
