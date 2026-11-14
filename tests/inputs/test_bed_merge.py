"""The bed slot's merge and fill, offline.

Covered: an ordered list handed the bed laid first-wins with band 2 naming each cell's surface, the order
being the priority, each surface read on the run's frame through its own shift or its own offset, a pair
nothing bridges refusing, surfaces with no common ground refusing, a cliff refusing, a hole inside the cut
filled by the FILL_ALGORITHM setting up to FILL_MAX_DISTANCE and marked FILLED, a hole past it refusing by
the setting's name, the fill reading inside the cut alone, linear selectable, the seam blended over
MERGE_BUFFER_CELLS, water past every surface's edge a hole, one surface with nothing painted handed on as
it came, an engine laying its own ladder neither merging nor filling, the age between the surfaces stated,
a case layer by id carrying its datum and quantity, and a fetched layer carrying both."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.inputs.bed import RASTER, bed
from trid3nt_server.inputs.run_notes import bind_coverage, drain_coverage
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.settings import bind_run, reset_run
from trid3nt_server.tools.derive._raster_layers import FILLED, UNMEASURED

_NAN = float("nan")


@pytest.fixture(autouse=True)
def _written_locally(monkeypatch, tmp_path):
    """Every raster the bed writes lands under the test's own directory, never the store."""
    from trid3nt_server.tools.derive import _hydrology_common as common

    real = common.write_cog
    monkeypatch.setattr(common, "write_cog", lambda band, **kw: real(
        band, **{**kw, "output_dir": kw.get("output_dir") or str(tmp_path)}))


def _write(values: np.ndarray, *, west: float, north: float, cell: float,
           crs: str = "EPSG:32610") -> str:
    handle, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_bed_")
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


def _terrain(datum: str | None = "NAVD88", **kw) -> LayerURI:
    """A 4 m surface over 16 m of ground, the survey inside it."""
    return _layer(_write(np.full((4, 4), 10.0), west=500_000.0,
                         north=4_000_000.0, cell=4.0), "terrain", datum, **kw)


def _bands(layer) -> tuple[np.ndarray, np.ndarray]:
    with rasterio.open(layer.uri) as src:
        return src.read(1), src.read(2).astype("uint8")


def _said(run):
    """What ``run()`` stated on the coverage channel, and what it returned."""
    token = bind_coverage()
    try:
        out = run()
    finally:
        said = drain_coverage(token)
    return out, said


def test_the_first_surface_wins_where_it_measured_and_the_next_paints_the_rest():
    laid, said = _said(lambda: bed([_survey(), _terrain()], frame="NAVD88"))
    values, won = _bands(laid.source)
    assert values.shape == (16, 16), "the finest cell over the union"
    assert np.all(values[:4, 4:8] == -5.0) and np.all(values[8:, :] == 10.0)
    # THE SECOND BAND names the surface by its place in the list.
    assert np.all(won[:4, 4:8] == 0) and np.all(won[8:, :] == 1)
    assert laid.source.sources[:2] == ["survey", "terrain"]
    assert dict(laid.source.coverage)["survey"] == 0.0625
    assert laid.source.vertical_datum == "NAVD88"
    assert any("survey 6.2%" in line and "terrain 93.8%" in line for line in said)


def test_the_order_of_the_list_is_the_priority():
    laid = bed([_terrain(), _survey()], frame="NAVD88")
    values, won = _bands(laid.source)
    assert np.all(values == 10.0) and np.all(won == 0)


def test_a_survey_of_depths_is_read_onto_the_run_s_frame_through_its_own_shift():
    """5 m of depth below a project datum 1.6093 m above NAVD88 is -3.3907 m on NAVD88."""
    survey = _layer(_write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0, cell=1.0),
                    "soundings", "CRD", quantity="depth_below_datum_m",
                    datum_offset_m=1.6093, datum_offset_frame="NAVD88")
    values, _won = _bands(bed([survey, _terrain()], frame="NAVD88").source)
    assert values[0, 4] == pytest.approx(-3.3907, abs=1e-4)


def test_each_surface_is_moved_onto_the_frame_by_its_own_offset():
    record = {"offset_m": 0.5, "from_frame": "EGM2008", "to_frame": "NAVD88",
              "source": "NOAA VDatum"}
    values, _won = _bands(bed([_survey("EGM2008"), _terrain()], frame="NAVD88",
                              offset=[record, None]).source)
    assert values[0, 4] == pytest.approx(-4.5)


