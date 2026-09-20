"""Unit tests for the stream-network primitive, with no network.

The DEM is a SYNTHETIC south-draining V-valley passed through the override uri,
so the full D8 chain runs for real. The network follows the valley centre line;
the threshold, the cell clamp and bad inputs refuse typed, and a missing DEM is
a refusal rather than a fetch."""

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
    entry = TOOL_REGISTRY["extract_stream_network"]
    assert entry.fn is extract_stream_network
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"




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



def test_cell_clamp_raises(dense_dem) -> None:
    with pytest.raises(HydrologyDemTooLargeError):
        extract_stream_network(dem_uri=dense_dem)



def test_bad_inputs_raise(valley_dem, tmp_path) -> None:
    # No DEM: the tool never fetches one.
    with pytest.raises(HydrologyInputError):
        extract_stream_network(dem_uri="")
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
