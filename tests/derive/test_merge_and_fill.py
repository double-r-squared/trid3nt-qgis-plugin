"""The two raster derives: merge_rasters and fill_nodata, offline.

Covered: the first input winning every cell it measured and the next painting
only what it left, the order of the list being the priority, the second band
naming which input each cell came from, the coverage feedback per input, each
input read onto the LAST input's frame through the shift it publishes or the
offset stated for it, the merged surface stating the shift its zero's input
publishes, two surfaces with no common ground refusing, a fill
reaching inside 'within' alone with its edge at the stated seed and every cell it
painted marked FILLED, a fill with no seed refusing, a bed whose water nothing
measured refused naming both derives, ops= gone from every signature, inputs on
differing datums refusing without an offset and naming the fetch that measures
one, a case layer by id carrying its datum and quantity, a source refused
because a derive never fetches, and a fetched layer carrying both."""

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
    FILLED, UNMEASURED, RasterLayerError, case_layer)
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
    survey = LayerURI(
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


def test_the_merged_surface_states_the_shift_its_zero_s_input_publishes(tmp_path):
    terrain = _layer(_write(np.full((4, 4), 10.0), west=500_000.0,
                            north=4_000_000.0, cell=4.0), "terrain", "CRD",
                     datum_offset_m=1.6093, datum_offset_frame="NAVD88")
    laid = merged([_survey("CRD"), terrain], "bed", [None, None],
                  _output_dir=str(tmp_path))
    assert (laid.vertical_datum, laid.datum_offset_m,
            laid.datum_offset_frame) == ("CRD", 1.6093, "NAVD88")


def test_a_stated_offset_moves_an_input_onto_the_frame(tmp_path):
    laid = merged([_survey("EGM2008"), _terrain()], "bed",
                  [{"offset_m": 0.5, "from_frame": "EGM2008",
                    "to_frame": "NAVD88", "source": "NOAA VDatum"}, None],
                  _output_dir=str(tmp_path))
    values, _won = _bands(laid.uri)
    assert values[0, 4] == pytest.approx(-4.5)


def test_inputs_on_differing_datums_with_no_offset_refuse_naming_the_offset_fetch(
        tmp_path):
    with pytest.raises(RasterLayerError) as refused:
        merged([_survey("EGM2008"), _terrain()], "bed", [],
               _output_dir=str(tmp_path))
    assert refused.value.error_code == "MERGE_RASTERS_OFFSET_UNSTATED"
    assert "fetch_vertical_datum_offset" in str(refused.value)
    assert "EGM2008" in str(refused.value) and "NAVD88" in str(refused.value)


def test_one_offset_record_reads_every_input_on_its_pair_either_way_round(
        tmp_path):
    """The record the offset fetch returns is matched by the frames it names,
    so a depth survey and a chart on one datum share it, in any order."""
    record = {"offset_m": 176.056, "from_frame": "NAVD88",
              "to_frame": "LWD_IGLD85", "source": "NOAA VDatum"}
    depths = _layer(_write(np.full((4, 4), 5.0), west=500_004.0,
                           north=4_000_000.0, cell=1.0), "survey", "LWD_IGLD85",
                    quantity="depth_below_datum_m")
    laid = merged([depths, _terrain()], "bed", [None, record],
                  _output_dir=str(tmp_path))
    values, _won = _bands(laid.uri)
    assert values[0, 4] == pytest.approx(-176.056 - 5.0, abs=1e-3)
    assert laid.quantity == "elevation"


def test_a_case_layer_by_id_carries_its_datum_and_quantity_into_the_merge(
        tmp_path):
    """A layer named by id brings what its producer said about it: the depth
    survey on its own datum is read through the offset, never as on the frame."""
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)
    from trid3nt_server.tools.derive.merge_rasters.merge_rasters import (
        merge_rasters)

    survey = _layer(_write(np.full((4, 4), 5.0), west=500_004.0,
                           north=4_000_000.0, cell=1.0), "survey-1", "CRD",
                    quantity="depth_below_datum_m")
    registry = get_uri_registry("merge-by-id")
    registry.register_tool_result("run_qgis_algorithm", survey)
    token = activate_registry(registry)
    try:
        held = case_layer("survey-1", "input")
        assert held["vertical_datum"] == "CRD"
        assert held["quantity"] == "depth_below_datum_m"
        with pytest.raises(RasterLayerError) as refused:
            asyncio.run(merge_rasters(layers=["survey-1", _terrain()]))
        assert refused.value.error_code == "MERGE_RASTERS_OFFSET_UNSTATED"
    finally:
        deactivate_registry(token)


