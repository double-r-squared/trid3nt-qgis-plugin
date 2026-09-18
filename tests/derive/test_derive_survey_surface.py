"""``derive_survey_surface``: the surface between scattered measurements.

IDW in the points' own UTM zone, holding each measurement at its own position and
leaving every cell outside the soundings' FOOTPRINT as nodata - a footprint the
output grid never widens. Covered with that, the refusals: no points, no decidable
value field, a cell size that is not one, a grid past the cell ceiling, and a cell
so coarse that the footprint holds no cell centre."""

from __future__ import annotations

import math

import numpy as np
import pytest
import rasterio

from trid3nt_server.tools.derive.derive_survey_surface.derive_survey_surface import (
    SurveySurfaceError,
    derive_survey_surface,
)

#: A patch of the Willamette at Portland, so the zone the grid is built in is real.
_LON, _LAT = -122.673, 45.517


def _plane(n: int = 12, step_deg: float = 0.0004, extra: dict | None = None) -> dict:
    """A grid of soundings whose depth is a plane in the north direction."""
    features = []
    for row in range(n):
        for col in range(n):
            lon = _LON + col * step_deg
            lat = _LAT + row * step_deg
            properties = {"depth_m": 5.0 + row}
            features.append({"type": "Feature", "properties": {**properties, **(extra or {})},
                             "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return {"type": "FeatureCollection", "features": features}


def _read(uri: str) -> tuple[np.ndarray, rasterio.Affine, str]:
    with rasterio.open(uri) as src:
        return src.read(1), src.transform, str(src.crs)


def test_the_surface_holds_each_measurement_at_its_own_position(tmp_path):
    doc = _plane()
    layer = derive_survey_surface(
        points=doc, resolution_m=5.0, _output_dir=str(tmp_path))
    assert layer.value_field == "depth_m"
    assert layer.n_points == 144
    band, _transform, crs = _read(layer.uri)
    assert crs == "EPSG:32610"
    with rasterio.open(layer.uri) as src:
        from rasterio.warp import transform as warp

        lons = [f["geometry"]["coordinates"][0] for f in doc["features"]]
        lats = [f["geometry"]["coordinates"][1] for f in doc["features"]]
        xs, ys = warp("EPSG:4326", src.crs, lons, lats)
        read_back = np.array([v[0] for v in src.sample(list(zip(xs, ys)))])
    measured = np.array([f["properties"]["depth_m"] for f in doc["features"]])
    assert np.nanmax(np.abs(read_back - measured)) < 0.5
    assert layer.value_min == 5.0 and layer.value_max == 16.0


def test_a_cell_with_no_measurement_near_it_is_nodata_not_filled(tmp_path):
    layer = derive_survey_surface(
        points=_plane(), resolution_m=5.0, max_distance_m=10.0,
        _output_dir=str(tmp_path))
    band, _transform, _crs = _read(layer.uri)
    assert np.isnan(band).any(), "a tight search radius must leave gaps unfilled"
    assert 0.0 < layer.filled_fraction < 1.0
    assert layer.search_radius_m == 10.0


def test_the_search_radius_defaults_to_the_survey_own_spacing(tmp_path):
    layer = derive_survey_surface(
        points=_plane(step_deg=0.0004), resolution_m=10.0, _output_dir=str(tmp_path))
    # 0.0004 deg of longitude at this latitude is about 31 m, which is the nearest
    # neighbour on a square degree grid; the default reach is three of those.
    assert layer.search_radius_m == pytest.approx(3.0 * 31.2, rel=0.1)
    assert "Median point spacing" in " ".join(layer.notes)


def test_a_coarse_output_grid_never_widens_the_measurements_reach(tmp_path):
    """The reach is the survey's own: asking for a coarser cell must not let the
    surface claim ground farther from a sounding than a fine cell would."""
    fine = derive_survey_surface(points=_plane(), resolution_m=5.0,
                                 _output_dir=str(tmp_path))
    coarse = derive_survey_surface(points=_plane(), resolution_m=150.0,
                                   _output_dir=str(tmp_path))
    assert coarse.search_radius_m == fine.search_radius_m


def test_the_note_states_the_footprint_the_soundings_measured(tmp_path):
    layer = derive_survey_surface(points=_plane(), resolution_m=10.0,
                                  _output_dir=str(tmp_path))
    note = " ".join(layer.notes)
    assert "FOOTPRINT" in note and "km2" in note
    band, _transform, _crs = _read(layer.uri)
    assert layer.filled_fraction == pytest.approx(
        float(np.isfinite(band).mean()), abs=1e-4)


def _ring(count: int = 72, radius_m: float = 50.0) -> dict:
    """Soundings around a circle, so the grid's own corners stand off the survey."""
    dlon = radius_m / (111_320.0 * math.cos(math.radians(_LAT)))
    dlat = radius_m / 111_132.0
    features = []
    for step in range(count):
        angle = 2.0 * math.pi * step / count
        features.append({"type": "Feature", "properties": {"depth_m": 4.0},
                         "geometry": {"type": "Point", "coordinates": [
                             _LON + dlon * math.cos(angle),
                             _LAT + dlat * math.sin(angle)]}})
    return {"type": "FeatureCollection", "features": features}


def test_a_cell_coarser_than_the_surveys_reach_refuses_rather_than_reaching(tmp_path):
    with pytest.raises(SurveySurfaceError) as excinfo:
        derive_survey_surface(points=_ring(), resolution_m=200.0,
                              _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SURVEY_SURFACE_FOOTPRINT_EMPTY"
    assert "max_distance_m" in str(excinfo.value)


def test_several_numeric_fields_are_refused_rather_than_guessed_between(tmp_path):
    with pytest.raises(SurveySurfaceError) as excinfo:
        derive_survey_surface(points=_plane(extra={"count": 3}),
                                     resolution_m=5.0, _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SURVEY_SURFACE_NO_VALUE_FIELD"
    assert "value_field" in str(excinfo.value)


def test_naming_the_field_resolves_that(tmp_path):
    layer = derive_survey_surface(
        points=_plane(extra={"count": 3}), resolution_m=10.0, value_field="depth_m",
        _output_dir=str(tmp_path))
    assert layer.value_field == "depth_m"


def test_a_field_no_point_carries_a_number_under_refuses_naming_the_fields(tmp_path):
    with pytest.raises(SurveySurfaceError) as excinfo:
        derive_survey_surface(points=_plane(), resolution_m=5.0,
                                     value_field="elevation_m", _output_dir=str(tmp_path))
    assert "depth_m" in str(excinfo.value)


def test_a_layer_with_no_point_geometry_refuses_by_name(tmp_path):
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"depth_m": 1.0},
         "geometry": {"type": "LineString", "coordinates": [[_LON, _LAT], [_LON, _LAT + 0.01]]}}]}
    with pytest.raises(SurveySurfaceError) as excinfo:
        derive_survey_surface(points=doc, resolution_m=5.0, _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SURVEY_SURFACE_NO_POINTS"


def test_a_cell_size_that_is_not_one_refuses_by_name(tmp_path):
    for bad in (0.0, -5.0, "fine"):
        with pytest.raises(SurveySurfaceError) as excinfo:
            derive_survey_surface(points=_plane(), resolution_m=bad,
                                         _output_dir=str(tmp_path))
        assert excinfo.value.error_code == "SURVEY_SURFACE_RESOLUTION_INVALID"


def test_a_grid_past_the_cell_ceiling_refuses_naming_the_cell(tmp_path):
    with pytest.raises(SurveySurfaceError) as excinfo:
        derive_survey_surface(points=_plane(step_deg=0.05), resolution_m=0.05,
                                     _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SURVEY_SURFACE_RESOLUTION_INVALID"
    assert "coarser" in str(excinfo.value)


def test_a_point_layer_carrying_other_geometry_beside_it_still_interpolates(tmp_path):
    doc = _plane()
    doc["features"].append(
        {"type": "Feature", "properties": {"depth_m": None},
         "geometry": {"type": "Polygon", "coordinates": [[
             [_LON, _LAT], [_LON + 0.01, _LAT], [_LON + 0.01, _LAT + 0.01], [_LON, _LAT]]]}})
    layer = derive_survey_surface(points=doc, resolution_m=10.0,
                                         _output_dir=str(tmp_path))
    assert layer.n_points == 144


def test_the_derive_surfaces_from_its_own_corpus_phrasings():
    from pathlib import Path

    import yaml

    import trid3nt_server.tools.derive.derive_survey_surface as package
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(package.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text())
               or {})["derive_survey_surface"]
    assert queries
    assert any("derive_survey_surface" in retrieve_visible_tools(q, None, 8)
               for q in queries), (
        "derive_survey_surface surfaces in NO top-8 for any of its corpus queries")
