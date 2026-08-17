#!/usr/bin/env python3
"""Assemble the WETTING pure-2D fake-reach carve deck (/ OI-FT1).

This extends ``build_chippewa_fakereach_deck.py`` (which SOLVES but
stays DRY) with the ONE missing forcing link the chain named: the plan-HDF
``/Event Conditions`` 2D-BC-line flow-hydrograph enumeration read by the engine's
``read_un_q2d_bc_``. That schema was decoded (schema facts only) from shipped
HEC-RAS 6.6 pure-2D plan HDFs and is authored here by ``hecras_event_conditions``
against OUR carved ``Inflow`` BC line -- directing moving water onto the carved
2D area so it WETS.

The deck otherwise matches: the fresh NW-quadrant Muncie carve, the
Chippewa clean fake reach (``.x04``/``.b04``, an inert required-1D placeholder),
solved by production 6.6 ``RasGeomPreprocess`` + ``RasUnsteady``.

Usage: python build_chippewa_wetting_deck.py <out_rundir> [--peak-cfs F]
Solve: docker run --rm -v <out>:/run -v <freshtopo>:/ft:ro --entrypoint bash \
         trid3nt-local/hecras:latest -lc \
         '/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for p in (str(_HERE), str(_HECRAS2025)):
    if p not in sys.path:
        sys.path.insert(0, p)

from carve_muncie import load_muncie, carve, MUNCIE_PLAN  # noqa: E402
from hecras_deck2d import compose_pure2d_deck  # noqa: E402


def build(out: Path, keep_mask: np.ndarray, peak_cfs: float = 2000.0,
          n_bc_faces: int = 14) -> dict:
    """Carve the Muncie NW quadrant, then compose the wetting pure-2D deck.

    Thin glue over ``hecras_deck2d.compose_pure2d_deck`` (the source-agnostic
    composer): this function's ONLY responsibility is the CARVE (the mesh source);
    the whole deck assembly -- geometry HDF + BC lines + Event-Conditions forcing +
    .xNN/.bNN -- is the composer, shared verbatim with the C# AuthorMesh path.
    """
    import h5py

    m = load_muncie()
    r = carve(m, keep_mask)

    with h5py.File(MUNCIE_PLAN, "r") as f:
        projection = f.attrs["Projection"]
        proj_wkt = projection.decode() if isinstance(projection, bytes) else projection

    info = compose_pure2d_deck(
        out, r.mesh, r.tables,
        projection_wkt=proj_wkt,
        target_peak_cfs=peak_cfs,
        n_bc_faces=n_bc_faces,
    )
    return {
        "real": r.n_real, "ghost": r.n_ghost, "faces": r.n_faces,
        "perimeter_pts": info["perimeter_pts"], "bc_faces": info["bc_faces"],
        "bc_length_ft": info["bc_length_ft"], "ec_peak_cfs": info["ec_peak_cfs"],
        "ec_ordinates": info["ec_ordinates"],
        "stale_1d_ec_removed": info["stale_1d_ec_removed"],
        "deck": "chippewa-wetting-2dbc-ec", "rundir": str(out),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--peak-cfs", type=float, default=2000.0)
    ap.add_argument("--xmax", type=float, default=408600.0)
    ap.add_argument("--ymin", type=float, default=1803025.0)
    args = ap.parse_args()

    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < args.xmax) & (c[:, 1] > args.ymin)
    info = build(Path(args.out), keep, peak_cfs=args.peak_cfs)
    print("[build] " + "  ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
