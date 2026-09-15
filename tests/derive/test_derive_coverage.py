"""Unit tests for ``derive_coverage``.

Covered: the fraction of a line inside polygons measured in metres, a line
wholly inside, a line with no polygon under it refusing rather than reading
zero, a source with no line and a source with no polygon, and the registration."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_coverage.derive_coverage import (
    CoverageError,
    derive_coverage,
)


def _write(doc: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".geojson", prefix="trid3nt_coverage_")
    with os.fdopen(fd, "w") as handle:
        json.dump(doc, handle)
    return path


def _line(coords: list[list[float]]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString", "coordinates": coords}}]}


def _box(west: float, south: float, east: float, north: float) -> dict:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def test_registered_and_not_cacheable() -> None:
    assert "derive_coverage" in TOOL_REGISTRY
    assert TOOL_REGISTRY["derive_coverage"].metadata.cacheable is False


def test_half_the_line_is_covered() -> None:
    line = _write(_line([[-122.70, 45.50], [-122.60, 45.50]]))
    water = _write(_box(-122.70, 45.49, -122.65, 45.51))
    out = derive_coverage(line=line, within=water)
    assert out["fraction"] == pytest.approx(0.5, abs=0.02)
    assert out["of"] == "polygons"
    assert out["covered_m"] < out["length_m"]
    assert out["epsg"] == 32610


def test_whole_line_is_covered() -> None:
    line = _write(_line([[-122.70, 45.50], [-122.60, 45.50]]))
    water = _write(_box(-122.80, 45.40, -122.50, 45.60))
    assert derive_coverage(line=line, within=water)["fraction"] == pytest.approx(1.0)


def test_a_disjoint_area_refuses_rather_than_reading_zero() -> None:
    line = _write(_line([[-122.70, 45.50], [-122.60, 45.50]]))
    elsewhere = _write(_box(-100.0, 30.0, -99.0, 31.0))
    with pytest.raises(CoverageError) as caught:
        derive_coverage(line=line, within=elsewhere)
    assert caught.value.error_code == "DERIVE_COVERAGE_DISJOINT"


def test_a_source_with_no_line_refuses() -> None:
    water = _write(_box(-122.70, 45.49, -122.65, 45.51))
    with pytest.raises(CoverageError) as caught:
        derive_coverage(line=water, within=water)
    assert caught.value.error_code == "DERIVE_COVERAGE_NO_LINE"


def test_a_source_with_no_polygon_refuses() -> None:
    line = _write(_line([[-122.70, 45.50], [-122.60, 45.50]]))
    with pytest.raises(CoverageError) as caught:
        derive_coverage(line=line, within=line)
    assert caught.value.error_code == "DERIVE_COVERAGE_NO_AREA"


def test_a_mesh_face_with_no_zone_refuses_rather_than_guessing() -> None:
    line = _write(_line([[-122.70, 45.50], [-122.60, 45.50]]))
    fd, face = tempfile.mkstemp(suffix=".2dm", prefix="trid3nt_coverage_")
    os.close(fd)
    with pytest.raises(CoverageError) as caught:
        derive_coverage(line=line, within=face)
    assert caught.value.error_code == "DERIVE_COVERAGE_UNPROJECTED"
