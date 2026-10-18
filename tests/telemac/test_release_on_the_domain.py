"""WHERE a release enters the water: the stated point, held where it was stated.

The point is checked, never moved - inside the domain, and the node the engine reads
it at holding water at t0 off the rim. A miss refuses with the one shared code and
the nearest node it would not miss. Offline: no solve, no emitter.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from trid3nt_server.inputs.domain import domain as ingest
from trid3nt_server.inputs.point import Point, PointNotViableError
from trid3nt_server.workflows.telemac.authoring import release_point as D

_POND = {"type": "Polygon", "coordinates": [[
    [-122.70, 45.50], [-122.60, 45.50], [-122.60, 45.56], [-122.70, 45.56],
    [-122.70, 45.50]]]}
_REACH = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"part": "reach"}, "geometry": _POND},
    {"type": "Feature", "properties": {"part": "centerline"},
     "geometry": {"type": "LineString",
                  "coordinates": [[-122.695, 45.53], [-122.605, 45.53]]}}]}

#: A mesh whose cells cover the whole pond, in its own UTM metres, at a hundred
#: metres: a release lands on the NODE the engine solves it at, so the stand-in
#: has to carry nodes where a point is placed rather than four far corners.
_XY = np.array([[x, y]
                for x in np.arange(523400.0, 531300.0, 100.0)
                for y in np.arange(5038500.0, 5045300.0, 100.0)])


class _Artifact:
    utm_epsg = 32610

    def __init__(self, extent):
        self.provenance = {"recipe": {"mesher": "om2d", "extent": extent}}


def _mesh(extent):
    return {"artifact": _Artifact(extent), "display_uri": "s3://m/M/mesh.2dm",
            "mesh_id": "mesh-1"}


#: The cells over those nodes: a release lands on an INTERIOR node, so the
#: stand-in has to have an inside. One strip of triangles across the grid's
#: rows leaves the rim as its boundary.
def _cells() -> np.ndarray:
    rows = int(round((5045300.0 - 5038500.0) / 100.0))
    out = []
    for i in range(len(_XY) // rows - 1):
        for j in range(rows - 1):
            a = i * rows + j
            out += [[a, a + rows, a + 1], [a + 1, a + rows, a + rows + 1]]
    return np.asarray(out, dtype=np.int64)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(D, "accepted_mesh_nodes",
                        lambda mesh: (_XY, _cells(), None, None))
    monkeypatch.setattr(D, "initial_state_of", lambda continue_from, count: {
        "wet": np.ones(len(_XY), dtype=bool), "note": "dry start", "start_s": 0.0})
    monkeypatch.setattr(D, "journal_note", lambda note: None)

    async def no_layer(*args, **kwargs):
        return False

    monkeypatch.setattr(D, "publish_point", no_layer)


def _settle(point, dom, **over):
    return asyncio.run(D.settle_release(
        point=point, mesh=_mesh(json.dumps(_POND)), label="Release point",
        domain=dom, **over))


def test_a_stated_point_is_held_exactly_where_it_was_stated():
    placed = _settle(Point(-122.65, 45.53), ingest(_POND))
    assert (placed["lon"], placed["lat"]) == (-122.65, 45.53)
    assert "the engine reads it at mesh node" in placed["note"]


def test_a_point_outside_the_domain_refuses_with_the_nearest_viable_hint():
    with pytest.raises(PointNotViableError) as caught:
        _settle(Point(-123.20, 45.53), ingest(_POND))
    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert "outside the domain" in str(caught.value)
    assert caught.value.hint is not None and caught.value.hint["distance_m"] > 0


def test_a_point_whose_node_is_dry_refuses_rather_than_moving_to_water(monkeypatch):
    dry = np.ones(len(_XY), dtype=bool)
    here = D.as_utm(Point(-122.65, 45.53), 32610)
    near = int(np.argmin(np.hypot(_XY[:, 0] - here[0], _XY[:, 1] - here[1])))
    dry[near] = False
    monkeypatch.setattr(D, "initial_state_of", lambda continue_from, count: {
        "wet": dry, "note": "a stand-in state", "start_s": 0.0})
    with pytest.raises(PointNotViableError) as caught:
        _settle(Point(-122.65, 45.53), ingest(_POND))
    assert "holds no water at t0" in str(caught.value)
    assert caught.value.hint["distance_m"] > 0


def test_a_bank_click_on_a_reach_is_not_pulled_onto_the_channel():
    placed = _settle(Point(-122.65, 45.555), ingest(_REACH))
    assert placed["lat"] == 45.555