def test_a_derive_never_fetches_a_source_named_to_it():
    from trid3nt_server.tools.derive.merge_rasters.merge_rasters import (
        merge_rasters)

    with pytest.raises(RasterLayerError) as refused:
        asyncio.run(merge_rasters(layers=[{"source": "fetch_dem",
                                           "bbox": [0, 0, 1, 1]}]))
    assert refused.value.error_code == "RASTER_LAYER_UNKNOWN"
    assert "never fetches" in str(refused.value)


def test_no_derive_reaches_the_tool_registry():
    """A derive reaching TOOL_REGISTRY can call a fetcher by name."""
    import ast
    from pathlib import Path

    import trid3nt_server.tools.derive as derive

    found = []
    for path in Path(derive.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                    and node.value.id == "TOOL_REGISTRY":
                found.append(f"{path.name}:{node.lineno}")
    assert not found


def test_a_fetched_layer_carries_the_datum_and_the_quantity_its_row_states():
    import trid3nt_server.main as main
    from trid3nt_server.tools.fetchers._router.registration import get_spec
    from trid3nt_server.tools.fetchers._router.router import build_layer_uri

    main._import_tools_registry()
    box = {"bbox": [-82.47, 42.88, -82.40, 42.99]}
    chart = build_layer_uri(get_spec("fetch_chs_nonna"), box, "s3://b/c.tif")
    assert (chart.vertical_datum, chart.quantity) == ("lwd_igld85", "elevation")
    terrain = build_layer_uri(get_spec("fetch_dem"), box, "s3://b/d.tif")
    assert (terrain.vertical_datum, terrain.quantity) == ("NAVD88", "elevation")
    survey = build_layer_uri(get_spec("fetch_ehydro_surveys"), box, "s3://b/e.fgb")
    assert survey.quantity == "depth_below_datum"


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


def test_no_slot_reads_an_op():
    """The bed takes a composed layer, so no slot's ingestion declares an op and
    the slots offer no reader of one."""
    import inspect

    from trid3nt_server.inputs import slots

    assert not hasattr(slots, "takes_op")
    reading = sorted(name for name, slot in slots.SLOTS.items() if slot.ingest
                     and "op" in inspect.signature(slot.ingest).parameters)
    assert reading == []


#: A lon/lat box around the UTM 10N test surfaces.
_AROUND = {"type": "Polygon", "coordinates": [[
    [-123.01, 36.10], [-122.99, 36.10], [-122.99, 36.20], [-123.01, 36.20],
    [-123.01, 36.10]]]}


def test_layers_named_by_id_carry_the_offset_through_the_tool_and_the_datum_through_the_fill(
        tmp_path, monkeypatch):
    """The merge TOOL hands its offsets to the overlay, and a fill of a layer
    named by id states that layer's datum, quantity and sources on what it
    makes."""
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)
    import sys

    fill_mod = sys.modules[filled.__module__]
    merge_mod = sys.modules[merged.__module__]

    monkeypatch.setattr(merge_mod, "merged", lambda layers, name, offsets: merged(
        layers, name, offsets, _output_dir=str(tmp_path)))
    monkeypatch.setattr(fill_mod, "filled", lambda layer, within, seed: filled(
        layer, within, seed, _output_dir=str(tmp_path)))
    depths = _layer(_write(np.full((4, 4), 5.0), west=500_004.0,
                           north=4_000_000.0, cell=1.0), "survey-2",
                    "LWD_IGLD85", quantity="depth_below_datum_m")
    registry = get_uri_registry("merge-tool-offsets")
    registry.register_tool_result("run_qgis_algorithm", depths)
    registry.register_tool_result("fetch_dem", _terrain())
    token = activate_registry(registry)
    try:
        laid = asyncio.run(merge_mod.merge_rasters(
            layers=["survey-2", "terrain"], name="bed",
            offsets=[{"offset_m": 176.056, "from_frame": "NAVD88",
                      "to_frame": "LWD_IGLD85", "source": "NOAA VDatum"}]))
        values, _won = _bands(laid.uri)
        assert values[0, 4] == pytest.approx(-181.056, abs=1e-3)
        registry.register_tool_result("merge_rasters", laid)
        out = asyncio.run(fill_nodata(layer=laid.layer_id, within=_AROUND,
                                      seed=176.582))
        assert (out.vertical_datum, out.quantity) == ("NAVD88", "elevation")
        assert out.sources[:2] == ["survey-2", "terrain"]
    finally:
        deactivate_registry(token)


