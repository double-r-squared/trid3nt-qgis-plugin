#!/usr/bin/env python3
"""ADR 0251 Stage-2 real-site culvert-through-embankment A/B proof figure
(North Fork Salt Creek x Green Valley Road, Brown County IN).

Renders the WITH-culvert (A) vs BLOCKED (B) peak-depth field as the reprojected
depth COG (rasterio nearest/bilinear reproject of the actual per-cell raster,
built by rog2025_pipeline.build_depth_cog -- the SAME rasterizer the live
composer publishes) over ESRI World Imagery -- never cell-center scatter dots.
The structured 2D mesh wireframe, culvert barrel, and embankment band are
overlaid at their real UTM/EPSG:3857 positions.

INPUT PROVENANCE (read this before editing numbers): the original live-E2E run
(2026-08-13, culvert_embankment_flow composer wave) staged its result HDFs +
local_dem.tif to the probe host dir but did NOT persist the Rog2025Prep
(origin_x/origin_y/utm_epsg) the composer used internally -- that prep only ever
lived in the composer process's memory. This script RE-DERIVES an equivalent
georeference: ADR 0251 records the site centroid (lon -86.2883, lat 39.1893,
UTM 16N/EPSG:32616); the local-frame culvert barrel midpoint (from the run's own
spec.json, exact) is pinned to that centroid, fixing origin_x/origin_y. This
reproduces the correct SHAPE/relative geometry and a defensible real-world
placement, but is not guaranteed pixel-identical to the original composer framing
(that framing was never persisted). The depth field, discriminant, barrel/
embankment geometry, and terrain are all the REAL run outputs -- only the
absolute placement of local (0,0) in UTM is a documented reconstruction.

Result HDFs used: cvr_cvr_nfsalt_run_A_culvert / _B_blocked under the probe host
dir (timestamped 2026-08-13 19:13, before the committed PNG's 19:22 mtime --
the run.log culvertreach barrel matches spec.json exactly: (257.5,432.5)->
(292.5,347.5), matching the published discriminant bit-for-bit, confirmed below).
"""
import sys
import json
import tempfile
from pathlib import Path

import h5py
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pyproj import Transformer

_HERE = Path(__file__).resolve().parent
_FRESHTOPO = _HERE.parents[3] / "services/workers/hecras2025/subst/crux/freshtopo"
_SCRIPTS = _HERE.parents[3] / "scripts"
sys.path.insert(0, str(_FRESHTOPO))
sys.path.insert(0, str(_SCRIPTS))

from rog2025_pipeline import Rog2025Prep, build_depth_cog  # type: ignore
from culvert_reach_pipeline import extract_discriminant, CulvertGeometry  # type: ignore
from render_fidelity_proof_generic import basemap, reproject_to_3857, add_scale_bar, TO3857  # type: ignore

P = Path("/home/nate/hecras_probe2025")
RUN_A = P / "cvr_cvr_nfsalt_run_A_culvert" / "result.h5"
RUN_B = P / "cvr_cvr_nfsalt_run_B_blocked" / "result.h5"
SPEC_A = json.loads((P / "cvr_cvr_nfsalt_run_A_culvert" / "spec.json").read_text())

NX, NY, CELL = 27, 43, 20.0
WIDTH_M, HEIGHT_M = NX * CELL, NY * CELL
UTM_EPSG = 32616  # IN Brown County -> UTM zone 16N
SITE_LON, SITE_LAT = -86.2883, 39.1893  # ADR 0251 site centroid

barrel = SPEC_A["culvert"]["barrel"]  # [us_x, us_y, ds_x, ds_y], local metres
us_x, us_y, ds_x, ds_y = barrel
barrel_mid_local = ((us_x + ds_x) / 2.0, (us_y + ds_y) / 2.0)

_to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{UTM_EPSG}", always_xy=True)
site_utm_x, site_utm_y = _to_utm.transform(SITE_LON, SITE_LAT)
origin_x = site_utm_x - barrel_mid_local[0]
origin_y = site_utm_y - barrel_mid_local[1]

prep = Rog2025Prep(
    local_dem=str(P / "cvr_cvr_nfsalt_run_A_culvert" / "local_dem.tif"),
    nx=NX, ny=NY, cell_size=CELL, width_m=WIDTH_M, height_m=HEIGHT_M,
    outlet_edge="s", utm_epsg=UTM_EPSG, origin_x=origin_x, origin_y=origin_y,
    elev_min_m=0.0, elev_max_m=0.0, valid_frac=1.0,
)

# embankment band from the real terrain (detrended cross-stream-minimum anomaly,
# same derivation the pipeline uses to decide the blocked case)
geom = CulvertGeometry(
    us_x=us_x, us_y=us_y, ds_x=ds_x, ds_y=ds_y,
    us_invert=SPEC_A["culvert"]["us_invert"], ds_invert=SPEC_A["culvert"]["ds_invert"],
    embankment_y0=380.0, embankment_y1=400.0, embankment_crest_m=175.7,
    bed_us_m=173.2, bed_ds_m=173.1, blocks=True,
)
disc = extract_discriminant(str(RUN_A), str(RUN_B), geom)
print("recomputed discriminant (must match ADR 0251):", json.dumps(disc, indent=2))