def test_a_surface_no_offset_bridges_refuses_naming_both_frames():
    with pytest.raises(UserInputError) as refused:
        bed([_survey("EGM2008"), _terrain()], frame="NAVD88")
    assert "EGM2008" in str(refused.value) and "NAVD88" in str(refused.value)


def test_surfaces_with_no_common_ground_refuse():
    far = _layer(_write(np.full((4, 4), _NAN), west=500_100.0, north=4_000_000.0,
                        cell=1.0), "far")
    with pytest.raises(UserInputError) as refused:
        bed([far, _layer(far.uri, "farther")], frame="NAVD88")
    assert refused.value.error_code == "BED_DISJOINT"


def test_two_populations_farther_apart_than_the_relief_refuse_as_a_cliff():
    under = np.full((4, 4), 10.0)
    under[0, 0] = 12.0
    terrain = _layer(_write(under, west=500_000.0, north=4_000_000.0, cell=4.0), "terrain")
    survey = _layer(_write(np.full((4, 4), -400.0), west=500_004.0, north=4_000_000.0,
                           cell=1.0), "survey")
    with pytest.raises(UserInputError) as refused:
        bed([survey, terrain], frame="NAVD88")
    assert refused.value.error_code == "BED_CLIFF"
    assert "MERGE_CLIFF_FACTOR" in str(refused.value)


def _holed() -> LayerURI:
    """A lon/lat surface with a hole in its west half and one in its east."""
    values = np.full((10, 10), -6.0, dtype="float32")
    values[4:6, 1:4] = _NAN
    values[4:6, 6:9] = _NAN
    return _layer(_write(values, west=-82.5, north=43.0, cell=0.001, crs="EPSG:4326"), "bed")


#: The west half of the holed surface.
_WEST = {"type": "Polygon", "coordinates": [[
    [-82.5, 42.99], [-82.495, 42.99], [-82.495, 43.0], [-82.5, 43.0], [-82.5, 42.99]]]}

#: The whole of the holed surface.
_ALL = {"type": "Polygon", "coordinates": [[
    [-82.5, 42.99], [-82.49, 42.99], [-82.49, 43.0], [-82.5, 43.0], [-82.5, 42.99]]]}


def test_a_hole_inside_the_cut_is_filled_and_marked_and_one_outside_is_left():
    laid, said = _said(lambda: bed(_holed(), frame="NAVD88", water=_WEST))
    values, won = _bands(laid.source)
    assert np.all(np.isfinite(values[4:6, 1:4])) and np.all(won[4:6, 1:4] == FILLED)
    assert np.allclose(values[4:6, 1:4], -6.0)
    assert np.all(np.isnan(values[4:6, 6:9])), "a dry hole is left to the mesh"
    assert np.all(won[4:6, 6:9] == UNMEASURED)
    assert any("The fill painted 6 cells" in line and "inverse_distance" in line
               and "100 px" in line for line in said)
    assert any(line.startswith("Over the WATER") and "filled" in line for line in said)


def test_a_hole_past_the_fill_s_reach_refuses_naming_the_setting_and_another_surface():
    values = np.full((20, 20), -6.0, dtype="float32")
    values[6:14, 6:14] = _NAN
    wide = _layer(_write(values, west=-82.5, north=43.0, cell=0.0005, crs="EPSG:4326"), "chart")
    token = bind_run({"FILL_MAX_DISTANCE": 2})
    try:
        with pytest.raises(UserInputError) as refused:
            bed(wide, frame="NAVD88", water=_ALL)
    finally:
        reset_run(token)
    said = str(refused.value)
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "FILL_MAX_DISTANCE (now 2 px)" in said and "further surface" in said
    assert "merge_rasters" not in said and "fill_nodata" not in said
    laid = bed(wide, frame="NAVD88", water=_ALL)
    assert np.all(np.isfinite(_bands(laid.source)[0]))


def test_the_fill_reads_inside_the_cut_alone():
    """Inside a cut nothing measured but one cell, that cell is all the fill reads; the
    high ground east of the cut is never read in."""
    values = np.full((10, 10), _NAN, dtype="float32")
    values[:, 6:] = 100.0
    values[5, 2] = -6.0
    layer = _layer(_write(values, west=-82.5, north=43.0, cell=0.001, crs="EPSG:4326"), "bed")
    painted, _won = _bands(bed(layer, frame="NAVD88", water=_WEST).source)
    assert np.allclose(painted[:, :5], -6.0)
    assert np.all(painted[:, 6:] == 100.0)


