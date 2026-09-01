"""Which end of the reach ``spill_fraction`` counts from, proved three ways.

``spill_fraction`` walks the reach's own centerline, and every claim the run
makes downstream of the release - the plume travel, the DO sag, the deposition
pattern - reads the wrong way round if the walk starts at the wrong end. Nothing
in a solved result says which end it started from: a plume that advected upstream
looks exactly like a plume that advected downstream on a reversed picture.

So the invariant is pinned from three independent directions:

  * chainage 0 is UPSTREAM, at the seed the flowline was navigated downstream
    from, whatever order the source document's vertices happened to arrive in;
  * the BED agrees - fitted along that same normalized centerline, it FALLS as
    chainage rises, so the water that leaves the release goes the way the
    fraction counts;
  * the walk DISCRIMINATES - 0.1 lands near the inflow and 0.9 near the outflow,
    rather than both landing near the middle of a line nobody oriented.
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.shared.nodes import (
    fit_downstream_bed,
    read_centerline_utm,
)
from trid3nt_server.workflows.telemac.steps.deck import _settle_release

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


def _walk(fraction: float, centerline_utm, monkeypatch):
    """Where an unplaced release lands -> ``(lon, lat)``."""
    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: _HOLDS_EVERYTHING)
    mesh = {"display_uri": "s3://m/M/mesh.2dm",
            "artifact": type("A", (), {"utm_epsg": _UTM_EPSG})()}
    (lon, lat), note = asyncio.run(_settle_release(
        None, mesh=mesh, centerline=None, centerline_utm=centerline_utm,
        utm_epsg=_UTM_EPSG, spill_fraction=fraction))
    assert note is None  # a derived release inside the mesh relocates nothing
    return lon, lat


# --------------------------------------------------------------------------- #
# 1. Chainage 0 is upstream, whichever way the document was written.
# --------------------------------------------------------------------------- #
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


def test_fraction_zero_is_the_upstream_end_and_one_is_the_downstream_end(monkeypatch):
    line = read_centerline_utm(_line(_COORDS), _UTM_EPSG, start_lonlat=_SEED)
    assert _walk(0.0, line, monkeypatch)[0] == pytest.approx(_COORDS[0][0], abs=1e-6)
    assert _walk(1.0, line, monkeypatch)[0] == pytest.approx(_COORDS[-1][0], abs=1e-6)


# --------------------------------------------------------------------------- #
# 2. The bed cross-check: it falls the way the fraction counts.
# --------------------------------------------------------------------------- #
def test_the_bed_fitted_on_that_centerline_falls_with_chainage():
    """The release walk and the bed fit read the SAME line the SAME way.

    If the two disagreed, a run would release its plume at the low end of a bed
    that slopes toward it - a source the flow carries out of the domain the way
    it came in, reported as a downstream study.
    """
    line = read_centerline_utm(_line(_COORDS), _UTM_EPSG, start_lonlat=_SEED)
    # Nodes strung along the centerline, sampling a DEM that drops eastward with
    # enough noise to run locally uphill - which is the case the fit exists for.
    nodes = np.array([line[0] + (line[-1] - line[0]) * t
                      for t in np.linspace(0.0, 1.0, 41)])
    span = float(np.hypot(*(line[-1] - line[0])))
    chainage = np.linspace(0.0, span, nodes.shape[0])
    sampled = 100.0 - 0.002 * chainage + np.tile([0.0, 0.6], 21)[:41]

    bed, stats = fit_downstream_bed(nodes, line, sampled,
                                    min_slope=1e-4, max_slope=1e-2)
    assert stats["measured_slope"] > 0.0  # positive = downhill
    assert stats["enforced_slope"] == pytest.approx(stats["measured_slope"],
                                                    abs=1e-4)
    assert stats["bed_drop_m"] > 0.0
    # Monotone: every step downstream is a step down.
    assert np.all(np.diff(bed) < 0.0)
    # And the release walk agrees: the 0.1 station stands above the 0.9 one.
    near, far = int(0.1 * (bed.size - 1)), int(0.9 * (bed.size - 1))
    assert bed[near] > bed[far]


# --------------------------------------------------------------------------- #
# 3. Discrimination: the two fractions are not the same place.
# --------------------------------------------------------------------------- #
def test_a_tenth_lands_near_the_inflow_and_nine_tenths_near_the_outflow(monkeypatch):
    """A walk that ignored its argument would put both at the same station."""
    line = read_centerline_utm(_line(_COORDS), _UTM_EPSG, start_lonlat=_SEED)
    span = float(np.hypot(*(line[-1] - line[0])))
    to_utm = read_centerline_utm  # the same reader both stations are measured in

    def _station(fraction: float) -> float:
        lon, lat = _walk(fraction, line, monkeypatch)
        point = to_utm(_line([[lon, lat], [lon + 1e-6, lat]]), _UTM_EPSG,
                       start_lonlat=(lon, lat))[0]
        return float(np.hypot(*(point - line[0])))

    upstream, downstream = _station(0.1), _station(0.9)
    assert upstream == pytest.approx(0.1 * span, rel=0.02)
    assert downstream == pytest.approx(0.9 * span, rel=0.02)
    assert downstream - upstream == pytest.approx(0.8 * span, rel=0.02)
