#!/usr/bin/env python3
# Host-side embankment: raise a full-width y-band in an authored synthetic Terrain.tif
# to ridge_elev (valid cells only; NoData untouched). Reads the band from the driver's
# culvert_probe.json so the ridge stays in sync with the authored barrel geometry.
import sys, json
import rasterio
import numpy as np

case_dir = sys.argv[1]
probe = json.load(open(f"{case_dir}/culvert_probe.json"))
y0, y1, elev = probe["ridge_y0"], probe["ridge_y1"], probe["ridge_elev"]
tif = f"{case_dir}/Terrains/Terrain.tif"

with rasterio.open(tif, "r+") as d:
    a = d.read(1)
    nd = d.nodata if d.nodata is not None else -9999.0
    valid = a != nd
    # world-y of each row centre
    rows = np.arange(d.height)
    ys = d.transform.f + (rows + 0.5) * d.transform.e  # e is negative
    band_rows = (ys >= y0) & (ys <= y1)
    mask = np.zeros_like(a, dtype=bool)
    mask[band_rows, :] = True
    mask &= valid
    a[mask] = elev
    d.write(a, 1)
    print(f"[ridge] {tif}: raised {int(mask.sum())} px in y=[{y0},{y1}] to {elev} m")