def test_linear_is_one_setting_away():
    values = np.full((3, 12), _NAN, dtype="float32")
    values[:, 0], values[:, 11] = 0.0, 11.0
    line = _layer(_write(values, west=-82.5, north=43.0, cell=0.001, crs="EPSG:4326"), "bed")
    cut = {"type": "Polygon", "coordinates": [[
        [-82.5, 42.997], [-82.488, 42.997], [-82.488, 43.0], [-82.5, 43.0], [-82.5, 42.997]]]}
    token = bind_run({"FILL_ALGORITHM": "linear"})
    try:
        painted, _won = _bands(bed(line, frame="NAVD88", water=cut).source)
    finally:
        reset_run(token)
    assert np.allclose(painted[1], np.arange(12.0), atol=1e-4)


def test_the_seam_is_blended_over_merge_buffer_cells():
    """HydroMT-SFINCS buffer_cells: the later surface's cells within N of the earlier one's are
    interpolated between them (its own run gives 0 0 0 0 3.33 6.67 10 at two cells, linear)."""
    first = np.full((3, 10), _NAN, dtype="float32")
    first[:, :4] = 0.0
    a = _layer(_write(first, west=500_000.0, north=4_000_000.0, cell=1.0), "first")
    b = _layer(_write(np.full((3, 10), 10.0), west=500_000.0, north=4_000_000.0, cell=1.0), "second")
    token = bind_run({"MERGE_BUFFER_CELLS": 2, "FILL_ALGORITHM": "linear"})
    try:
        values, won = _bands(bed([a, b], frame="NAVD88").source)
    finally:
        reset_run(token)
    assert np.allclose(values[1], [0, 0, 0, 0, 10 / 3, 20 / 3, 10, 10, 10, 10], atol=1e-3)
    assert list(won[1]) == [0, 0, 0, 0, FILLED, FILLED, 1, 1, 1, 1]


def test_water_past_every_surface_s_edge_is_a_hole_the_fill_reaches_or_refuses():
    whole = _layer(_write(np.full((10, 10), -6.0), west=-82.5, north=43.0, cell=0.001,
                          crs="EPSG:4326"), "bed")
    wider = {"type": "Polygon", "coordinates": [[
        [-82.5, 42.98], [-82.49, 42.98], [-82.49, 43.0], [-82.5, 43.0], [-82.5, 42.98]]]}
    values, won = _bands(bed(whole, frame="NAVD88", water=wider).source)
    assert values.shape == (20, 10) and np.all(won[10:, :] == FILLED)
    token = bind_run({"FILL_MAX_DISTANCE": 3})
    try:
        with pytest.raises(UserInputError, match="WET hole"):
            bed(whole, frame="NAVD88", water=wider)
    finally:
        reset_run(token)


def test_one_surface_with_nothing_to_paint_is_handed_on_as_it_came():
    whole = _layer(_write(np.full((10, 10), -6.0), west=-82.5, north=43.0, cell=0.001,
                          crs="EPSG:4326"), "bed")
    assert bed(whole, frame="NAVD88", water=_WEST).source is whole


def test_an_engine_laying_its_own_ladder_neither_merges_nor_fills():
    with pytest.raises(UserInputError) as refused:
        bed([_survey(), _terrain()], frame="NAVD88", water={}, merge=False)
    assert refused.value.error_code == "BED_LADDER"
    with pytest.raises(UserInputError) as refused:
        bed(_holed(), frame="NAVD88", water=_WEST, merge=False)
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "FILL_MAX_DISTANCE" not in str(refused.value)


def test_the_age_between_the_surfaces_is_stated():
    _laid, said = _said(lambda: bed(
        [_survey(reference_time="2019-05-01T00:00:00Z"),
         _terrain(reference_time="2023-05-01T00:00:00Z"), _terrain()], frame="NAVD88"))
    assert any("measured 1461 days apart" in line and "terrain states no instant" in line
               for line in said)


