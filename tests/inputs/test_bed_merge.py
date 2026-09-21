"""The bed slot's MERGE: a ranked LADDER of measurements, a wider surface under
all of them.

Covered: every rung painting what the rungs above it left and the whole ladder
refusing where none of them measured a cell, the top rung winning every cell it
measured and the fallback filling the rest, the merged grid taking the finer cell over the union of both, the sidecar
naming which input painted each cell, an absent row passing the other surface
through, two different vertical datums refusing by name, an unstated datum
refusing, a STATED OFFSET bringing two differing datums onto one axis while an
offset between two other frames still refuses, EACH input read onto the run's
own frame through the offset row declared for it, a region no service serves
still refusing by name, a primary of DEPTHS read as elevations on the frame
through the shift the survey publishes about itself, two disjoint surfaces, a measurement over half a domain landing survey and
terrain in the right halves even though it is read onto a finer grid, a corner
no sounding stood in coming back terrain, a rung the offset service cannot place
dropping off while the rungs under it paint, the TOP rung being unplaceable
refusing the whole merge, a ladder left with one rung passing it through, and a
merged bed splitting into two populations farther apart than the domain's own
relief refusing as a cliff while an ordinary channel cut into the terrain around
it merges, the terrain rung painting only OUTSIDE the polygon the domain was cut
with, the share of the water measured by nothing reaching the result, the
journal and the input row, a merge handed no cut claiming nothing about the
water, a cut carrying no polygon refusing rather than painting it, and the op a
run states painting that water from the shoreline at depth zero to the
measurements as one more rung, ranked last and per cell, while an op the merge
does not know refuses by name."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.inputs.bed import (
    MergeRastersError,
    SurveySurfaceLayerURI,
    merged_surface,
    survey_surface,
)

_NODATA = float("nan")


def _write(values: np.ndarray, *, west: float, north: float, cell: float) -> str:
    handle, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_merge_")
    os.close(handle)
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype="float32", crs="EPSG:32610", nodata=_NODATA,
        transform=from_origin(west, north, cell, cell),
    ) as destination:
        destination.write(values.astype("float32"), 1)
    return path


def _layer(path: str, datum: str | None) -> LayerURI:
    return LayerURI(layer_id="x", name=os.path.basename(path), layer_type="raster",
                    uri=path, vertical_datum=datum)


def _survey(datum: str | None = "NAVD88") -> LayerURI:
    """A 1 m measurement over the middle 4 m of the terrain below."""
    return _layer(_write(np.full((4, 4), -5.0), west=500_004.0, north=4_000_000.0,
                         cell=1.0), datum)


def _terrain(datum: str | None = "NAVD88") -> LayerURI:
    """A 4 m surface over 16 m of ground, the survey inside it."""
    return _layer(_write(np.full((4, 4), 10.0), west=500_000.0, north=4_000_000.0,
                         cell=4.0), datum)


def _soundings(datum: str = "CRD", offset: float | None = 1.6093,
               frame: str = "NAVD88") -> SurveySurfaceLayerURI:
    """The same 1 m measurement, stated as DEPTHS below a project datum - what a
    channel survey gridded from eHydro rows actually is."""
    return SurveySurfaceLayerURI(
        layer_id="s", name="depth_below_datum_m interpolated at 1 m",
        layer_type="raster",
        uri=_write(np.full((4, 4), 5.0), west=500_004.0, north=4_000_000.0,
                   cell=1.0),
        quantity="depth_below_datum_m", vertical_datum=datum,
        datum_offset_m=offset, datum_offset_frame=frame)


def test_the_primary_wins_where_it_measured_and_the_fallback_fills_the_rest(
        tmp_path) -> None:
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as src:
        values = src.read(1)
        # The finer input sets the cell, over the union of both footprints.
        assert src.res == pytest.approx((1.0, 1.0))
        assert (src.width, src.height) == (16, 16)
    assert float(np.nanmin(values)) == pytest.approx(-5.0)
    assert float(np.nanmax(values)) == pytest.approx(10.0)
    assert merged.primary_fraction == pytest.approx(16 / 256.0, abs=1e-3)
    assert merged.fallback_fraction + merged.primary_fraction == pytest.approx(1.0)
    assert merged.resolution_m == pytest.approx(1.0)


def test_the_sidecar_says_which_input_painted_each_cell(tmp_path) -> None:
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
        assert won.shape == values.shape
    assert set(np.unique(won).tolist()) == {0, 1}
    assert float(values[won == 0].max()) == pytest.approx(-5.0)
    assert float(values[won == 1].min()) == pytest.approx(10.0)


def test_a_measurement_over_half_a_domain_lands_in_the_right_halves(tmp_path) -> None:
    """The primary is read onto a FINER grid than its own, and the cells it wins
    are exactly the ones it measured: a warp that grew the measurement by a cell
    would hand the merge survey provenance over ground nobody surveyed."""
    half = np.full((8, 8), -3.0)
    half[:, 4:] = _NODATA
    primary = _layer(_write(half, west=500_000.0, north=4_000_000.0, cell=8.0),
                     "NAVD88")
    fallback = _layer(_write(np.full((32, 32), 12.0), west=500_000.0,
                             north=4_000_000.0, cell=2.0), "NAVD88")
    merged = merged_surface(primary=primary, fallback=fallback,
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.provenance_uri) as source:
        won = source.read(1)
    assert won.shape == (32, 32)
    assert (won[:, :16] == 0).all(), "the surveyed half must read survey whole"
    assert (won[:, 16:] == 1).all(), "the unsurveyed half must read terrain whole"
    assert merged.primary_fraction == pytest.approx(0.5)


def _quarter_left_unsounded(step_deg: float = 0.0001, n: int = 20) -> dict:
    """Soundings over three quadrants of a patch, the north-east one left alone."""
    lon, lat = -122.673, 45.517
    features = []
    for row in range(n):
        for col in range(n):
            if row >= n // 2 and col >= n // 2:
                continue
            features.append({"type": "Feature",
                             "properties": {"depth_m": 4.0,
                                            "vertical_datum": "NAVD88"},
                             "geometry": {"type": "Point", "coordinates": [
                                 lon + col * step_deg, lat + row * step_deg]}})
    return {"type": "FeatureCollection", "features": features}


def test_a_corner_no_sounding_stood_in_comes_back_terrain(tmp_path) -> None:
    """End to end: the derive bounds its surface to the footprint, and the merge
    reads the corner outside it off the wider surface."""
    from rasterio.warp import transform as warp

    doc = _quarter_left_unsounded()
    surface = survey_surface(doc, resolution_m=5.0,
                                    _output_dir=str(tmp_path))
    west, south, east, north = surface.bbox
    fallback = _layer(_write(np.full((40, 40), 12.0), west=west - 0.0002,
                             north=north + 0.0002, cell=0.0001), "NAVD88")
    with rasterio.open(fallback.uri, "r+") as handle:
        handle.crs = rasterio.crs.CRS.from_epsg(4326)
    merged = merged_surface(primary=surface, fallback=fallback,
                                  _output_dir=str(tmp_path))
    corner = (east - 0.00005, north - 0.00005)
    sounded = (west + 0.00005, south + 0.00005)
    with rasterio.open(merged.provenance_uri) as source:
        xs, ys = warp("EPSG:4326", source.crs,
                      [corner[0], sounded[0]], [corner[1], sounded[1]])
        read = [value[0] for value in source.sample(list(zip(xs, ys)))]
    assert read[0] == 1, "a corner no sounding stood in is terrain"
    assert read[1] == 0, "the sounded corner is survey"


def test_an_absent_primary_passes_the_other_surface_through(tmp_path) -> None:
    terrain = _terrain()
    merged = merged_surface(primary=None, fallback=terrain,
                                  _output_dir=str(tmp_path))
    assert merged.uri == terrain.uri
    assert merged.primary_fraction == 0.0 and merged.fallback_fraction == 1.0
    assert "absent" in merged.notes[0]


def test_two_datums_refuse_by_name(tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_survey("CRD"), fallback=_terrain("NAVD88"),
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"
    assert "CRD" in str(excinfo.value) and "NAVD88" in str(excinfo.value)


def test_an_unstated_datum_refuses(tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_survey(None), fallback=_terrain("NAVD88"),
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUM_UNSTATED"


def test_a_stated_resolution_is_the_merged_cell(tmp_path) -> None:
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                                  resolution_m=2.0, _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as src:
        assert src.res == pytest.approx((2.0, 2.0))


def test_neither_surface_refuses() -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface()
    assert excinfo.value.error_code == "MERGE_RASTERS_NO_SOURCE"


def test_a_grid_past_the_cell_ceiling_refuses(tmp_path) -> None:
    far = _layer(_write(np.full((4, 4), 1.0), west=900_000.0, north=4_400_000.0,
                        cell=4.0), "NAVD88")
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_survey(), fallback=far,
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_RESOLUTION_INVALID"


def test_a_stated_offset_reads_the_primary_on_the_fallback_s_datum(tmp_path) -> None:
    merged = merged_surface(
        primary=_survey("CRD"), fallback=_terrain("NAVD88"),
        primary_offset={"offset_m": 1.6093, "from_frame": "CRD",
                        "to_frame": "NAVD88",
                        "source": "the survey's own metadata"},
        _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
    assert merged.vertical_datum == "NAVD88"
    assert merged.datum_shift_m == pytest.approx(1.6093)
    # The survey measured -5.0 on ITS zero and reads 1.6093 m higher on the
    # fallback's; the fallback itself is untouched.
    assert float(values[won == 0].max()) == pytest.approx(-5.0 + 1.6093)
    assert float(values[won == 1].min()) == pytest.approx(10.0)
    assert "stated by the survey's own metadata" in merged.notes[-1]


def test_an_offset_between_two_other_frames_refuses(tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(
            primary=_survey("CRD"), fallback=_terrain("NAVD88"),
            primary_offset={"offset_m": -1.131, "from_frame": "NAVD88",
                            "to_frame": "EGM2008", "source": "NOAA VDatum"},
            _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUM_OFFSET_MISMATCH"


def test_one_datum_needs_no_offset_and_shifts_nothing(tmp_path) -> None:
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                                  _output_dir=str(tmp_path))
    assert merged.datum_shift_m == 0.0 and merged.fallback_shift_m == 0.0
    assert "Every surface counts from NAVD88." in merged.notes


def test_each_source_off_the_runs_frame_is_shifted_onto_it_before_the_overlay(
        tmp_path) -> None:
    """A lake survey on IGLD85 and a terrain DEM on NAVD88 are one axis only on
    the run's own frame: each carries the offset row declared for it, both are
    re-zeroed on their own grids, and the merged surface says which shift moved
    which input."""
    merged = merged_surface(
        primary=_survey("IGLD85"), fallback=_terrain("NAVD88"), frame="EGM2008",
        primary_offset={"offset_m": 0.013, "from_frame": "IGLD85",
                        "to_frame": "EGM2008", "source": "NOAA VDatum"},
        fallback_offset={"offset_m": -1.054, "from_frame": "NAVD88",
                         "to_frame": "EGM2008", "source": "NOAA VDatum"},
        _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
    assert merged.vertical_datum == "EGM2008"
    assert merged.datum_shift_m == pytest.approx(0.013)
    assert merged.fallback_shift_m == pytest.approx(-1.054)
    assert float(values[won == 0].max()) == pytest.approx(-5.0 + 0.013)
    assert float(values[won == 1].min()) == pytest.approx(10.0 - 1.054)
    assert "elevations on IGLD85" in merged.notes[-2]
    assert "elevations on NAVD88" in merged.notes[-1]
    assert "NOAA VDatum" in merged.notes[-1]


def test_a_frame_no_offset_reaches_refuses_naming_both(tmp_path) -> None:
    """The offset fetch serves the frames it serves; a region it does not reach
    produces no row, and the merge then refuses by name rather than laying one
    surface over the other."""
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_survey("CRD"), fallback=_terrain("NAVD88"),
                       frame="NAVD88", _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"
    assert "CRD" in str(excinfo.value) and "NAVD88" in str(excinfo.value)


def test_a_primary_of_depths_is_read_as_elevations_on_the_fallbacks_zero(
        tmp_path) -> None:
    """A depth counted DOWN from a project datum and an elevation counted UP from
    a national one are one surface only through the flip, and the shift the
    survey publishes about itself is the offset nothing else serves."""
    terrain = _terrain("NAVD88")
    terrain.quantity = "elevation"
    merged = merged_surface(primary=_soundings(), fallback=terrain,
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
    assert merged.vertical_datum == "NAVD88"
    assert merged.datum_shift_m == pytest.approx(1.6093)
    # 5 m below a zero that stands 1.6093 m over NAVD88 is -3.3907 m NAVD88.
    assert float(values[won == 0].max()) == pytest.approx(1.6093 - 5.0)
    assert float(values[won == 1].min()) == pytest.approx(10.0)
    # The merged surface no longer counts the way the primary did, so it states
    # the quantity it was read onto rather than the one the primary named.
    assert merged.quantity == "elevation"
    assert "DEPTHS below CRD" in merged.notes[-1]


def test_a_depth_surface_whose_zero_nothing_bridges_refuses_by_name(
        tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_soundings(offset=None),
                             fallback=_terrain("NAVD88"),
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"
    assert "CRD" in str(excinfo.value) and "NAVD88" in str(excinfo.value)


def test_an_absent_survey_leaves_the_terrain_as_the_whole_bed(tmp_path) -> None:
    """The row a domain with no federal navigation project produces is nothing,
    and the bed is then the wider surface, stated as itself."""
    terrain = _terrain("NAVD88")
    merged = merged_surface(primary=None, fallback=terrain,
                                  _output_dir=str(tmp_path))
    assert merged.uri == terrain.uri
    assert merged.vertical_datum == "NAVD88"
    assert merged.fallback_fraction == 1.0


def _partial(columns: list[float | None], datum: str = "NAVD88") -> LayerURI:
    """A 4 m x 4 m rung at 1 m, measuring only the columns it names a value for."""
    grid = np.full((4, 4), _NODATA)
    for column, value in enumerate(columns):
        if value is not None:
            grid[:, column] = value
    return _layer(_write(grid, west=500_000.0, north=4_000_000.0, cell=1.0), datum)


def test_every_rung_paints_the_cells_the_rungs_above_it_left(tmp_path) -> None:
    """A domain reaching past the top pick is what the next rung is for: the
    ladder is laid whole, in rank order, and the sidecar names the rung that
    painted each cell by its rank."""
    merged = merged_surface(primary=[_partial([-5.0, -5.0, None, None]),
                                     _partial([None, -7.0, -7.0, None])],
                            fallback=_partial([10.0, 10.0, 10.0, 10.0]),
                            _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
    assert won.shape == (4, 4)
    assert [int(v) for v in won[0]] == [0, 0, 1, 2]
    assert [float(v) for v in values[0]] == pytest.approx([-5.0, -5.0, -7.0, 10.0])
    assert [share for _name, share in merged.rungs] == pytest.approx(
        [0.5, 0.25, 0.25])
    assert merged.primary_fraction == pytest.approx(0.75)
    assert merged.fallback_fraction == pytest.approx(0.25)


def test_each_rung_is_read_onto_the_frame_through_the_row_declared_for_it(
        tmp_path) -> None:
    """A chart datum and the system it is expressed on are metres apart, so every
    rung crosses onto the run's frame through the offset row of its own rank, and
    the run says which point was asked and how close the service claims to be."""
    merged = merged_surface(
        primary=[_partial([-5.0, -5.0, None, None], "IGLD85"),
                 _partial([None, -7.0, -7.0, None], "LWD_IGLD85")],
        fallback=_partial([10.0, 10.0, 10.0, 10.0], "NAVD88"), frame="NAVD88",
        primary_offset=[{"offset_m": 0.013, "from_frame": "IGLD85",
                         "to_frame": "NAVD88", "source": "NOAA VDatum"},
                        {"offset_m": 176.056, "from_frame": "LWD_IGLD85",
                         "to_frame": "NAVD88", "source": "NOAA VDatum",
                         "uncertainty_m": 0.2, "lon": -82.424158,
                         "lat": 42.986770}],
        _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface:
        values = surface.read(1)
    assert [float(v) for v in values[0]] == pytest.approx(
        [-5.0 + 0.013, -5.0 + 0.013, -7.0 + 176.056, 10.0])
    assert merged.datum_shift_m == pytest.approx(0.013)
    assert "(+/- 0.200 m) at (-82.42416, 42.98677)" in merged.notes[-1]


def test_a_grid_the_whole_ladder_leaves_unpainted_refuses_naming_the_rungs(
        tmp_path) -> None:
    """A rung measuring nothing is not a refusal - the next one is what it is for
    - and only a grid every rung left refuses, naming how many were tried."""
    tried = [_partial([None, None, None, None]) for _ in range(3)]
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=tried[:2], fallback=tried[2],
                       _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DISJOINT"
    assert "none of the 3 surfaces tried" in str(excinfo.value)
    assert all(os.path.basename(rung.uri) in str(excinfo.value) for rung in tried)


def test_a_rung_the_service_cannot_place_drops_off_and_the_rest_paint(
        tmp_path) -> None:
    """A rung whose zero nothing measures against the run's frame is not a rung
    of this bed: it drops off as an empty one does, the journal says which and
    why, and the rungs under it paint the cells it would have."""
    from trid3nt_server.workflows.runtime.journal import bind_notes, drain_notes

    token = bind_notes()
    try:
        merged = merged_surface(
            primary=[_partial([-5.0, -5.0, None, None], "NAVD88"),
                     _partial([None, -7.0, -7.0, None], "LWD_IGLD85")],
            fallback=_partial([10.0, 10.0, 10.0, 10.0], "NAVD88"),
            frame="NAVD88", _output_dir=str(tmp_path))
        said = drain_notes(token)
    finally:
        pass
    with rasterio.open(merged.uri) as surface:
        values = surface.read(1)
    assert [float(v) for v in values[0]] == pytest.approx([-5.0, -5.0, 10.0, 10.0])
    assert len(merged.rungs) == 2
    assert merged.primary_fraction == pytest.approx(0.5)
    assert merged.fallback_fraction == pytest.approx(0.5)
    dropped = [line for line in said if "drops off the ladder" in line]
    assert len(dropped) == 1
    assert "LWD_IGLD85" in dropped[0] and "NAVD88" in dropped[0]


def test_the_top_rung_the_service_cannot_place_refuses_the_whole_merge(
        tmp_path) -> None:
    """A bed whose best source cannot be placed is not that bed."""
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(
            primary=[_partial([-5.0, -5.0, None, None], "LWD_IGLD85"),
                     _partial([None, -7.0, -7.0, None], "NAVD88")],
            fallback=_partial([10.0, 10.0, 10.0, 10.0], "NAVD88"),
            frame="NAVD88", _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"


def test_a_ladder_left_with_one_rung_passes_that_surface_through(
        tmp_path) -> None:
    """Every rung under the top one dropping leaves one surface, which is that
    surface and not a merge of two."""
    merged = merged_surface(
        primary=_partial([-5.0, -5.0, None, None], "NAVD88"),
        fallback=_partial([10.0, 10.0, 10.0, 10.0], "LWD_IGLD85"),
        frame="NAVD88", _output_dir=str(tmp_path))
    assert merged.primary_fraction == 1.0 and merged.fallback_fraction == 0.0
    assert "passed through unchanged" in merged.notes[0]


def _relief(low: float, high: float, *, west: float, cell: float) -> LayerURI:
    """A surface with REAL relief, so the domain states a step size of its own."""
    return _layer(_write(np.linspace(low, high, 16).reshape(4, 4),
                         west=west, north=4_000_000.0, cell=cell), "NAVD88")


def test_two_populations_farther_apart_than_the_relief_refuse_as_a_cliff(
        tmp_path) -> None:
    """A measurement 170 m under a terrain that falls 5 m is two zeros that never
    met: every node is painted, so only this floor catches it."""
    survey = _relief(0.0, 2.0, west=500_004.0, cell=1.0)
    terrain = _relief(170.0, 175.0, west=500_000.0, cell=4.0)
    with pytest.raises(MergeRastersError) as raised:
        merged_surface(primary=survey, fallback=terrain, _output_dir=str(tmp_path))
    assert raised.value.error_code == "MERGE_BED_CLIFF"
    said = str(raised.value)
    assert survey.name in said and terrain.name in said
    assert "populations" in said and "relief" in said


def test_a_bed_within_the_domain_relief_merges(tmp_path) -> None:
    """The floor refuses a step no ground holds, never an ordinary channel cut
    into the terrain around it."""
    merged = merged_surface(primary=_relief(168.0, 171.0, west=500_004.0, cell=1.0),
                            fallback=_relief(170.0, 175.0, west=500_000.0, cell=4.0),
                            _output_dir=str(tmp_path))
    assert merged.primary_fraction > 0.0 and merged.fallback_fraction > 0.0


def test_the_merge_publishes_the_bed_as_an_input_layer(tmp_path, monkeypatch) -> None:
    """The surface the mesh is painted from reaches the map itself: one input row
    naming every rung and its share, read in metres on the run's own frame."""
    from trid3nt_server.render import layer_uri_emit, pipeline_emitter

    published: list[dict] = []

    async def _publish(emitter, **fields) -> bool:
        published.append(fields)
        return True

    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())
    monkeypatch.setattr(layer_uri_emit, "publish_raster_input_cog", _publish)
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                            frame="NAVD88", _output_dir=str(tmp_path))

    assert len(published) == 1
    row = published[0]
    assert row["name"] == "Input: bed (merged: {} 6.2%, {} 93.8%, datum NAVD88)".format(
        *(label for label, _share in merged.rungs))
    assert row["cog_uri"] == merged.uri
    assert row["layer_id"] == f"input-{merged.layer_id}"
    assert row["style"] == merged.style and row["style"]["units"] == "m"
    assert set(row) == {"cog_uri", "layer_id", "name", "style"}


