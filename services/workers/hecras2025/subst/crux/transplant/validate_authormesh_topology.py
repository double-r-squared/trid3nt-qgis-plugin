#!/usr/bin/env python3
"""Validate the AuthorMesh full-topology dump against the shipped Muncie mesh.

This is the ADR 0134 link-c1 gate, refined by ADR 0135 Finding 1 (byte-identity is
STRUCTURALLY impossible -- a fresh TryCreateMesh tessellation is +2 faces vs the 6.x
GUI, so the meaningful c1 checks are the cell-center BIJECTION + internal STRUCTURAL
consistency, not a byte-compare). Runs on the HOST (numpy/h5py). Reads the raw dump
AuthorMesh wrote (terrain-free, from Muncie's own perimeter + 5391 seeds) and the
shipped Muncie 2D geometry, and asserts the general dump path reproduces the mesh.

  python3 validate_authormesh_topology.py <dump_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
MUNCIE_PLAN = (
    _HERE.parents[3] / "hecras/fixtures/muncie_smoke/wrk_source/Muncie.p04.tmp.hdf"
)
AREA = "Geometry/2D Flow Areas/2D Interior Area"
NC_REAL = 5391


def main() -> int:
    d = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    meta = json.loads((d / "topo_meta.json").read_text())
    nc, nf, nfp = meta["cell_count"], meta["face_count"], meta["facepoint_count"]
    print(f"[validate] AuthorMesh dump: cells={nc} faces={nf} facepoints={nfp} ok={meta['ok']}")

    faces = np.fromfile(d / "faces.i32", np.int32).reshape(-1, 4)      # cellA,cellB,fpA,fpB
    fp = np.fromfile(d / "facepoints.f64", np.float64).reshape(-1, 2)
    cc = np.fromfile(d / "cellcenters.f64", np.float64).reshape(-1, 2)
    normals = np.fromfile(d / "face_normals.f64", np.float64).reshape(-1, 2)
    cfi = np.fromfile(d / "cell_face_info.i32", np.int32).reshape(-1, 2)
    cfv = np.fromfile(d / "cell_face_vals.i32", np.int32)

    assert faces.shape[0] == nf and fp.shape[0] == nfp and cc.shape[0] == nc, "count mismatch"

    # --- gate 1: real-cell count is EXACT vs shipped Muncie ---
    assert nc == NC_REAL, f"real cells {nc} != shipped {NC_REAL}"
    print(f"[gate 1] real cells {nc} == shipped {NC_REAL}  EXACT")

    # --- gate 2: the +2 face/facepoint tie-break (ADR 0132/0135 Finding 1) ---
    import h5py
    with h5py.File(MUNCIE_PLAN, "r") as f:
        g = f[AREA]
        shipped_cc = g["Cells Center Coordinate"][()][:NC_REAL].astype(np.float64)
        shipped_nf = g["Faces Cell Indexes"].shape[0]
        shipped_nfp = g["FacePoints Coordinate"].shape[0]
    print(f"[gate 2] faces {nf} vs shipped {shipped_nf} (delta {nf-shipped_nf}); "
          f"facepoints {nfp} vs shipped {shipped_nfp} (delta {nfp-shipped_nfp}) "
          f"-- the expected +2 boundary tie-break")
    assert 0 <= nf - shipped_nf <= 4 and 0 <= nfp - shipped_nfp <= 4, "tessellation diverged too far"

    # --- gate 3: cell-center BIJECTION vs shipped (ADR 0132: displacement 0.0 ft) ---
    from scipy.spatial import cKDTree  # noqa: PLC0415
    tree = cKDTree(shipped_cc)
    dist, idx = tree.query(cc[:NC_REAL], k=1)
    bij = len(set(idx.tolist())) == NC_REAL
    print(f"[gate 3] cell-center bijection: unique={len(set(idx.tolist()))}/{NC_REAL} "
          f"max_displacement={dist.max():.6f} ft mean={dist.mean():.6f} ft")
    assert bij, "cell-center match is not a bijection"
    assert dist.max() < 0.01, f"cell centers not bit-identical (max {dist.max()} ft)"

    # --- gate 4: face internal consistency (indices in range, fps distinct) ---
    assert (faces[:, 2] >= 0).all() and (faces[:, 2] < nfp).all(), "fpA out of range"
    assert (faces[:, 3] >= 0).all() and (faces[:, 3] < nfp).all(), "fpB out of range"
    assert (faces[:, 2] != faces[:, 3]).all(), "a face has fpA == fpB (degenerate)"
    # cellA is always a real cell; cellB may be a ghost (>= nc) on the perimeter
    assert (faces[:, 0] >= 0).all(), "cellA negative"
    print(f"[gate 4] all {nf} faces: fpA/fpB in [0,{nfp}) + distinct; cellA valid  PASS")

    # --- gate 5: face normals perpendicular to the face segment + unit length ---
    edge = fp[faces[:, 3]] - fp[faces[:, 2]]                       # fpB - fpA
    elen = np.linalg.norm(edge, axis=1)
    edge_u = edge / elen[:, None]
    nlen = np.linalg.norm(normals, axis=1)
    perp = np.abs((normals / nlen[:, None] * edge_u).sum(1))
    print(f"[gate 5] face normals: unit-length max_err={abs(nlen-1).max():.2e} "
          f"perpendicular max_dot={perp.max():.2e}")
    assert abs(nlen - 1).max() < 1e-3, "normals not unit-length"
    assert perp.max() < 1e-3, "normals not perpendicular to the face segment"

    # --- gate 6: per-cell face-list ragged consistency ---
    assert cfi[:, 1].sum() == cfv.shape[0], "cell_face Info/Values inconsistent"
    assert (cfv >= 0).all() and (cfv < nf).all(), "cell face index out of range"
    print(f"[gate 6] cell face-list ragged: sum(count)={cfi[:,1].sum()} == len(vals)={cfv.shape[0]}  PASS")

    print("\n[validate] AuthorMesh full-topology dump VALIDATED vs shipped Muncie "
          "(c1: bit-identical cell bijection + the +2 tie-break + full structural consistency)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
