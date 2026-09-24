"""WHERE a release enters the water, on a domain that may have no channel at all.

The reach chain is gone from this step: what it needs is the DOMAIN, and the
centerline it places an unplaced point along is a companion the domain's own
producer measured. A drawn pond carries none, so a point is required there and
the refusal says so. Offline: no solve, no emitter.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from trid3nt_server.inputs.domain import domain as ingest
from trid3nt_server.inputs.point import Point, PointOutsideDomainError
from trid3nt_server.workflows.telemac.authoring import release_point as D
from trid3nt_server.workflows.telemac.errors import TelemacError

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


def test_a_point_the_user_placed_in_a_pond_is_held_where_they_placed_it():
    """A pond has no channel to pull a point onto, so holding it inside the
    domain is the whole of what containment can honestly do."""
    placed = _settle(Point(-122.65, 45.53), ingest(_POND))
    assert placed["user_supplied"] is True
    # the nearest node of the stand-in mesh, which is within its own spacing
    assert placed["lon"] == pytest.approx(-122.65, abs=2e-3)
    assert "inside the modeled domain" in placed["note"]


def test_a_point_outside_the_domain_refuses_rather_than_being_moved_inside():
    with pytest.raises(PointOutsideDomainError):
        _settle(Point(-123.20, 45.53), ingest(_POND))


def test_an_unplaced_point_on_a_domain_with_no_centerline_refuses_by_name():
    """Where the substance enters the water is not something a closed body can
    derive, and inventing a place would be this step choosing the question."""
    with pytest.raises(TelemacError) as exc:
        _settle(None, ingest(_POND))
    assert exc.value.error_code == "TELEMAC_RELEASE_UNPLACED"


def test_an_unplaced_point_rides_the_domains_own_centerline_companion(monkeypatch):
    """The producer measured the line; the step reads it as ``domain.centerline``
    rather than being handed the whole artifact."""
    seen = {}

    def station(*, centerline_utm, mesh, fraction):
        seen["length"] = float(np.hypot(*(np.asarray(centerline_utm)[-1]
                                          - np.asarray(centerline_utm)[0])))
        seen["fraction"] = fraction
        return (-122.65, 45.53), None

    monkeypatch.setattr(D, "_station_on_mesh", station)
    placed = _settle(None, ingest(_REACH), fraction=0.25)
    assert placed["user_supplied"] is False
    assert seen["fraction"] == 0.25
    # The companion is the LINE, read in the mesh's own metres.
    assert seen["length"] > 6000.0


def test_a_placed_point_is_pulled_onto_the_channel_where_the_domain_has_one():
    """The flowline is the centerline companion, so a bank click lands in water."""
    placed = _settle(Point(-122.65, 45.555), ingest(_REACH))
    assert placed["lat"] == pytest.approx(45.53, abs=1e-3)
    assert "onto the flowline" in placed["note"]
