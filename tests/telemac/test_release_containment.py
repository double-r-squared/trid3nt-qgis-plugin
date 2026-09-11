"""Offline tests for the DERIVED source: a station along the reach, inside the
accepted mesh, and the mesh's own record of the domain a supplied point is
tested against."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.authoring.assembler import (
    _domain_polygon,
    _station_on_mesh,
)
from trid3nt_server.workflows.telemac.errors import TelemacError


class _Artifact:
    """The one thing the pre-flight reads off an accepted mesh."""

    def __init__(self, extent):
        self.provenance = {"recipe": {"mesher": "om2d", "extent": extent}}


def test_the_domain_read_is_the_mesh_own_record_of_what_it_was_cut_from():
    assert _domain_polygon(_Artifact("s3://cache/section/reach.geojson")) == (
        "s3://cache/section/reach.geojson")


@pytest.mark.parametrize("art", [
    _Artifact([-114.33, 42.57, -114.29, 42.59]),  # a box, not a shape
    _Artifact(None),                              # a mesh that states no extent
    None,                                         # no accepted mesh at all
])
def test_a_mesh_with_no_domain_polygon_refuses_rather_than_waving_the_point_through(art):
    """There is ONE containment path and it always has a polygon.

    Four numbers are not a shape a point can be inside of, so answering "no domain"
    would let a supplied point ride into the run untested."""
    with pytest.raises(TelemacError) as excinfo:
        _domain_polygon(art)
    assert "no mapped shape" in str(excinfo.value)


def _mesh_holding(x_from: float, x_to: float, monkeypatch):
    """Stand in a mesh whose cells cover only ``x_from..x_to`` of the centerline."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod

    monkeypatch.setattr(
        nodes_mod, "read_accepted_mesh_nodes",
        lambda _uri, utm_epsg=None: (
            np.array([[x_from, -50.0], [x_to, -50.0], [x_to, 50.0], [x_from, 50.0]]),
            np.array([[0, 1, 2], [0, 2, 3]]), None, None))
    return {"display_uri": "s3://m/M/mesh.2dm",
            "artifact": type("A", (), {"utm_epsg": 32611})()}


def test_a_derived_release_inside_the_mesh_is_left_where_it_was(monkeypatch):
    mesh = _mesh_holding(0.0, 1000.0, monkeypatch)
    (_lon, _lat), note = _station_on_mesh(
        centerline_utm=[[0.0, 0.0], [1000.0, 0.0]], mesh=mesh, fraction=0.25)
    assert note is None


def test_a_derived_release_above_the_meshed_stretch_walks_downstream(monkeypatch):
    """The centerline runs on past what the mapped banks left; the station has to
    be inside the triangulation or the solver stops with the source outside the
    domain."""
    mesh = _mesh_holding(400.0, 1000.0, monkeypatch)
    _lonlat, note = _station_on_mesh(
        centerline_utm=[[0.0, 0.0], [1000.0, 0.0]], mesh=mesh, fraction=0.02)
    assert note is not None and "downstream" in note
    walked = float(note.split("moved ")[1].split(" m")[0])
    assert 370.0 <= walked <= 400.0     # 20 m in, then the first meshed station


def test_a_centerline_the_mesh_never_holds_refuses(monkeypatch):
    mesh = _mesh_holding(5000.0, 6000.0, monkeypatch)
    with pytest.raises(TelemacError):
        _station_on_mesh(centerline_utm=[[0.0, 0.0], [1000.0, 0.0]],
                               mesh=mesh, fraction=0.0)
