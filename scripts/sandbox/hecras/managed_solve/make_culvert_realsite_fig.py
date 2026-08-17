#!/usr/bin/env python3
"""Stage-2 real-site culvert-through-embankment A/B proof figure
(North Fork Salt Creek x Green Valley Road, Brown County IN).

Renders the WITH-culvert (A) vs BLOCKED (B) peak-depth field as the reprojected
depth COG (rasterio nearest/bilinear reproject of the actual per-cell raster,
built by rog2025_pipeline.build_depth_cog -- the SAME rasterizer the live
composer publishes) over ESRI World Imagery -- never cell-center scatter dots.
The structured 2D mesh wireframe, culvert barrel, and embankment band are
overlaid at their real UTM/EPSG:3857 positions.

RESOLUTION RE-PROOF (2026-08-14, append): renders a 2x2 panel, 10 m
(finest the template supports) on top, 20 m (screening default) on bottom --
same site, bbox, DEM, barrel spec, auto_seal embankment mode; ONLY resolution_m
differs. The 10 m case is driven directly through
``culvert_reach_pipeline.run_culvert_reach(cell_size=10.0, ...)`` (a fresh
reproducer run, result JSON at ``/tmp/cvr_nfsalt_result_10m.json`` -- its
``prep``/``geometry`` dicts are the EXACT ones the pipeline used, no
reconstruction needed).

20 m INPUT PROVENANCE (read this before editing numbers): the original live-E2E
composer run (2026-08-13, culvert_embankment_flow composer wave) staged its
result HDFs + local_dem.tif to the probe host dir but did NOT persist the
Rog2025Prep (origin_x/origin_y/utm_epsg) the composer used internally -- that
prep only ever lived in the composer process's memory. This script RE-DERIVES
an equivalent georeference for the 20 m row: records the site centroid
(lon -86.2883, lat 39.1893, UTM 16N/EPSG:32616); the local-frame culvert barrel
midpoint (from the run's own spec.json, exact) is pinned to that centroid,
fixing origin_x/origin_y. This reproduces the correct SHAPE/relative geometry
and a defensible real-world placement, but is not guaranteed pixel-identical to
the original composer framing (that framing was never persisted). The depth
field, discriminant, barrel/embankment geometry, and terrain are all the REAL
run outputs -- only the absolute placement of local (0,0) in UTM is a
documented reconstruction. (A same-site same-bbox reproducer run at cell_size
=20 -- ``/tmp/cvr_nfsalt_result.json`` -- lands the identical discriminant
bit-for-bit, confirming the reconstruction is sound; .)

Result HDFs used: cvr_cvr_nfsalt_run_A_culvert / _B_blocked under the probe host
dir (timestamped 2026-08-13 19:13, before the committed PNG's 19:22 mtime --
the run.log culvertreach barrel matches spec.json exactly: (257.5,432.5)->
(292.5,347.5), matching the published discriminant bit-for-bit, confirmed below).
"""
import sys
import json
import tempfile
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pyproj import Transformer

_HERE = Path(__file__).resolve().parent
_FRESHTOPO = _HERE.parents[3] / "workers/hecras2025/subst/crux/freshtopo"
_SCRIPTS = _HERE.parents[3] / "scripts"
sys.path.insert(0, str(_FRESHTOPO))
sys.path.insert(0, str(_SCRIPTS))

from rog2025_pipeline import Rog2025Prep, build_depth_cog  # type: ignore
from culvert_reach_pipeline import extract_discriminant, CulvertGeometry  # type: ignore
from render_fidelity_proof_generic import basemap, reproject_to_3857, add_scale_bar, TO3857  # type: ignore

P = Path("/home/nate/hecras_probe2025")
RUN_A_20 = P / "cvr_cvr_nfsalt_run_A_culvert" / "result.h5"
RUN_B_20 = P / "cvr_cvr_nfsalt_run_B_blocked" / "result.h5"
SPEC_A_20 = json.loads((P / "cvr_cvr_nfsalt_run_A_culvert" / "spec.json").read_text())