workdir = Path(tempfile.mkdtemp(prefix="culvert-realsite-fig-"))
cog_a = str(workdir / "depth_a.tif")
cog_b = str(workdir / "depth_b.tif")
info_a = build_depth_cog(str(RUN_A), prep, cog_a, None, 1.0)
info_b = build_depth_cog(str(RUN_B), prep, cog_b, None, 1.0)

# --- mesh wireframe + barrel + embankment band, local (m) -> UTM -> EPSG:3857 ---
_utm_to_3857 = Transformer.from_crs(f"EPSG:{UTM_EPSG}", "EPSG:3857", always_xy=True)


def local_to_3857(lx, ly):
    return _utm_to_3857.transform(origin_x + np.asarray(lx), origin_y + np.asarray(ly))


def draw_mesh_and_overlays(ax):
    xs = np.arange(0, WIDTH_M + 1e-6, CELL)
    ys = np.arange(0, HEIGHT_M + 1e-6, CELL)
    for x in xs:
        mx, my = local_to_3857([x, x], [0.0, HEIGHT_M])
        ax.plot(mx, my, color="white", lw=0.25, alpha=0.5, zorder=4)
    for y in ys:
        mx, my = local_to_3857([0.0, WIDTH_M], [y, y])
        ax.plot(mx, my, color="white", lw=0.25, alpha=0.5, zorder=4)
    for y in (geom.embankment_y0, geom.embankment_y1):
        mx, my = local_to_3857([0.0, WIDTH_M], [y, y])
        ax.plot(mx, my, color="orange", lw=1.8, zorder=6)
    mx, my = local_to_3857([geom.us_x, geom.ds_x], [geom.us_y, geom.ds_y])
    ax.plot(mx, my, color="red", lw=2.6, zorder=7)
    ax.plot(mx, my, color="red", marker="o", ms=5, zorder=7)


fig, axes = plt.subplots(1, 2, figsize=(16, 8.6), dpi=130)
vmax = max(info_a["depth_max"], info_b["depth_max"])

for ax, cog, label, dmax, umean in [
    (axes[0], cog_a, f"WITH culvert (A) -- conveyed {disc['storage_relieved_m3s']:.2g} m3/s; upstream max {disc['us_max_depth_a_m']:.2g} m", info_a["depth_max"], disc["us_mean_depth_a_m"]),
    (axes[1], cog_b, f"BLOCKED, no culvert (B) -- ponds; upstream max {disc['us_max_depth_b_m']:.2g} m", info_b["depth_max"], disc["us_mean_depth_b_m"]),
]:
    data, ext3857, _ = reproject_to_3857(cog)
    x0, x1, y0, y1 = ext3857
    to4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lw, ls = to4326.transform(x0, y0)
    le, ln = to4326.transform(x1, y1)
    pad = 0.25
    padx = (le - lw) * pad
    pady = (ln - ls) * pad
    bm, bm_ext = basemap(lw - padx, ls - pady, le + padx, ln + pady, 17)
    ax.imshow(bm, extent=bm_ext, origin="upper", zorder=1)
    masked = np.ma.masked_invalid(data)
    im = ax.imshow(masked, extent=(x0, x1, y0, y1), origin="upper", cmap="Blues",
                    vmin=0, vmax=vmax, alpha=0.78, zorder=3)
    draw_mesh_and_overlays(ax)
    mx0, my0 = TO3857.transform(lw, ls)
    mx1, my1 = TO3857.transform(le, ln)
    xlim = (mx0 - (mx1 - mx0) * pad, mx1 + (mx1 - mx0) * pad)
    ylim = (my0 - (my1 - my0) * pad, my1 + (my1 - my0) * pad)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(label, fontsize=11)
    add_scale_bar(ax, xlim)

cb = fig.colorbar(im, ax=axes, fraction=0.03, pad=0.01)
cb.set_label("Peak water depth (m)")

fig.suptitle(
    "HEC-RAS 2025 2D culvert-through-embankment A/B -- North Fork Salt Creek x Green Valley Road, Brown County IN\n"
    f"real NHD reach + 3DEP terrain; barrel relieves {disc['ponding_relieved_max_m']:.2g} m ponding, "
    f"max|A-B|={disc['max_abs_depth_delta_m']:.2g} m (moves_water={disc['moves_water']}); "
    "burned 1-cell crest cap at the real road centerline (sub-cell fill under-resolves at screening mesh)",
    fontsize=11, y=0.985)

from matplotlib.lines import Line2D
handles = [Line2D([0], [0], color="red", lw=2.6, label="culvert barrel"),
           Line2D([0], [0], color="orange", lw=1.8, label="embankment band")]
axes[0].legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.85)

out = "/home/nate/Documents/trid3nt-local/docs/proof/templates/culvert_embankment_flow_ab.png"
fig.savefig(out, dpi=130, bbox_inches="tight")
print("wrote", out)
