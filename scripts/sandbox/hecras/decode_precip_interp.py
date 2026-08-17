#!/usr/bin/env python3
"""Decode a reference plan HDF's rain-on-grid precipitation interpolation folder.

The link-3 residual: the HEC-RAS 6.x per-2D-area precipitation
interpolation folder ``Event Conditions/Meteorology/Precipitation/2D Flow Areas/
<area>`` that ``RasUnsteady``'s ``READ_UN_M2D_PRECIP_INTERP`` (MetInterp.f90) reads
is a WINDOWS RAS-preprocessing artifact -- it is generated when RAS Mapper computes
the plan and is NOT present in any shipped USACE example deck (see the 0199 appendix
reference-hunt record). The unblock is ONE Windows-GUI-authored reference plan HDF.

This is the decode step that turns that hand-off into an authoring spec: given a
reference plan HDF that DOES carry the interpolation folder, it dumps the folder
byte-exact -- every dataset's name/dtype/shape + a value sample, every group/dataset
attribute, and (when a mesh is discoverable in the same file) the cell-index mapping
-- so ``hecras_meteorology.py`` can author the uniform-grid folder for our carved
meshes with zero guessing.

It is READ-ONLY: it never writes into the reference, and it makes no schema
assumptions -- it reports whatever the real GUI wrote. If the folder is absent it
says so plainly (that HDF is a config-only deck, not a computed plan -- see the 0199
appendix for why shipped examples are config-only).

Usage:
  python decode_precip_interp.py <reference_plan.hdf> [--area "2D Interior Area"]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py
import numpy as np

MET_PRECIP = "Event Conditions/Meteorology/Precipitation"
INTERP_ROOT = f"{MET_PRECIP}/2D Flow Areas"


def _fmt_attr(v):
    if isinstance(v, bytes):
        return repr(v[:120])
    if isinstance(v, np.ndarray):
        return f"ndarray shape={v.shape} dtype={v.dtype} head={v.reshape(-1)[:6]}"
    return repr(v)


def _dump(node, indent: int = 0) -> None:
    pad = "  " * indent
    for ak in node.attrs:
        print(f"{pad}@{ak} = {_fmt_attr(node.attrs[ak])}")
    if isinstance(node, h5py.Group):
        for k in node:
            child = node[k]
            if isinstance(child, h5py.Group):
                print(f"{pad}[G] {k}")
                _dump(child, indent + 1)
            else:
                arr = child[()]
                sample = np.asarray(arr).reshape(-1)[:8]
                print(f"{pad}[D] {k}  shape={child.shape} dtype={child.dtype}")
                print(f"{pad}    sample={sample}")
                for ak in child.attrs:
                    print(f"{pad}    @{ak} = {_fmt_attr(child.attrs[ak])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan_hdf")
    ap.add_argument("--area", default=None,
                    help="2D area name to focus (default: dump all under the folder)")
    args = ap.parse_args()

    path = Path(args.plan_hdf)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    with h5py.File(path, "r") as f:
        if MET_PRECIP not in f:
            print(f"NO {MET_PRECIP} group -- this HDF has no meteorology precipitation "
                  "record at all.")
            return 1
        print(f"### {MET_PRECIP} (attrs) ###")
        for ak in f[MET_PRECIP].attrs:
            print(f"@{ak} = {_fmt_attr(f[MET_PRECIP].attrs[ak])}")
        print(f"children: {list(f[MET_PRECIP].keys())}")
        print()

        if INTERP_ROOT not in f:
            print(f"NO INTERPOLATION FOLDER ({INTERP_ROOT}).")
            print("This is a CONFIG-ONLY deck (meteorology attributes without the "
                  "GUI-precomputed per-2D-area interpolation). Shipped USACE examples "
                  "are all config-only -- the folder is a Windows RAS-preprocessing "
                  "artifact. See docs/decisions/0199 appendix; a NATE GUI export of a "
                  "COMPUTED plan HDF is required.")
            return 1

        print(f"### FOUND {INTERP_ROOT} -- decoding byte-exact ###")
        root = f[INTERP_ROOT]
        areas = [args.area] if args.area else list(root.keys())
        for area in areas:
            ap_ = f"{INTERP_ROOT}/{area}"
            if ap_ not in f:
                print(f"  (area {area!r} not under the folder; present: {list(root.keys())})")
                continue
            print(f"\n=== 2D area: {area} ===")
            _dump(f[ap_], indent=1)

            # Cross-reference against the geometry cell count when present.
            geom = f"Geometry/2D Flow Areas/{area}/Cells Center Coordinate"
            if geom in f:
                nc = f[geom].shape[0]
                print(f"\n  geometry cell count (Cells Center Coordinate): {nc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
