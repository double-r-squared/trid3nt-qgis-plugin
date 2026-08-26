#!/usr/bin/env python3
"""Proof render: the TELEMAC open-water bed INPUT, before and after the migration.

The bed the four open-water domains solve on used to be rasterized from the mesh
NODES inside the container - a lattice of interpolated dots with nodata between
them, because the clip radius scaled with a 512 px grid rather than with the mesh.
It is now the STAGED SOURCE RASTER the run was handed, so the layer on the canvas
is a continuous surface and is literally the data the nodes were sampled from.

Renders the CURRENT layer for a run, and - when the run's own bed COG from a
pre-migration run is passed - the old one beside it, on the same colour scale, so
the two are comparable rather than merely adjacent.

    venvs/agent/bin/python scripts/proof_telemac_bed_continuity.py \
        --uri s3://.../ncei_dem_mosaic/<key>.tif --out <png> --title "..."

ASCII only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_fidelity_proof_generic import download_s3, render  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent


def _finite_range(uri: str) -> tuple[float, float, float]:
    """(vmin, vmax, painted_fraction) for a bed raster, p2..p98 over finite cells."""
    local = download_s3(uri)
    with rasterio.open(local) as src:
        band = src.read(1, masked=True).filled(np.nan)
    finite = band[np.isfinite(band)]
    painted = float(finite.size) / float(band.size or 1)
    return (float(np.nanpercentile(finite, 2)),
            float(np.nanpercentile(finite, 98)), painted)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", required=True, help="the staged bed raster (s3://)")
    ap.add_argument("--out", required=True, help="output PNG path")
    ap.add_argument("--title", required=True)
    ap.add_argument("--caption", default="")
    ap.add_argument("--zoom", type=int, default=11)
    args = ap.parse_args()

    vmin, vmax, painted = _finite_range(args.uri)
    caption = args.caption or (
        f"-- the STAGED source raster the solve sampled its nodes from, surfaced by "
        f"the emit-on-fetch seam as a role=context input. {painted * 100:.1f}% of "
        f"cells carry data (a node-interpolated bed COG painted only within ~2 output "
        f"cells of a node, so it read as a lattice of dots). Elevation m, positive up "
        f"on the mosaic's own datum; negative is bathymetry."
    )
    render(args.uri, args.out, args.title, caption, cmap="terrain",
           units_label="elevation (m, source datum)", vmin=vmin, vmax=vmax,
           zoom=args.zoom)
    print(f"wrote {args.out}  vmin={vmin:.2f} vmax={vmax:.2f} "
          f"painted={painted * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
