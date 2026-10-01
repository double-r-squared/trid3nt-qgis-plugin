"""The two raster derives: merge_rasters and fill_nodata, offline.

Covered: the first input winning every cell it measured and the next painting
only what it left, the order of the list being the priority, the second band
naming which input each cell came from, the coverage feedback per input, each
input read onto the LAST input's frame through the shift it publishes or the
offset stated for it, two surfaces with no common ground refusing, a fill
reaching inside 'within' alone with its edge at the stated seed and every cell it
painted marked FILLED, a fill with no seed refusing, a bed whose water nothing
measured refused naming both derives, and ops= gone from every signature."""

from __future__ import annotations

import asyncio
import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools.derive._raster_layers import (
    FILLED, UNMEASURED, RasterLayerError)
from trid3nt_server.tools.derive.fill_nodata.fill_nodata import (
    fill_nodata, filled)
from trid3nt_server.tools.derive.merge_rasters.merge_rasters import merged

_NAN = float("nan")


def _write(values: np.ndarray, *, west: float, north: float, cell: float,
           crs: str = "EPSG:32610") -> str:
    handle, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_steps_")
    os.close(handle)
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0],
                       width=values.shape[1], count=1, dtype="float32", crs=crs,
                       nodata=_NAN, transform=from_origin(west, north, cell, cell)
                       ) as out:
        out.write(values.astype("float32"), 1)
    return path


def _layer(path: str, name: str, datum: str | None = "NAVD88", **kw) -> LayerURI:
    return LayerURI(layer_id=name, name=name, layer_type="raster", uri=path,
                    vertical_datum=datum, **kw)


def _survey(datum: str | None = "NAVD88", **kw) -> LayerURI:
    """A 1 m measurement over the middle 4 m of the terrain below."""
    return _layer(_write(np.full((4, 4), -5.0), west=500_004.0,
                         north=4_000_000.0, cell=1.0), "survey", datum, **kw)


def _terrain(datum: str | None = "NAVD88") -> LayerURI:
    """A 4 m surface over 16 m of ground, the survey inside it."""
    return _layer(_write(np.full((4, 4), 10.0), west=500_000.0,
                         north=4_000_000.0, cell=4.0), "terrain", datum)


def _bands(uri: str) -> tuple[np.ndarray, np.ndarray]:
    with rasterio.open(uri) as src:
        return src.read(1), src.read(2).astype("uint8")


def test_the_first_input_wins_where_it_measured_and_the_next_fills_the_rest(
        tmp_path):
    laid = merged([_survey(), _terrain()], "bed", [None, None],
                  _output_dir=str(tmp_path))
    values, won = _bands(laid.uri)
    assert values.shape == (16, 16), "the finest cell over the union"
    assert np.all(values[:4, 4:8] == -5.0)
    assert np.all(values[8:, :] == 10.0)
    # THE SECOND BAND names the input by its place in the list.
    assert np.all(won[:4, 4:8] == 0) and np.all(won[8:, :] == 1)
    assert laid.sources == ["survey", "terrain"]
    assert dict(laid.coverage) == {"survey": 0.0625, "terrain": 0.9375}
    assert laid.unmeasured_fraction == 0.0
    assert any("survey 6.2%" in note and "terrain 93.8%" in note
               for note in laid.notes)


def test_the_order_of_the_list_is_the_priority(tmp_path):
    laid = merged([_terrain(), _survey()], "bed", [None, None],
                  _output_dir=str(tmp_path))
    values, won = _bands(laid.uri)
    assert np.all(values == 10.0) and np.all(won == 0)
    assert dict(laid.coverage)["survey"] == 0.0


def test_a_survey_of_depths_is_read_onto_the_last_input_s_frame_through_its_own_shift(
        tmp_path):
    """5 m of depth below a project datum that sits 1.6093 m above NAVD88 is an
    elevation of -3.3907 m on NAVD88, the frame the terrain counts from."""
    from trid3nt_server.inputs.bed import SurveySurfaceLayerURI

    survey = SurveySurfaceLayerURI(
        layer_id="s", name="soundings", layer_type="raster",
        uri=_write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0,
                   cell=1.0),
        quantity="depth_below_datum_m", vertical_datum="CRD",
        datum_offset_m=1.6093, datum_offset_frame="NAVD88")
    laid = merged([survey, _terrain()], "bed", [None, None],
                  _output_dir=str(tmp_path))
    values, _won = _bands(laid.uri)
    assert values[0, 4] == pytest.approx(-3.3907, abs=1e-4)
    assert laid.vertical_datum == "NAVD88"
    assert any("DEPTHS below CRD" in note for note in laid.notes)


def test_a_stated_offset_moves_an_input_onto_the_frame(tmp_path):
    laid = merged([_survey("EGM2008"), _terrain()], "bed",
                  [{"offset_m": 0.5, "from_frame": "EGM2008",
                    "to_frame": "NAVD88", "source": "NOAA VDatum"}, None],
                  _output_dir=str(tmp_path))
    values, _won = _bands(laid.uri)
    assert values[0, 4] == pytest.approx(-4.5)