def test_the_datum_quantity_and_shift_survive_fetch_qgis_merge_and_fill(
        tmp_path, monkeypatch, fake_s3):
    """The three fields ride the layer record from the fetch through QGIS's
    grid into the merge, which reads the depths through the shift the fetch
    recorded, and the fill keeps what its input states."""
    import io
    import sys

    import geopandas as gpd
    from shapely.geometry import Point

    from trid3nt_contracts.processing_contracts import ProcessingResponsePayload
    from trid3nt_server import storage
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)
    from trid3nt_server.tools.derive.run_qgis_algorithm import (
        run_qgis_algorithm as qgis)
    from trid3nt_server.tools.fetchers._router import router
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    spec = compose_specs_from_tree()["fetch_ehydro_surveys"]
    rows = gpd.GeoDataFrame(
        {"vertical_datum": ["CRD"] * 2, "datum_offset_m": [1.6093] * 2,
         "datum_offset_frame": ["NAVD88"] * 2, "depth_below_datum_m": [5.0, 5.0]},
        geometry=[Point(-122.67, 45.52), Point(-122.68, 45.53)], crs="EPSG:4326")
    buffer = io.BytesIO()
    rows.to_file(buffer, driver="FlatGeobuf")
    fetched = router.build_layer_uri(spec, {"bbox": (-122.7, 45.5, -122.6, 45.6)},
                                     "s3://b/soundings.fgb")
    fetched = fetched.model_copy(
        update=router._stated_by_records(buffer.getvalue()))
    grid = _write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0,
                  cell=1.0)

    async def _session(**_kw):
        return ProcessingResponsePayload(
            request_id="01HZZZZZZZZZZZZZZZZZZZZZZZ", status="ok",
            result={"layer_name": "Grid (IDW)", "layer_id": "grid_idw_1",
                    "kind": "raster", "source": grid})

    monkeypatch.setattr(qgis, "run_in_session", _session)
    monkeypatch.setattr(storage, "_CLIENT", fake_s3)
    fill_mod = sys.modules[filled.__module__]
    monkeypatch.setattr(fill_mod, "filled", lambda layer, within, seed: filled(
        layer, within, seed, _output_dir=str(tmp_path)))
    registry = get_uri_registry("three-fields")
    registry.register_tool_result("fetch_ehydro_surveys", fetched)
    registry.register_tool_result("fetch_dem", _terrain())
    token = activate_registry(registry)
    try:
        gridded = asyncio.run(qgis.run_qgis_algorithm(
            "gdal:gridinversedistancenearestneighbor",
            {"INPUT": fetched.layer_id, "RADIUS": 9.14}))
        registry.register_tool_result("run_qgis_algorithm", gridded)
        held = case_layer(gridded.layer_id, "input")
        laid = merged([held, case_layer("terrain", "input")], "bed", [],
                      _output_dir=str(tmp_path))
        kept = asyncio.run(fill_nodata(layer=gridded.layer_id, within=_AROUND,
                                       seed=-3.0))
    finally:
        deactivate_registry(token)
    stated = ("CRD", "depth_below_datum", 1.6093, "NAVD88")
    assert (held["vertical_datum"], held["quantity"], held["datum_offset_m"],
            held["datum_offset_frame"]) == stated
    values, _won = _bands(laid.uri)
    assert values[0, 4] == pytest.approx(1.6093 - 5.0, abs=1e-4)
    assert (laid.vertical_datum, laid.quantity) == ("NAVD88", "elevation")
    assert (kept.vertical_datum, kept.quantity, kept.datum_offset_m,
            kept.datum_offset_frame) == stated