def _cut(west: float, south: float, east: float, north: float) -> dict:
    """The water polygon the domain was cut with, in the lon/lat a domain
    publishes its geometry in, over a box stated in the rungs' own UTM zone."""
    from rasterio.warp import transform_geom

    return transform_geom("EPSG:32610", "EPSG:4326", {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north],
                         [west, north], [west, south]]]})


def test_the_terrain_paints_only_outside_the_polygon_the_domain_was_cut_with(
        tmp_path) -> None:
    """A wider surface measures the water TOP, so inside the cut it is not a bed:
    the measured rung paints the water it reached, the water it did not reach is
    left unpainted, and the ground outside the cut is the terrain's as before."""
    merged = merged_surface(
        primary=_survey(), fallback=_terrain(), frame="NAVD88",
        water=_cut(500_004.0, 3_999_992.0, 500_012.0, 4_000_000.0),
        _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface:
        values = surface.read(1)
    # The 1 m grid spans 500,000-500,016 east and 3,999,984-4,000,000 north, so
    # the cut is its columns 4-11 over rows 0-7 and the survey is the top-left
    # 4 x 4 of that.
    assert float(values[0, 4]) == pytest.approx(-5.0)
    assert np.isnan(values[7, 11])
    assert np.isnan(values[0, 8])
    assert float(values[0, 12]) == pytest.approx(10.0)
    assert float(values[8, 4]) == pytest.approx(10.0)


def test_the_merge_states_the_share_of_the_water_measured_by_nothing(
        tmp_path) -> None:
    """The heuristic a reader weighs before asking for a surface between the
    measurements: one number over the WATER, on the result, on the journal line
    and on the input row the packet shows."""
    from trid3nt_server.workflows.runtime.journal import bind_notes, drain_notes

    token = bind_notes()
    merged = merged_surface(
        primary=_survey(), fallback=_terrain(), frame="NAVD88",
        water=_cut(500_004.0, 3_999_992.0, 500_012.0, 4_000_000.0),
        _output_dir=str(tmp_path))
    said = drain_notes(token)
    # Sixteen of the cut's sixty-four cells were sounded.
    assert merged.unmeasured_water_fraction == pytest.approx(0.75)
    stated = [line for line in said if "measured by nothing" in line]
    assert len(stated) == 1
    assert "75.0%" in stated[0]
    assert "75.0% of the water measured by nothing" in merged.input_row()["name"]


def test_a_merge_handed_no_cut_claims_nothing_about_the_water(tmp_path) -> None:
    """The share is over the polygon the domain was cut with, so a merge given
    none states no share rather than one over a grid that is not the water."""
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                            frame="NAVD88", _output_dir=str(tmp_path))
    assert merged.unmeasured_water_fraction is None
    assert "measured by nothing" not in merged.input_row()["name"]