def test_surfaces_with_no_common_ground_refuse(tmp_path):
    far = _layer(_write(np.full((4, 4), _NAN), west=500_100.0,
                        north=4_000_000.0, cell=1.0), "far")
    with pytest.raises(RasterLayerError) as refused:
        merged([far], "bed", [None], _output_dir=str(tmp_path))
    assert refused.value.error_code == "MERGE_RASTERS_DISJOINT"


def _holed() -> LayerURI:
    """A lon/lat surface with a hole in its west half and one in its east."""
    values = np.full((10, 10), -6.0, dtype="float32")
    values[4:6, 1:4] = _NAN
    values[4:6, 6:9] = _NAN
    return _layer(_write(values, west=-82.5, north=43.0, cell=0.001,
                         crs="EPSG:4326"), "bed")


#: The west half of the holed surface.
_WEST = {"type": "Polygon", "coordinates": [[
    [-82.5, 42.99], [-82.495, 42.99], [-82.495, 43.0], [-82.5, 43.0],
    [-82.5, 42.99]]]}


def test_the_fill_reaches_inside_within_alone_and_marks_what_it_painted(
        tmp_path):
    out = filled(_holed(), _WEST, 176.041, _output_dir=str(tmp_path))
    values, won = _bands(out.uri)
    assert np.all(np.isfinite(values[4:6, 1:4])), "the west hole is filled"
    assert np.all(won[4:6, 1:4] == FILLED)
    assert np.all(np.isnan(values[4:6, 6:9])), "nothing outside within moves"
    assert np.all(won[4:6, 6:9] == UNMEASURED)
    assert np.all(won[:4, :] == 0)
    assert out.sources == ["bed", "filled"]
    assert dict(out.coverage)["filled"] == pytest.approx(0.12)


def test_the_edge_of_within_is_seeded_at_the_stated_value(tmp_path):
    values = np.full((10, 10), _NAN, dtype="float32")
    values[5, 2] = -6.0
    layer = _layer(_write(values, west=-82.5, north=43.0, cell=0.001,
                          crs="EPSG:4326"), "bed")
    out = filled(layer, _WEST, 176.041, _output_dir=str(tmp_path))
    painted, _won = _bands(out.uri)
    # A cell on the polygon's edge with nothing measured on it takes the seed.
    assert painted[0, 0] == pytest.approx(176.041)
    assert painted[5, 2] == pytest.approx(-6.0)


def test_the_fill_never_reads_ground_outside_within(tmp_path):
    """Inside a polygon nothing measured, the only value the fill has is the
    seed at its edge; terrain outside it is never read in."""
    values = np.full((10, 10), _NAN, dtype="float32")
    values[:, 6:] = 100.0
    layer = _layer(_write(values, west=-82.5, north=43.0, cell=0.001,
                          crs="EPSG:4326"), "bed")
    out = filled(layer, _WEST, 176.041, _output_dir=str(tmp_path))
    painted, _won = _bands(out.uri)
    assert np.allclose(painted[:, :5], 176.041)
    assert np.all(painted[:, 6:] == 100.0)


def test_a_fill_with_no_seed_refuses_rather_than_reading_one_off_a_run():
    with pytest.raises(RasterLayerError) as refused:
        asyncio.run(fill_nodata(layer="L1", within=_WEST))
    assert refused.value.error_code == "FILL_NODATA_SEED_UNSTATED"


def test_a_bed_whose_water_nothing_measured_is_refused_naming_both_remedies():
    from trid3nt_server.inputs.bed import RASTER, bed
    from trid3nt_server.inputs.user_input import UserInputError

    with pytest.raises(UserInputError) as refused:
        bed(_holed(), water=_WEST)
    said = str(refused.value)
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "merge_rasters" in said and "fill_nodata" in said
    # The east hole is LAND to this cut: said, and not refused.
    east = {"type": "Polygon", "coordinates": [[
        [-82.5, 42.99], [-82.4995, 42.99], [-82.4995, 43.0], [-82.5, 43.0],
        [-82.5, 42.99]]]}
    assert bed(_holed(), water=east).kind == RASTER


def test_water_past_the_surface_s_own_edge_is_a_wet_hole():
    from trid3nt_server.inputs.bed import bed
    from trid3nt_server.inputs.user_input import UserInputError

    whole = _layer(_write(np.full((10, 10), -6.0), west=-82.5, north=43.0,
                          cell=0.001, crs="EPSG:4326"), "bed")
    wider = {"type": "Polygon", "coordinates": [[
        [-82.5, 42.98], [-82.49, 42.98], [-82.49, 43.0], [-82.5, 43.0],
        [-82.5, 42.98]]]}
    with pytest.raises(UserInputError, match="WET hole"):
        bed(whole, water=wider)


def test_ops_is_on_no_signature():
    import inspect

    import trid3nt_server.main as main

    main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.inputs.bed import bed

    assert "op" not in inspect.signature(bed).parameters
    carrying = sorted(name for name, tool in TOOL_REGISTRY.items()
                      if "ops" in inspect.signature(tool.fn).parameters
                      and getattr(tool.fn, "workflow", None) is not None)
    assert carrying == []
    assert any(getattr(tool.fn, "workflow", None) is not None
               for tool in TOOL_REGISTRY.values())
