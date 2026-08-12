#!/usr/bin/env python3
"""Proof render for ADR 0227: the surfaced bathymetry INPUT layer over ESRI.

Renders the exact topobathy COG the schism_pahm_surge showcase surfaced as its
Case input layer, on ESRI World Imagery in EPSG:3857 with a hypsometric/terrain
ramp, so NATE can spot-check WHICH data fed the surge. Reuses the shared
render() in render_fidelity_proof_generic.py. ASCII only.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from render_fidelity_proof_generic import download_s3, render

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The topobathy COG the live schism_pahm_surge showcase surfaced (rode the
# EXISTING trid3nt-cache object; case 01KZV18TVBR25V5REVSNC5VRHG).
BATHY_URI = "s3://trid3nt-cache/cache/static-30d/topobathy/5d50194c46da76c485b6f997d66f03f0.tif"
OUT_PNG = str(_REPO_ROOT / "docs/proof/templates/input_bathymetry_layer.png")
TITLE = "Input: bathymetry (topobathy composite) over ESRI -- schism_pahm_surge"
CAPTION = (
    "ADR 0227 -- fetched topobathy surfaced as a role=context Case INPUT layer "
    "(continuous_dem hypsometric ramp, EPSG:3857 over ESRI World Imagery). "
    "Source: CUDEM 1/9\" nearshore + ETOPO 2022 shelf base, ~199 m fetch cell. "
    "Fed schism_pahm_surge Hurricane Ike (2008) Galveston surge; "
    "case 01KZV18TVBR25V5REVSNC5VRHG. Elevation m (NAVD88), negative = bathymetry."
)


def main() -> None:
    # Data-driven range: include the negative bathymetry (the render() default
    # clamps vmin=0, which would flatten the sea floor). Symmetric-ish p2..p98.
    local = download_s3(BATHY_URI)
    with rasterio.open(local) as src:
        band = src.read(1, masked=True).filled(np.nan)
    finite = band[np.isfinite(band)]
    vmin = float(np.nanpercentile(finite, 2))
    vmax = float(np.nanpercentile(finite, 98))
    render(
        BATHY_URI, OUT_PNG, TITLE, CAPTION,
        cmap="terrain", units_label="elevation (m, NAVD88)",
        vmin=vmin, vmax=vmax, zoom=9,
    )
    print(f"wrote {OUT_PNG}  vmin={vmin:.1f} vmax={vmax:.1f}")


if __name__ == "__main__":
    main()
