"""A layer of soundings handed the bed, offline.

Covered: points laid on the grid by cell mean and filled by the bed's own fill, depths flipped onto the run's
frame through the layer's published shift, soundings past FILL_MAX_DISTANCE leaving a wet hole that refuses,
an ordered list where the points win the cells they measured, and the run note naming the soundings."""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.inputs.bed import RASTER, bed
from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.settings import bind_run, reset_run
from trid3nt_server.tools.derive._raster_layers import FILLED

_LON, _LAT, _STEP = -122.67, 45.52, 0.0001


@pytest.fixture(autouse=True)
def _written_locally(monkeypatch, tmp_path):
    from trid3nt_server.tools.derive import _hydrology_common as common

    real = common.write_cog
    monkeypatch.setattr(common, "write_cog", lambda band, **kw: real(
        band, **{**kw, "output_dir": kw.get("output_dir") or str(tmp_path)}))


def _points(tmp_path, value, *, rows=10, cols=10, skip=(), quantity="bed_elevation_m",
            datum="NAVD88", **layer):
    """A lattice of soundings about 8 m by 11 m apart, ``value(i, j)`` each, minus the ``skip`` cells."""
    features = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [_LON + j * _STEP, _LAT + i * _STEP]},
         "properties": {quantity: value(i, j), "vertical_datum": datum}}
        for i in range(rows) for j in range(cols) if (i, j) not in skip]
    path = tmp_path / "soundings.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return LayerURI(layer_id="soundings", name="channel soundings", layer_type="vector", uri=str(path),
                    quantity=quantity.removesuffix("_m") if quantity.startswith("depth") else quantity,
                    vertical_datum=datum, **layer)


def _water(west=-122.6705, east=-122.6685, south=45.5195, north=45.5215):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north],
                                                [west, south]]]}


def _bands(layer):
    with rasterio.open(layer.uri) as src:
        return src.read(1), src.read(2).astype("uint8")


def test_a_points_layer_becomes_a_filled_raster_bed(tmp_path):
    holed = {(i, j) for i in range(4, 6) for j in range(4, 6)}
    layer = _points(tmp_path, lambda i, j: 10.0 + j, skip=holed)
    token = bind_notes()
    try:
        laid = bed(layer, frame="NAVD88", water=_water())
    finally:
        said = drain_notes(token)
    assert laid.kind == RASTER
    values, won = _bands(laid.source)
    assert (won == FILLED).any() and np.isfinite(values[won == FILLED]).all()
    assert np.nanmin(values) >= 10.0 and np.nanmax(values) <= 19.0
    assert any("100 soundings" in line or "96 soundings" in line for line in said), said
    assert any("channel soundings" in line and "inverse_distance" in line for line in said)


def test_soundings_of_depth_are_flipped_like_a_depth_raster(tmp_path):
    layer = _points(tmp_path, lambda i, j: 5.0, quantity="depth_below_datum_m", datum="CRD",
                    datum_offset_m=1.6093, datum_offset_frame="NAVD88")
    laid = bed(layer, frame="NAVD88", water=_water())
    values, won = _bands(laid.source)
    measured = values[won == 0]
    assert measured.size and np.allclose(measured, 1.6093 - 5.0, atol=1e-4)
    assert laid.source.vertical_datum == "NAVD88"


def test_soundings_past_the_reach_leave_a_wet_hole(tmp_path):
    layer = _points(tmp_path, lambda i, j: 3.0, rows=2, cols=2)
    token = bind_run({"FILL_MAX_DISTANCE": 1})
    try:
        with pytest.raises(UserInputError) as refused:
            bed(layer, frame="NAVD88", water=_water())
    finally:
        reset_run(token)
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "FILL_MAX_DISTANCE" in str(refused.value)


def test_points_listed_before_a_raster_win_where_they_measured(tmp_path):
    layer = _points(tmp_path, lambda i, j: 3.0, rows=4, cols=4)
    terrain = np.tile(np.linspace(0.0, 20.0, 60, dtype="float32"), (60, 1))
    handle, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_terrain_", dir=tmp_path)
    os.close(handle)
    with rasterio.open(path, "w", driver="GTiff", height=60, width=60, count=1, dtype="float32",
                       crs="EPSG:4326", nodata=float("nan"),
                       transform=from_origin(_LON - 0.001, _LAT + 0.003, 0.0001, 0.0001)) as out:
        out.write(terrain, 1)
    raster = LayerURI(layer_id="terrain", name="terrain", layer_type="raster", uri=path,
                      vertical_datum="NAVD88")
    laid = bed([layer, raster], frame="NAVD88", water=_water(-122.671, -122.667, 45.519, 45.523))
    values, won = _bands(laid.source)
    assert (won == 0).any() and np.allclose(values[won == 0], 3.0)
    assert (won == 1).any() and values[won == 1].max() > 15.0