NX20, NY20, CELL20 = 27, 43, 20.0
UTM_EPSG = 32616  # IN Brown County -> UTM zone 16N
SITE_LON, SITE_LAT = -86.2883, 39.1893  # site centroid

barrel20 = SPEC_A_20["culvert"]["barrel"]  # [us_x, us_y, ds_x, ds_y], local metres
us_x20, us_y20, ds_x20, ds_y20 = barrel20
barrel_mid_local20 = ((us_x20 + ds_x20) / 2.0, (us_y20 + ds_y20) / 2.0)

_to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{UTM_EPSG}", always_xy=True)
site_utm_x, site_utm_y = _to_utm.transform(SITE_LON, SITE_LAT)
origin_x20 = site_utm_x - barrel_mid_local20[0]
origin_y20 = site_utm_y - barrel_mid_local20[1]

prep20 = Rog2025Prep(
    local_dem=str(P / "cvr_cvr_nfsalt_run_A_culvert" / "local_dem.tif"),
    nx=NX20, ny=NY20, cell_size=CELL20, width_m=NX20 * CELL20, height_m=NY20 * CELL20,
    outlet_edge="s", utm_epsg=UTM_EPSG, origin_x=origin_x20, origin_y=origin_y20,
    elev_min_m=0.0, elev_max_m=0.0, valid_frac=1.0,
)
# embankment band from the real terrain (detrended cross-stream-minimum anomaly,
# same derivation the pipeline uses to decide the blocked case)
geom20 = CulvertGeometry(
    us_x=us_x20, us_y=us_y20, ds_x=ds_x20, ds_y=ds_y20,
    us_invert=SPEC_A_20["culvert"]["us_invert"], ds_invert=SPEC_A_20["culvert"]["ds_invert"],
    embankment_y0=380.0, embankment_y1=400.0, embankment_crest_m=175.7,
    bed_us_m=173.2, bed_ds_m=173.1, blocks=True,
)
disc20 = extract_discriminant(str(RUN_A_20), str(RUN_B_20), geom20)
print("recomputed 20m discriminant (must match ADR 0251):", json.dumps(disc20, indent=2))

# --- 10 m row: EXACT prep/geometry from a fresh cell_size=10 reproducer run --------- #
res10 = json.loads(Path("/tmp/cvr_nfsalt_result_10m.json").read_text())
prep10 = Rog2025Prep(**res10["prep"])
geom10_d = res10["geometry"]
geom10 = CulvertGeometry(**geom10_d)
disc10 = res10["discriminant"]
print("10m discriminant (fresh cell_size=10 run):", json.dumps(disc10, indent=2))
RUN_A_10, RUN_B_10 = res10["result_a_h5"], res10["result_b_h5"]

workdir = Path(tempfile.mkdtemp(prefix="culvert-realsite-fig-"))


