"""The two generic geometry composition links: ``combine`` and ``endpoints``.

Offline: every source is a file this module writes. What is checked is what a
CHAIN depends on - that one document comes back holding exactly what went in,
that the two ends are vertices of the supplied line, that the mesher reads the
layer either tool returns without being handed its uri, and that every refusal is
typed and names what to supply instead.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.processing.combine.combine import CombineError, combine
from trid3nt_server.tools.processing.endpoints.endpoints import (
    EndpointsError,
    endpoints,
)

_POLYGON = {"type": "Polygon", "coordinates": [
    [[-83.5, 35.0], [-83.4, 35.0], [-83.4, 35.1], [-83.5, 35.1], [-83.5, 35.0]]]}
_LINE_A = {"type": "LineString", "coordinates": [[-83.49, 35.01], [-83.45, 35.05]]}
_LINE_B = {"type": "LineString", "coordinates": [[-83.45, 35.05], [-83.41, 35.09]]}
_DETACHED = {"type": "LineString", "coordinates": [[-83.42, 35.02], [-83.43, 35.03]]}


def _write(tmp_path, name, *geometries):
    path = tmp_path / name
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": g} for g in geometries]}))
    return str(path)


# --- combine ---------------------------------------------------------------- #
def test_combine_holds_exactly_what_went_in(tmp_path):
    """Nothing is dissolved, clipped or invented: the counts are the inputs'."""
    cut = combine(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                  lines=_write(tmp_path, "lines.geojson", _LINE_A, _LINE_B),
                  _output_dir=str(tmp_path))
    assert (cut.polygon_count, cut.line_count, cut.point_count) == (1, 2, 0)
    assert cut.source_count == 2
    assert cut.crs_authid == "EPSG:4326"
    doc = json.loads((tmp_path / cut.uri.rsplit("/", 1)[-1]).read_text())
    assert [f["geometry"]["type"] for f in doc["features"]] == [
        "Polygon", "LineString", "LineString"]


def test_combine_takes_a_list_of_line_layers(tmp_path):
    cut = combine(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                  lines=[_write(tmp_path, "a.geojson", _LINE_A),
                         _write(tmp_path, "b.geojson", _LINE_B)],
                  _output_dir=str(tmp_path))
    assert (cut.line_count, cut.source_count) == (2, 3)


def test_combine_without_lines_is_the_polygon_alone(tmp_path):
    cut = combine(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                  _output_dir=str(tmp_path))
    assert (cut.polygon_count, cut.line_count, cut.source_count) == (1, 0, 1)


def test_combine_refuses_an_empty_source_by_name(tmp_path):
    empty = tmp_path / "empty.geojson"
    empty.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(CombineError) as ei:
        combine(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                lines=str(empty), _output_dir=str(tmp_path))
    assert ei.value.error_code == "COMBINE_NO_GEOMETRY"
    assert "lines" in str(ei.value)


def test_combine_refuses_an_unreadable_source(tmp_path):
    with pytest.raises(CombineError) as ei:
        combine(polygon=str(tmp_path / "nothing-here.geojson"),
                _output_dir=str(tmp_path))
    assert ei.value.error_code == "COMBINE_SOURCE_UNREADABLE"


# --- endpoints -------------------------------------------------------------- #
def test_endpoints_are_vertices_of_the_supplied_line(tmp_path):
    """Joined parts, ends in vertex order, and the pair ``section`` takes."""
    ends = endpoints(line=_write(tmp_path, "line.geojson", _LINE_A, _LINE_B),
                     _output_dir=str(tmp_path))
    assert ends.start == (-83.49, 35.01)
    assert ends.end == (-83.41, 35.09)
    assert ends.between == [[-83.49, 35.01], [-83.41, 35.09]]
    assert ends.part_count == 2
    assert ends.length_m > 0.0
    doc = json.loads((tmp_path / ends.uri.rsplit("/", 1)[-1]).read_text())
    assert [f["properties"]["position"] for f in doc["features"]] == ["start", "end"]


def test_endpoints_refuses_parts_that_do_not_join(tmp_path):
    with pytest.raises(EndpointsError) as ei:
        endpoints(line=_write(tmp_path, "split.geojson", _LINE_A, _DETACHED),
                  _output_dir=str(tmp_path))
    assert ei.value.error_code == "ENDPOINTS_NOT_CONTINUOUS"


def test_endpoints_refuses_a_source_with_no_line(tmp_path):
    with pytest.raises(EndpointsError) as ei:
        endpoints(line=_write(tmp_path, "poly.geojson", _POLYGON),
                  _output_dir=str(tmp_path))
    assert ei.value.error_code == "ENDPOINTS_NO_LINE"


# --- what a chain needs of them --------------------------------------------- #
def test_the_endpoints_pair_cuts_a_section(tmp_path):
    """``section(between=<endpoints pair>)`` is the reach chain's last link."""
    from trid3nt_server.tools.processing.section.section import section

    ends = endpoints(line=_write(tmp_path, "line.geojson", _LINE_A, _LINE_B),
                     _output_dir=str(tmp_path))
    cut = section(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                  between=ends.between, _output_dir=str(tmp_path))
    assert cut.area_km2 > 0.0
    assert cut.length_m > 0.0


@pytest.mark.parametrize("as_", ["layer", "uri", "doc"])
def test_the_mesher_reads_a_combined_layer_however_the_chain_hands_it_over(
        tmp_path, as_):
    """``extent=Ref("sized")`` binds the LAYER, not the uri string it carries."""
    from trid3nt_server.workflows.mesh.meshers.om2d import (
        _split_geometry,
        read_geometry,
    )

    cut = combine(polygon=_write(tmp_path, "poly.geojson", _POLYGON),
                  lines=_write(tmp_path, "lines.geojson", _LINE_A, _LINE_B),
                  _output_dir=str(tmp_path))
    source = {"layer": cut, "uri": cut.uri,
              "doc": json.loads((tmp_path / cut.uri.rsplit("/", 1)[-1]).read_text())}[as_]
    polygons, lines = _split_geometry(read_geometry(source))
    assert len(polygons) == 1
    assert len(lines) == 4
