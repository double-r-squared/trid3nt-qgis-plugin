"""A centerline's chainage 0 is UPSTREAM, at the seed the flowline was navigated down from,
whatever order the source vertices arrived in - what a profile along it reads distance from."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from trid3nt_server.tools.mesh.shared.nodes import read_centerline_utm

#: A west-to-east flowline near Twin Falls, Idaho. The seed is its WEST end: the
#: navigate was walked downstream from there, so that is chainage 0.
_SEED = (-114.34, 42.58)
_COORDS = [[-114.34, 42.58], [-114.33, 42.58], [-114.31, 42.58],
           [-114.29, 42.58], [-114.28, 42.58]]
_UTM_EPSG = 32611


def _line(coords) -> str:
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {
            "type": "LineString", "coordinates": coords}}]})


#: A mesh that holds every station on the line. The subject here is WHICH END
#: the fraction counts from, so the containment the derived walk also does is
#: stood in for by a triangulation that refuses nothing - otherwise a failure
#: could not say which of the two it was about.
_HOLDS_EVERYTHING = (
    np.array([[-1.0e7, -1.0e7], [1.0e7, -1.0e7], [1.0e7, 1.0e7], [-1.0e7, 1.0e7]]),
    np.array([[0, 1, 2], [0, 2, 3]]), None, None)


def _nodes_along(centerline_utm) -> np.ndarray:
    """Nodes every ten metres down the line: a release lands on the NODE the
    engine solves it at, so a stand-in mesh whose nodes are elsewhere would move
    it there and this test would be about the move."""
    line = np.asarray(centerline_utm, dtype=float)
    out = [line[0]]
    for start, end in zip(line[:-1], line[1:]):
        steps = max(2, int(np.hypot(*(end - start)) / 10.0))
        out += [start + (end - start) * (i / steps) for i in range(1, steps + 1)]
    return np.asarray(out, dtype=float)


def _walk(fraction: float, centerline_utm, monkeypatch):
    """Where an unplaced release lands -> ``(lon, lat)``."""
    from trid3nt_server.tools.mesh.shared import nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: _HOLDS_EVERYTHING)
    mesh = {"display_uri": "s3://m/M/mesh.2dm",
            "artifact": type("A", (), {"utm_epsg": _UTM_EPSG})()}
    placed, note = asyncio.run(_settle_release(
        None, mesh=mesh, centerline=None, centerline_utm=centerline_utm,
        utm_epsg=_UTM_EPSG, fraction=fraction,
        node_xy=_nodes_along(centerline_utm), label="Release point",
        initial_state={"wet": [True] * len(_nodes_along(centerline_utm)),
                       "note": "a wet stand-in state"}))
    # a derived release inside the mesh lands on the wet node nearest the
    # station it was walked to, which is metres away on a mesh this fine
    assert "holds water at t0" in note
    return placed.lon, placed.lat


@pytest.mark.parametrize("coords", [_COORDS, list(reversed(_COORDS))])
def test_the_normalized_centerline_starts_at_the_seed(coords):
    """The seed decides the head; the document's vertex order does not.

    A flowline arrives as rows whose order says nothing, and reading it
    head-to-tail off the merge alone makes chainage 0 a coin flip.
    """
    line = read_centerline_utm(_line(coords), _UTM_EPSG, start_lonlat=_SEED)
    head_first = read_centerline_utm(_line(_COORDS), _UTM_EPSG,
                                     start_lonlat=_SEED)
    assert np.allclose(line, head_first)
    # the seed end is first: x increases eastward in this UTM zone
    assert line[0][0] < line[-1][0]
