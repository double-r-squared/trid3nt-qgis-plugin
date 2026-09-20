"""The bed slot's MERGE: a measurement over part of the ground, a wider surface
under the rest.

Covered: the primary winning every cell it measured and the fallback filling the
rest, the merged grid taking the finer cell over the union of both, the sidecar
naming which input painted each cell, an absent row passing the other surface
through, two different vertical datums refusing by name, an unstated datum
refusing, a STATED OFFSET bringing two differing datums onto one axis while an
offset between two other frames still refuses, a primary of DEPTHS read as
elevations on the fallback's zero through the shift the survey publishes about
itself, two disjoint surfaces, a measurement over half a domain landing survey and
terrain in the right halves even though it is read onto a finer grid, and a corner
no sounding stood in coming back terrain."""

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


def test_the_derive_surfaces_from_its_own_corpus_phrasings() -> None:
    from pathlib import Path

    import yaml

    import trid3nt_server.tools.derive.derive_merge_rasters as package
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(package.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text())
               or {})["derive_merge_rasters"]
    assert queries
    assert any("derive_merge_rasters" in retrieve_visible_tools(q, None, 8)
               for q in queries), (
        "derive_merge_rasters surfaces in NO top-8 for any of its corpus queries")


def test_a_stated_offset_reads_the_primary_on_the_fallback_s_datum(tmp_path) -> None:
    merged = merged_surface(
        primary=_survey("CRD"), fallback=_terrain("NAVD88"),
        offset={"offset_m": 1.6093, "from_frame": "CRD", "to_frame": "NAVD88",
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
            offset={"offset_m": -1.131, "from_frame": "NAVD88",
                    "to_frame": "EGM2008", "source": "NOAA VDatum"},
            _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUM_OFFSET_MISMATCH"


def test_one_datum_needs_no_offset_and_shifts_nothing(tmp_path) -> None:
    merged = merged_surface(primary=_survey(), fallback=_terrain(),
                                  offset=None, _output_dir=str(tmp_path))
    assert merged.datum_shift_m == 0.0
    assert "Both surfaces count from NAVD88." in merged.notes


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
    assert "counted DEPTHS below CRD" in merged.notes[-1]


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