def test_a_cut_carrying_no_polygon_refuses_rather_than_painting_the_water(
        tmp_path) -> None:
    """There is no inside for the terrain to stay out of, and merging as though
    the cut said nothing would paint the water with the surface over it."""
    with pytest.raises(MergeRastersError) as excinfo:
        merged_surface(primary=_survey(), fallback=_terrain(), frame="NAVD88",
                       water={"type": "LineString",
                              "coordinates": [[-122.0, 36.0], [-122.0, 36.1]]},
                       _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_NO_SOURCE"


def test_the_stated_op_paints_the_water_from_the_shore_to_the_measurements(
        tmp_path) -> None:
    """The op the run states lays one more rung, ranked under every measured
    one: the water the ladder left is interpolated between the soundings and the
    polygon's own edge, which is seeded at depth zero because the bed meets the
    water surface where the water ends."""
    merged = merged_surface(
        primary=_survey(), fallback=_terrain(), frame="NAVD88",
        water=_cut(500_004.0, 3_999_992.0, 500_012.0, 4_000_000.0),
        fill="interpolated", _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface:
        values = surface.read(1)
    # The cut is columns 4-11 over rows 0-7 of the 1 m grid, the survey its
    # top-left 4 x 4: every cell of it carries a bed now.
    water = values[0:8, 4:12]
    assert not np.isnan(water).any()
    assert float(values[0, 4]) == pytest.approx(-5.0)
    # Between the sounded bed and the shore it was painted from, and nowhere
    # near the terrain standing over the water.
    painted = water[~np.isclose(water, -5.0)]
    assert float(painted.min()) >= -5.0 and float(painted.max()) <= 0.0
    # The shore is the edge of the cut, seeded at the water surface itself.
    assert float(values[7, 11]) == pytest.approx(0.0)
    # Outside the cut the terrain is the bed, as it is with no op at all.
    assert float(values[0, 12]) == pytest.approx(10.0)


def test_the_interpolated_rung_is_ranked_last_and_says_so_per_cell(
        tmp_path) -> None:
    """A node painted by an interpolation is never read as one somebody
    sounded: the fill is its own rung on the result, under every measured one,
    and the provenance sidecar carries it cell by cell."""
    from trid3nt_server.workflows.runtime.journal import bind_notes, drain_notes

    token = bind_notes()
    merged = merged_surface(
        primary=_survey(), fallback=_terrain(), frame="NAVD88",
        water=_cut(500_004.0, 3_999_992.0, 500_012.0, 4_000_000.0),
        fill="interpolated", _output_dir=str(tmp_path))
    said = drain_notes(token)
    label, share = merged.rungs[-1]
    assert label == "interpolated"
    # The 48 cells of the cut no rung measured, over the whole 16 x 16 grid.
    assert share == pytest.approx(48.0 / 256.0)
    assert "interpolated" in merged.input_row()["name"]
    with rasterio.open(merged.provenance_uri) as sidecar:
        won = sidecar.read(1)
    assert int(won[7, 11]) == len(merged.rungs) - 1
    assert int(won[0, 4]) == 0
    # The share is what MEASURED the water, so the fill never moves it: an
    # interpolation is not a sounding, and the reader weighs the same number.
    assert merged.unmeasured_water_fraction == pytest.approx(0.75)
    assert merged.fallback_fraction == pytest.approx(
        [share for label, share in merged.rungs if label != "interpolated"][-1])
    stated = [line for line in said if "measured by nothing" in line]
    assert "painted here by the interpolated rung" in stated[0]


def test_an_op_the_merge_does_not_know_refuses_before_it_reads_a_surface(
        tmp_path) -> None:
    from trid3nt_server.inputs.user_input import UserInputError

    with pytest.raises(UserInputError) as excinfo:
        merged_surface(primary=_survey(), fallback=_terrain(), frame="NAVD88",
                       fill="smoothed", _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_BED_OP_INVALID"
