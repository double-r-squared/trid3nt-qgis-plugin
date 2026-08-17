#!/usr/bin/env python3
"""QGIS-true proof renders of the in-worker lake-datum bed INPUT layers (
S3): the tomawac (wave_field) + artemis (agitation) sampled beds surfaced as
role=context Case inputs, on ESRI World Imagery with a terrain ramp. Reuses the
shared render() (EPSG:3857 over ESRI). Pass the two live bed-COG uris. ASCII only.

Run: set -a; source .env.local; set +a; \
     venvs/agent/bin/python scripts/proof_wave_bed_input_render.py <tomawac_uri> <artemis_uri>
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_fidelity_proof_generic import download_s3, render  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_OUT = _REPO / "docs/proof/templates"


def _range(uri: str) -> tuple[float, float]:
    local = download_s3(uri)
    with rasterio.open(local) as src:
        band = src.read(1, masked=True).filled(np.nan)
    finite = band[np.isfinite(band)]
    return float(np.nanpercentile(finite, 2)), float(np.nanpercentile(finite, 98))


def main() -> None:
    tomawac_uri, artemis_uri = sys.argv[1], sys.argv[2]

    vmin, vmax = _range(tomawac_uri)
    out = render(
        tomawac_uri, str(_OUT / "wave_field_input_bed_bathymetry.png"),
        "Input: lake bed bathymetry (NOAA Great Lakes lake-datum) -- tomawac_wave_field",
        ("S3 -- the NOAA lake-datum bed the TOMAWAC solve sampled IN-WORKER, "
         "surfaced as a role=context Case INPUT (continuous_dem/terrain ramp, EPSG:3857 "
         "over ESRI World Imagery). Lake Superior off Marquette, MI; fetch-growth wind "
         "case. Elevation m below lake datum (negative = lake bottom)."),
        cmap="terrain", units_label="bed elevation (m, lake datum)",
        vmin=vmin, vmax=vmax, zoom=9)
    print("wrote", out, f"vmin={vmin:.1f} vmax={vmax:.1f}")

    vmin, vmax = _range(artemis_uri)
    out = render(
        artemis_uri, str(_OUT / "agitation_input_bed_bathymetry.png"),
        "Input: lake bed bathymetry (NOAA Great Lakes lake-datum) -- artemis_harbor_agitation",
        ("S3 -- the NOAA lake-datum bed the ARTEMIS diffraction solve sampled "
         "IN-WORKER, surfaced as a role=context Case INPUT (continuous_dem/terrain ramp, "
         "EPSG:3857 over ESRI World Imagery). Marquette Lower Harbor, MI. Elevation m "
         "below lake datum (negative = lake bottom)."),
        cmap="terrain", units_label="bed elevation (m, lake datum)",
        vmin=vmin, vmax=vmax, zoom=13)
    print("wrote", out, f"vmin={vmin:.1f} vmax={vmax:.1f}")


if __name__ == "__main__":
    main()