def test_a_stated_depth_is_never_one_surface_of_several():
    with pytest.raises(UserInputError, match="whole flat bed"):
        bed([_survey(), 4.0], frame="NAVD88")
    assert bed([_survey()], frame="NAVD88").kind == RASTER


def test_a_case_layer_by_id_carries_its_datum_and_quantity_into_the_list():
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)

    survey = _layer(_write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0, cell=1.0),
                    "survey-1", "CRD", quantity="depth_below_datum_m")
    registry = get_uri_registry("bed-by-id")
    registry.register_tool_result("run_qgis_algorithm", survey)
    token = activate_registry(registry)
    try:
        with pytest.raises(UserInputError) as refused:
            bed([{"layer": "survey-1"}, _terrain()], frame="NAVD88")
    finally:
        deactivate_registry(token)
    assert "CRD" in str(refused.value)


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


def test_no_slot_reads_an_op():
    import inspect

    from trid3nt_server.inputs import slots

    assert "op" not in inspect.signature(bed).parameters
    reading = sorted(slot.name for slot in vars(slots).values()
                     if isinstance(slot, slots.Slot) and slot.ingest
                     and "op" in inspect.signature(slot.ingest).parameters)
    assert reading == []


def test_the_datum_quantity_and_shift_survive_fetch_and_qgis_into_the_bed(monkeypatch, fake_s3):
    """The three fields ride the layer record from the fetch through QGIS's grid into the bed's list,
    which reads the depths through the shift the fetch recorded."""
    import asyncio
    import io

    import geopandas as gpd
    from shapely.geometry import Point

    from trid3nt_contracts.processing_contracts import ProcessingResponsePayload
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry, lookup_layer_for_handle)
    from trid3nt_server.store import objects as storage
    from trid3nt_server.tools.derive.run_qgis_algorithm import run_qgis_algorithm as qgis
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
    fetched = fetched.model_copy(update=router._stated_by_records(buffer.getvalue()))
    grid = _write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0, cell=1.0)

    async def _session(**_kw):
        return ProcessingResponsePayload(
            request_id="01HZZZZZZZZZZZZZZZZZZZZZZZ", status="ok",
            result={"layer_name": "Grid (IDW)", "layer_id": "grid_idw_1",
                    "kind": "raster", "source": grid})

    monkeypatch.setattr(qgis, "run_in_session", _session)
    monkeypatch.setattr(storage, "_CLIENT", fake_s3)
    registry = get_uri_registry("three-fields")
    registry.register_tool_result("fetch_ehydro_surveys", fetched)
    registry.register_tool_result("fetch_dem", _terrain())
    token = activate_registry(registry)
    try:
        gridded = asyncio.run(qgis.run_qgis_algorithm(
            "gdal:gridinversedistancenearestneighbor",
            {"INPUT": fetched.layer_id, "RADIUS": 9.14}))
        registry.register_tool_result("run_qgis_algorithm", gridded)
        held = lookup_layer_for_handle(gridded.layer_id)
        laid = bed([gridded.layer_id, "terrain"], frame="NAVD88")
    finally:
        deactivate_registry(token)
    assert (held["vertical_datum"], held["quantity"], held["datum_offset_m"],
            held["datum_offset_frame"]) == ("CRD", "depth_below_datum", 1.6093, "NAVD88")
    values, _won = _bands(laid.source)
    assert values[0, 4] == pytest.approx(1.6093 - 5.0, abs=1e-4)
    assert laid.source.vertical_datum == "NAVD88"


def test_a_cell_the_cut_touches_is_filled_though_its_centre_is_outside_it():
    """The mesh's rim nodes stand on the cut's edge; the cell under one is in the fill and in the water count."""
    values = np.full((10, 10), -6.0, dtype="float32")
    values[:, 4] = _NAN
    layer = _layer(_write(values, west=-82.5, north=43.0, cell=0.001, crs="EPSG:4326"), "bed")
    edge = {"type": "Polygon", "coordinates": [[
        [-82.5, 42.99], [-82.4958, 42.99], [-82.4958, 43.0], [-82.5, 43.0], [-82.5, 42.99]]]}
    laid, said = _said(lambda: bed(layer, frame="NAVD88", water=edge))
    values, won = _bands(laid.source)
    assert np.all(np.isfinite(values[:, 4])) and np.all(won[:, 4] == FILLED)
    assert any("filled" in line for line in said if line.startswith("Over the WATER"))