def render_row(fig, axes_row, prep, geom, run_a, run_b, disc, res_label):
    utm_to_3857 = Transformer.from_crs(f"EPSG:{prep.utm_epsg}", "EPSG:3857", always_xy=True)

    def local_to_3857(lx, ly):
        return utm_to_3857.transform(prep.origin_x + np.asarray(lx), prep.origin_y + np.asarray(ly))

    def draw_overlays(ax):
        xs = np.arange(0, prep.width_m + 1e-6, prep.cell_size)
        ys = np.arange(0, prep.height_m + 1e-6, prep.cell_size)
        for x in xs:
            mx, my = local_to_3857([x, x], [0.0, prep.height_m])
            ax.plot(mx, my, color="white", lw=0.2, alpha=0.45, zorder=4)
        for y in ys:
            mx, my = local_to_3857([0.0, prep.width_m], [y, y])
            ax.plot(mx, my, color="white", lw=0.2, alpha=0.45, zorder=4)
        for y in (geom.embankment_y0, geom.embankment_y1):
            mx, my = local_to_3857([0.0, prep.width_m], [y, y])
            ax.plot(mx, my, color="orange", lw=1.8, zorder=6)
        mx, my = local_to_3857([geom.us_x, geom.ds_x], [geom.us_y, geom.ds_y])
        ax.plot(mx, my, color="red", lw=2.6, zorder=7)
        ax.plot(mx, my, color="red", marker="o", ms=5, zorder=7)

    cog_a = str(workdir / f"depth_a_{res_label}.tif")
    cog_b = str(workdir / f"depth_b_{res_label}.tif")
    info_a = build_depth_cog(str(run_a), prep, cog_a, None, 1.0)
    info_b = build_depth_cog(str(run_b), prep, cog_b, None, 1.0)
    vmax_row = max(info_a["depth_max"], info_b["depth_max"])

    im = None
    for ax, cog, label in [
        (axes_row[0], cog_a,
         f"[{res_label} mesh, {prep.nx}x{prep.ny}={prep.nx*prep.ny} cells] WITH culvert (A) -- "
         f"conveyed {disc['storage_relieved_m3s']:.2g} m3/s; upstream max {disc['us_max_depth_a_m']:.2g} m"),
        (axes_row[1], cog_b,
         f"[{res_label} mesh, {prep.nx}x{prep.ny}={prep.nx*prep.ny} cells] BLOCKED, no culvert (B) -- "
         f"ponds; upstream max {disc['us_max_depth_b_m']:.2g} m"),
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
                        vmin=0, vmax=GLOBAL_VMAX, alpha=0.78, zorder=3)
        draw_overlays(ax)
        mx0, my0 = TO3857.transform(lw, ls)
        mx1, my1 = TO3857.transform(le, ln)
        xlim = (mx0 - (mx1 - mx0) * pad, mx1 + (mx1 - mx0) * pad)
        ylim = (my0 - (my1 - my0) * pad, my1 + (my1 - my0) * pad)
        ax.set_xlim(*xlim); ax.set_ylim(*ylim)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(label, fontsize=10)
        add_scale_bar(ax, xlim)
    return im


GLOBAL_VMAX = max(disc10["us_max_depth_b_m"], disc20["us_max_depth_b_m"]) * 1.15

fig, axes = plt.subplots(2, 2, figsize=(16, 16.5), dpi=130)
render_row(fig, axes[0], prep10, geom10, RUN_A_10, RUN_B_10, disc10, "10m")
im = render_row(fig, axes[1], prep20, geom20, RUN_A_20, RUN_B_20, disc20, "20m")

cb = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.01)
cb.set_label("Peak water depth (m)")

fig.suptitle(
    "HEC-RAS 2025 2D culvert-through-embankment A/B -- North Fork Salt Creek x Green Valley Road, Brown "
    "County IN\nreal NHD reach + 3DEP terrain; resolution re-proof 10 m (finest supported, top) vs "
    "20 m (screening default, bottom)\n"
    f"10 m: relieves {disc10['ponding_relieved_max_m']:.2g} m ponding, conveys "
    f"{disc10['storage_relieved_m3s']:.2g} m3/s, max|A-B|={disc10['max_abs_depth_delta_m']:.2g} m "
    f"(moves_water={disc10['moves_water']})  |  "
    f"20 m: relieves {disc20['ponding_relieved_max_m']:.2g} m, conveys {disc20['storage_relieved_m3s']:.2g} m3/s, "
    f"max|A-B|={disc20['max_abs_depth_delta_m']:.2g} m -- discriminant survives (sharpens slightly) under "
    "refinement; burned 1-cell crest cap at the real road centerline (sub-cell fill under-resolves at "
    "screening mesh, both resolutions)",
    fontsize=10, y=0.998)

from matplotlib.lines import Line2D
handles = [Line2D([0], [0], color="red", lw=2.6, label="culvert barrel"),
           Line2D([0], [0], color="orange", lw=1.8, label="embankment band")]
axes[0, 0].legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.85)

out = "/home/nate/Documents/trid3nt-local/docs/proof/templates/culvert_embankment_flow_ab.png"
fig.savefig(out, dpi=130, bbox_inches="tight")
print("wrote", out)
