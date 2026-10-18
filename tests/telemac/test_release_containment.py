"""The mesh's own record of the domain a stated release is tested against."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.authoring.release_point import _domain_m
from trid3nt_server.workflows.telemac.errors import TelemacError


class _Artifact:
    """The one thing the pre-flight reads off an accepted mesh."""

    def __init__(self, extent):
        self.provenance = {"recipe": {"mesher": "om2d", "extent": extent}}


def test_the_domain_read_is_the_mesh_own_record_of_what_it_was_cut_from():
    import json

    ring = [[-114.33, 42.57], [-114.29, 42.57], [-114.29, 42.59], [-114.33, 42.59],
            [-114.33, 42.57]]
    region = _domain_m(_Artifact(json.dumps({"type": "Polygon", "coordinates": [ring]})),
                       32611)
    # The mesh's own polygon, in the zone's metres: about 3.3 km by 2.2 km.
    width = region.bounds[2] - region.bounds[0]
    assert 3000.0 < width < 3600.0


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
        _domain_m(art, 32611)
    assert "no mapped shape" in str(excinfo.value)
