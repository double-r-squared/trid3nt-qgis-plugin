"""Offline tests for the fresh-topology carve + deck authors (ADR 0136).

Worker-local (numpy/h5py only, no docker, no server code). Validates the carve
machinery reproduces HEC's 2D conventions (identity + sub-rectangle) and that the
.xNN weir-removal + .bNN patch transforms are structurally correct. The SOLVE
itself is proven separately in-container (ADR 0136); these gates guard the pure
authoring logic the way ADR 0133's writer round-trip does.

Run from this dir:  python -m pytest test_freshtopo.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from carve_muncie import load_muncie, carve, MUNCIE_PLAN  # noqa: E402
from hecras_pure2d_deck import remove_lateral_weirs, patch_muncie_bnn  # noqa: E402

_WRK = MUNCIE_PLAN.parent


def _nw_carve():
    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < 408600) & (c[:, 1] > 1803025)
    return m, carve(m, keep)


def test_identity_carve_reproduces_counts():
    m = load_muncie()
    r = carve(m, np.ones(m.nc_real, bool))
    assert r.n_real == m.nc_real
    assert r.n_faces == m.faces_cell.shape[0]
    assert r.n_fp == m.fp_coord.shape[0]
    assert r.n_cut_faces == 0
    assert r.n_ghost == int((m.faces_cell[:, 1] >= m.nc_real).sum())


def test_subcarve_fresh_topology_is_consistent():
    m, r = _nw_carve()
    mesh = r.mesh
    # a genuine SUBSET with a fresh boundary
    assert r.n_real < m.nc_real
    assert r.n_cut_faces > 0            # faces interior in Muncie, now external
    # every face normal perpendicular + unit, points col0 -> col1
    fp = mesh.facepoints_coord
    ffp = mesh.faces_facepoint_indexes
    edge = fp[ffp[:, 1]] - fp[ffp[:, 0]]
    edge_u = edge / np.linalg.norm(edge, axis=1, keepdims=True)
    nuv = mesh.faces_normal_unit_vector_length[:, :2]
    assert np.abs((nuv * edge_u).sum(1)).max() < 1e-4
    assert abs(np.linalg.norm(nuv, axis=1).max() - 1) < 1e-4
    cen = mesh.cell_center_coord
    fc = mesh.faces_cell_indexes
    assert ((nuv * (cen[fc[:, 1]] - cen[fc[:, 0]])).sum(1) > 0).all()
    # ghost per external face; ghost center == face midpoint
    ext = np.where(fc[:, 1] >= r.n_real)[0]
    assert ext.size == r.n_ghost
    for nf in ext[:20]:
        a, b = ffp[nf]
        assert np.allclose(cen[fc[nf, 1]], 0.5 * (fp[a] + fp[b]))
    # orientation membership rule + ragged consistency
    cfo_i, cfo_v = mesh.cell_face_orientation_info, mesh.cell_face_orientation_values
    assert cfo_i[:, 1].sum() == cfo_v.shape[0]
    for c in range(0, cen.shape[0], 97):
        s, cnt = cfo_i[c]
        for nf, ori in cfo_v[s:s + cnt]:
            assert ori == (1 if fc[nf, 0] == c else -1)
    # perimeter walked, CCW, non-closed, one point per external face
    assert mesh.perimeter.shape[0] == r.n_ghost
    assert not np.allclose(mesh.perimeter[0], mesh.perimeter[-1])


def test_subcarve_carries_solverproven_subgrid_values():
    m, r = _nw_carve()
    # carved real cells keep Muncie's exact volume-elevation curves (re-indexed)
    old_real = np.where((m.cell_center[:m.nc_real, 0] < 408600) &
                        (m.cell_center[:m.nc_real, 1] > 1803025))[0]
    for new_i in range(0, r.n_real, 211):
        oc = old_real[new_i]
        s, cnt = m.cell_vol_info[oc]
        assert np.array_equal(r.tables.cell_vol_elev[new_i], m.cell_vol_values[s:s + cnt])


def test_remove_lateral_weirs():
    x04 = (_WRK / "Muncie.x04").read_text()
    out = remove_lateral_weirs(x04, new_perimeter=171)
    nodes = [l for l in out.splitlines() if l.startswith("NODE")]
    assert len(nodes) == 61                       # 63 - 2 weirs
    assert all(l[7] == "1" for l in nodes)        # no type-6 (lateral) nodes left
    b = out.splitlines()[4].split()
    assert (int(b[0]), int(b[1])) == (62, 64)     # node counts decremented by 2
    assert "171       T" in out and "170       T" not in out


def test_patch_muncie_bnn_forcing():
    b04 = (_WRK / "Muncie.b04").read_text()
    out = patch_muncie_bnn(b04, flow_scale=2.0).splitlines()
    # breach zeroed
    bi = out.index("Breach Data")
    assert out[bi + 1].strip() == "0"
    # a single valid hydrograph location (not zero -> no output div-by-zero)
    hi = out.index("HYDROGRAPH LOCATIONS")
    assert out[hi + 1].strip() == "1"
    # inflow ordinates doubled (13500 -> 27000 in the first flow pair)
    flat = " ".join(out)
    assert "27000" in flat and "42000" in flat     # 13500*2, 21000*2


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
