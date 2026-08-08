"""Offline unit tests for the rain-on-grid worker payload (ADR 0196 C1).

TELEMAC-free: exercise the pure helpers (boundary walk, outlet classification,
CN scatter map, distributed-friction zones, mass-balance parse) WITHOUT
telemac2d / the network. The live solve THROUGH the image is covered by
scripts/sandbox/telemac/rog_offline_smoke.py and the C4 Coweeta proof.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

_WORKER_DIR = Path(__file__).parent.parent
if str(_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKER_DIR))

import rog_build as R  # noqa: E402


def _unit_square_mesh():
    """A 4x4 node grid triangulated -> 9 quads -> 18 triangles (clean TIN)."""
    xs = np.linspace(0.0, 30.0, 4)
    ys = np.linspace(0.0, 30.0, 4)
    gx, gy = np.meshgrid(xs, ys)
    X = gx.ravel().astype(float)
    Y = gy.ravel().astype(float)
    tris = []
    n = 4
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            b = j * n + i + 1
            c = (j + 1) * n + i
            d = (j + 1) * n + i + 1
            tris.append([a, b, d])
            tris.append([a, d, c])
    return X, Y, np.asarray(tris, dtype=np.int64)


def test_build_boundary_ring_is_the_perimeter():
    X, Y, ikle = _unit_square_mesh()
    b = R.build_boundary(X, Y, ikle)
    # a 4x4 grid perimeter = 12 boundary nodes, 4 interior.
    assert b["nptfr"] == 12
    assert b["n_rings"] == 1
    # ipob is rank-based: nonzero exactly on the ring, 0 in the interior.
    assert int((b["ipob"] > 0).sum()) == 12
    assert set(int(n) for n in b["ring"]) == set(int(i) for i in np.where(b["ipob"] > 0)[0])


def test_classify_outlet_marks_nearest_ring_nodes_free():
    X, Y, ikle = _unit_square_mesh()
    b = R.build_boundary(X, Y, ikle)
    # pour point at the (30,0) corner -> the nearby ring nodes are the free exit.
    bc = R.classify_outlet(X, Y, b["ring"], (30.0, 0.0), n_outlet_nodes=3)
    assert bc["n_outlet_nodes"] == 3
    # free exit uses KSORT=4 on H/U/V; walls use KLOG=2.
    for i, cl in enumerate(bc["cls"]):
        if cl == "outlet":
            assert bc["lihbor"][i] == 4 and bc["liubor"][i] == 4 and bc["livbor"][i] == 4
        else:
            assert bc["lihbor"][i] == 2 and bc["liubor"][i] == 2
    # the (30,0) node is the closest boundary node -> dist ~ 0.
    assert bc["outlet_dist_min_m"] == 0.0


def test_classify_outlet_spans_at_least_one_edge():
    X, Y, ikle = _unit_square_mesh()
    b = R.build_boundary(X, Y, ikle)
    bc = R.classify_outlet(X, Y, b["ring"], (30.0, 0.0), n_outlet_nodes=1)
    # clamped up to 2 so the outlet spans a boundary EDGE (discharge integration).
    assert bc["n_outlet_nodes"] >= 2
    edges = R._outlet_edges(b["ring"], bc["outlet_nodes"])
    assert len(edges) >= 1


def test_write_cn_map_clamps_and_formats(tmp_path):
    X = np.array([0.0, 10.0, 20.0])
    Y = np.array([0.0, 0.0, 0.0])
    cn = np.array([150.0, 55.0, -3.0])  # out-of-range values must clamp to (0,100]
    p = tmp_path / "cn.dat"
    R.write_cn_map(str(p), X, Y, cn)
    body = [ln for ln in p.read_text().splitlines() if not ln.startswith("#")]
    assert len(body) == 3
    vals = [float(ln.split()[2]) for ln in body]
    assert vals[0] == 100.0 and vals[2] == 1.0 and vals[1] == 55.0


def test_write_cn_map_rejects_length_mismatch(tmp_path):
    with pytest.raises(R.RogInputError):
        R.write_cn_map(str(tmp_path / "cn.dat"),
                       np.array([0.0, 1.0]), np.array([0.0, 1.0]),
                       np.array([50.0]))


def test_write_friction_files_distinct_zones(tmp_path):
    manning = np.array([0.05, 0.05, 0.20, 0.20, 0.10])
    cof = tmp_path / "fric.tbl"
    zones = tmp_path / "zones.dat"
    stats = R.write_friction_files(str(cof), str(zones), manning)
    assert stats["n_zones"] == 3
    cof_txt = cof.read_text()
    # one MANNING line per distinct value + a terminating END (friction_scan
    # aborts on a bare EOF).
    assert cof_txt.count("MANNING") == 3
    assert cof_txt.strip().endswith("END")
    # ZONES FILE: one 1-based "node zone" line per node.
    zlines = [ln.split() for ln in zones.read_text().splitlines()]
    assert len(zlines) == 5
    assert zlines[0] == ["1", "1"] and zlines[2][1] == zlines[3][1]  # 0.20 nodes share a zone


def test_parse_mass_balance_reads_engine_closure():
    listing = (
        "     RUNOFF_SCS_CN : ACCUMULATED RAINFALL :    0.1500000     M\n"
        "                       BALANCE OF WATER VOLUME\n"
        "     VOLUME IN THE DOMAIN :    13789.38     M3\n"
        "     FLUX BOUNDARY    1:    -5.6872     M3/S  ( >0 : ENTERING  <0 : EXITING )\n"
        "     ADDITIONAL VOLUME DUE TO SOURCE TERMS:     25.96     M3\n"
        "     RELATIVE ERROR IN VOLUME AT T =        1800.     S :   -0.39E-15\n"
    )
    out = R._parse_mass_balance(listing)
    assert abs(out["continuity_rel_error"]) < 1e-12
    assert out["final_domain_volume_m3"] == 13789.38
    assert out["accumulated_rainfall_m"] == 0.15
    assert out["source_volume_m3"] == 25.96
    assert out["listing_peak_boundary_flux_m3s"] == 5.6872
