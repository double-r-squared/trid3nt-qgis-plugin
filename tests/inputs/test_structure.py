"""Unit tests for the STRUCTURE typed input.

Covered: a drawn centreline widened to a footprint whose width is the declared
one, several lines each widened, a structure already mapped as a polygon used
verbatim, a missing structure refusing, a line with no width refusing, and the
footprint coming back in the 4326 a mesh recipe speaks."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.structure import StructureError, structure


def _line(*coords: list[float]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString", "coordinates": list(coords)}}]}


def test_a_centreline_is_widened_to_its_declared_width() -> None:
    import geopandas as gpd
    from shapely.geometry import LineString

    drawn = [[-122.70, 45.50], [-122.60, 45.50]]
    doc = structure(_line(*drawn), width_m=40.0)
    frame = gpd.GeoDataFrame.from_features(doc["features"], crs=4326)
    metric = frame.estimate_utm_crs()
    length = gpd.GeoSeries([LineString(drawn)], crs=4326).to_crs(metric).length[0]
    # Area over length IS the width; the bounding box of a buffered line is not,
    # because grid convergence tilts an east-west line in its own UTM zone.
    assert float(frame.to_crs(metric).area[0]) / length == pytest.approx(
        40.0, abs=1.0)


def test_two_lines_are_both_widened() -> None:
    doc = structure({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "MultiLineString", "coordinates": [
             [[-122.70, 45.50], [-122.68, 45.50]],
             [[-122.66, 45.50], [-122.64, 45.50]]]}}]}, width_m=30.0)
    geometry = doc["features"][0]["geometry"]
    assert geometry["type"] == "MultiPolygon"


def test_a_mapped_polygon_is_its_own_footprint() -> None:
    ring = [[-122.70, 45.50], [-122.69, 45.50], [-122.69, 45.51], [-122.70, 45.50]]
    doc = structure({"type": "Polygon", "coordinates": [ring]}, width_m=0)
    assert doc["features"][0]["geometry"]["coordinates"] == [ring]


def test_no_structure_at_all_refuses_and_says_what_to_hand_it() -> None:
    with pytest.raises(StructureError) as caught:
        structure(None, width_m=40.0, asked="the breakwater",
                  code="ARTEMIS_STRUCTURE_INVALID")
    assert caught.value.error_code == "ARTEMIS_STRUCTURE_INVALID"
    assert "the breakwater" in str(caught.value)


@pytest.mark.parametrize("width", [0, -5.0, None, "wide"])
def test_a_line_with_no_usable_width_refuses(width: object) -> None:
    with pytest.raises(StructureError):
        structure(_line([-122.70, 45.50], [-122.60, 45.50]), width_m=width)


def test_the_footprint_comes_back_in_lon_lat() -> None:
    doc = structure(_line([-122.70, 45.50], [-122.60, 45.50]), width_m=40.0)
    coords = doc["features"][0]["geometry"]["coordinates"][0]
    assert all(-123.0 < float(x) < -122.0 and 45.0 < float(y) < 46.0
               for x, y in coords)
