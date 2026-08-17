#!/usr/bin/env python3
"""Compose a pure-2D deck from a C# AuthorMesh dump -- the fresh-C#-topology SOLVE.

This is the link c2 probe: a mesh authored ENTIRELY by the C#
AuthorMesh path (TryCreateMesh topology + ComputeFrom subgrid tables over real
terrain), adapted to Mesh2D, composed into the pure-2D deck, and handed to the
production 6.6 engines. Unlike the carve (which reindexes Muncie's solver-proven
arrays), NOTHING here is Muncie-proven: the tessellation AND the subgrid tables
are freshly computed. If it solves + wets, the last flagged risk is discharged.

Usage: python build_authormesh_deck.py <dump_dir> <out_rundir> [--peak-cfs F]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from authormesh_to_mesh2d import load_authormesh
from hecras_deck2d import compose_pure2d_deck, MUNCIE_PLAN


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("out")
    ap.add_argument("--peak-cfs", type=float, default=3000.0)
    ap.add_argument("--inflow-edge", default="e")
    ap.add_argument("--ds-edge", default="w")
    args = ap.parse_args()

    res = load_authormesh(Path(args.dump))
    with h5py.File(MUNCIE_PLAN, "r") as f:
        proj = f.attrs["Projection"]
        proj = proj.decode() if isinstance(proj, bytes) else proj

    info = compose_pure2d_deck(
        Path(args.out), res.mesh, res.tables,
        projection_wkt=proj, target_peak_cfs=args.peak_cfs,
        inflow_edge=args.inflow_edge, ds_edge=args.ds_edge)
    info.pop("paths", None)
    print("[authormesh-deck] real=%d ghost=%d faces=%d  " % (
        res.n_real, res.n_ghost, res.n_faces)
        + "  ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
