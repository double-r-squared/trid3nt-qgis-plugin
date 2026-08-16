"""Offline unit tests: ARTEMIS REAL surveyed-structure path (no solve, no network).

The real-marina demo (ADR 0237) meshes an ACTUAL breakwater as a thin solid
barrier: OSM man_made=breakwater/pier polylines [[lon,lat],...] are projected to
the mesh's LOCAL UTM frame (AOI SW-corner origin subtracted, matching the node
coordinates), and a proof-norm-#9 REMOVED control keeps the same bathy but drops
the solid barrier. This covers the pure geometry helpers + the strict-field
manifest gate; the physics-through-the-baked-binary pair is the in-image sandbox
(docs/proof/templates/artemis_real_breakwater_sandbox.py) + the live E2E.

Run: python3 -m pytest workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artemis_build as A  # noqa: E402
import entrypoint as E  # noqa: E402


def _tr(epsg=32616):
    from pyproj import Transformer
    return Transformer.from_crs(4326, epsg, always_xy=True)


def test_polylines_to_segments_local_frame_and_count():
    tr = _tr()
    # two OSM-style ways: a 3-vertex + a 2-vertex line near Marquette
    polylines = [
        [[-87.379, 46.5340], [-87.378, 46.5390], [-87.377, 46.5440]],
        [[-87.383, 46.5427], [-87.384, 46.5431]],
    ]
    x0m, y0m = tr.transform(-87.392, 46.528)   # AOI SW corner
    segs = A._polylines_to_segments(polylines, tr, x0m, y0m)
    # 2 + 1 = 3 consecutive segments
    assert segs.shape == (3, 4)
    # local frame: every coordinate is a small positive offset from the SW corner
    # (harbour-scale, well under 10 km), NOT a raw ~4e5 UTM easting.
    assert np.all(segs >= -1.0) and np.all(segs < 10000.0)


def test_polylines_to_segments_rejects_empty():
    tr = _tr()
    with pytest.raises(A.ArtemisInputError):
        A._polylines_to_segments([[[-87.38, 46.54]]], tr, 0.0, 0.0)  # single vertex


def test_dist_to_segments_is_min_over_segments():
    # a unit segment along +x at y=0 from (0,0)->(10,0) and one at y=100
    segs = np.array([[0.0, 0.0, 10.0, 0.0], [0.0, 100.0, 10.0, 100.0]])
    d = A._dist_to_segments(np.array([5.0, 5.0]), np.array([3.0, 97.0]), segs)
    assert np.allclose(d, [3.0, 3.0])              # nearest of the two lines
    # a point off the end projects to the endpoint, not the infinite line
    d2 = A._dist_to_segments(np.array([-4.0]), np.array([0.0]), segs)
    assert np.isclose(d2[0], 4.0)


def test_config_accepts_real_structure_fields():
    cfg = A.ArtemisConfig(
        wave_mode="diffraction", bathy_source="noaa_greatlakes",
        bbox=(-87.392, 46.528, -87.368, 46.55),
        breakwater_polylines=[[[-87.379, 46.534], [-87.377, 46.544]]],
        remove_structure=True)
    assert cfg.breakwater_polylines and cfg.remove_structure is True


def test_entrypoint_strict_field_gate_allows_real_structure(tmp_path):
    # the strict-field manifest gate (ADR 0158/0148) must ACCEPT the two new keys
    # (else a live real-structure run silently no-ops them).
    cfg = E._artemis_config(tmp_path, {
        "wave_mode": "diffraction", "bathy_source": "noaa_greatlakes",
        "bbox": [-87.392, 46.528, -87.368, 46.55],
        "breakwater_polylines": [[[-87.379, 46.534], [-87.377, 46.544]]],
        "remove_structure": False,
    })
    assert cfg.breakwater_polylines and cfg.remove_structure is False
    assert cfg.workdir == str(tmp_path)


def test_entrypoint_strict_field_gate_still_rejects_unknown(tmp_path):
    with pytest.raises(E.ArtemisManifestUnknownFieldsError):
        E._artemis_config(tmp_path, {"wave_mode": "diffraction", "bogus_knob": 1})


def test_parser_version_bumped_for_real_structure():
    # the image-provenance marker MUST move on a worker-output-contract change too
    # (ADR 0148); -3 adds the in-worker lake-datum bed COG (ADR 0244 S3).
    assert E._ARTEMIS_PARSER_VERSION == "artemis-agitation-3"


# ---------------------------------------------------------------------------
# Slit-connectivity invariant (ADR 0237 amendment). NATE flagged "agitation
# moving through the breakwater" on the Cinder Pond pair; the diagnosis found it
# was a RENDER-LIE (scipy.griddata bridged the mesh slit), NOT a solution leak:
# the solid-barrier mask (0.6*dx wide) genuinely DISCONNECTS the mesh across the
# structure line (0 elements cross it), while the REMOVED control is a true
# no-slit full mesh (elements DO cross). These tests pin that topology through the
# real build_mesh marching-cell logic so a future mask/geometry change that
# silently re-bridges the barrier fails offline.
# ---------------------------------------------------------------------------
def _seg_proper_cross(p1, p2, p3, p4):
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = ccw(p3, p4, p1), ccw(p3, p4, p2)
    d3, d4 = ccw(p1, p2, p3), ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _barrier_crossing_edges(mesh, segs):
    """Count mesh EDGES (from the element table) that properly cross a barrier seg."""
    X, Y = mesh["X"], mesh["Y"]
    edges = set()
    for a, b, c in mesh["ikle"]:
        for u, v in ((a, b), (b, c), (c, a)):
            edges.add((min(int(u), int(v)), max(int(u), int(v))))
    n = 0
    for u, v in edges:
        p1, p2 = (X[u], Y[u]), (X[v], Y[v])
        for sax, say, sbx, sby in segs:
            if _seg_proper_cross(p1, p2, (sax, say), (sbx, sby)):
                n += 1
                break
    return n


def _harbour_mesh(*, mask_barrier):
    """Build a flat-bed harbour grid with a diagonal barrier, EXACTLY the way the
    real diffraction path does: nodes within 0.6*dx of the structure line are
    masked out (present) vs kept (removed). Uses the real build_mesh."""
    Lx = Ly = 900.0
    dx = 30.0
    # diagonal barrier segment across the interior (local frame)
    segs = np.array([[150.0, 200.0, 700.0, 750.0]], dtype=float)

    def keep_fn(Xg, Yg):
        wet = np.ones(Xg.shape, dtype=bool)
        if not mask_barrier:
            return wet
        on_bw = A._dist_to_segments(Xg, Yg, segs) <= dx * 0.6
        return wet & (~on_bw)

    mesh = A.build_mesh(Lx, Ly, dx, lambda X, Y: np.full_like(X, -10.0),
                        dy=dx, keep_fn=keep_fn)
    return mesh, segs


def test_solid_barrier_mask_disconnects_the_mesh():
    # PRESENT: the 0.6*dx solid-barrier mask must leave ZERO elements crossing the
    # structure line -- the slit is a true topological cut, not a render artifact.
    mesh, segs = _harbour_mesh(mask_barrier=True)
    assert _barrier_crossing_edges(mesh, segs) == 0
    # and no kept node sits within 0.6*dx of the line
    d = A._dist_to_segments(mesh["X"], mesh["Y"], segs)
    assert float(d.min()) > 30.0 * 0.6 - 1e-6


def test_removed_control_has_no_slit():
    # REMOVED: the no-structure control is a full mesh; elements DO cross the former
    # barrier line and nodes sit on it (proving the removed panel is a physical
    # no-structure mesh, not a slit with the classification merely dropped).
    mesh, segs = _harbour_mesh(mask_barrier=False)
    assert _barrier_crossing_edges(mesh, segs) > 0
    d = A._dist_to_segments(mesh["X"], mesh["Y"], segs)
    assert float(d.min()) < 30.0 * 0.6
