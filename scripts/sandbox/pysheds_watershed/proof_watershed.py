"""ADR 0193 Part A -- pysheds watershed-coverage proof (STANDALONE sandbox).

Proves the full python-gis-book chapter-12 pysheds workflow on a REAL US 3DEP
DEM, at the Coweeta experimental watershed (Nantahala Mountains, NC -- a canonical
USFS/USGS gauged catchment). Two things are proved together:

  1. the REGISTERED tools on real 3DEP data -- ``delineate_watershed`` (catchment
     polygon) and ``extract_stream_network`` (channel network), driven with a
     3DEP dem_uri override;
  2. the two chapter capabilities NOT in the registered surface, run in the
     code_exec-style PLAYGROUND directly on pysheds:
       - flow-accumulation RASTER (grid.accumulation, rendered as the signature
         chapter visual),
       - distance-to-outlet (grid.distance_to_outlet flow-path length).

3DEP (EPSG:5070) is reprojected to EPSG:4326 so the registered tools' lon/lat
pour-point handling is exact. Render is ESRI World Imagery + flow accumulation +
catchment outline + stream network, white box = AOI only.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  TMPDIR=scripts/sandbox/pysheds_watershed/_work \
  PYTHONPATH=.:contracts/src \
    venvs/agent/bin/python scripts/sandbox/pysheds_watershed/proof_watershed.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.crs import CRS  # noqa: E402
from rasterio.warp import Resampling, calculate_default_transform, reproject  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pysheds_proof")

REPO = Path("/home/nate/Documents/trid3nt-local")
WORK = REPO / "scripts/sandbox/pysheds_watershed/_work"
PROOF = REPO / "docs/proof/templates"

# Coweeta Hydrologic Laboratory basin, Nantahala Mtns, NC (natural place name).
SITE = "Coweeta Creek watershed (Nantahala Mtns, NC)"
BBOX = (-83.50, 35.00, -83.40, 35.09)  # 0.10 x 0.09 deg -- within the 0.3 clamp
# reprojected 3DEP is ~7 m/cell here, so a realistic ~0.25 km^2 channel-
# initiation area is ~5000 cells (keeps the vector network to real main stems,
# not a discretization hairball).
ACC_THRESHOLD = 5000
# flow-accumulation raster is masked below this so only channels glow (chapter
# signature visual), instead of a flat wash over the whole frame.
ACC_RASTER_FLOOR = 400


# --------------------------------------------------------------------------- #
# 3DEP fetch + reproject to 4326                                              #
# --------------------------------------------------------------------------- #
def fetch_3dep_4326() -> Path:
    dem4326 = WORK / "coweeta_dem_4326.tif"
    if dem4326.exists() and dem4326.stat().st_size > 0:
        log.info("cached %s", dem4326)
        return dem4326
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_dem"].fn(bbox=BBOX, source="3dep", resolution_m=10)
    log.info("fetch_dem 3dep -> %s", layer.uri)
    raw = WORK / "coweeta_dem_5070.tif"
    if layer.uri.startswith("s3://"):
        raw.write_bytes(read_object_bytes_s3(layer.uri))
    else:
        raw.write_bytes(Path(layer.uri).read_bytes())

    with rasterio.open(raw) as src:
        dst_crs = CRS.from_epsg(4326)
        transform, w, h = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        prof = src.profile.copy()
        prof.update(crs=dst_crs, transform=transform, width=w, height=h)
        with rasterio.open(dem4326, "w", **prof) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
    log.info("reprojected 3DEP 5070 -> 4326 %dx%d %s", w, h, dem4326)
    return dem4326


# --------------------------------------------------------------------------- #
# pysheds playground: full chapter chain + the two gap capabilities           #
# --------------------------------------------------------------------------- #
def pysheds_playground(dem_path: Path) -> dict:
    """Runs the full chapter-12 chain and returns arrays for rendering + the
    outlet pour point (max-accumulation cell) + gap-capability outputs."""
    from pysheds.grid import Grid

    grid = Grid.from_raster(str(dem_path))
    dem = grid.read_raster(str(dem_path))
    # chapter chain (same order as the registered tools' _condition_dem)
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated, nodata_out=np.int64(0))
    acc = grid.accumulation(fdir, nodata_out=np.float64(0))
    acc_a = np.asarray(acc)

    # outlet = highest-accumulation cell STRICTLY INSIDE the AOI (inset ~4 cells
    # off the DEM rim + inside BBOX), so the upstream catchment is fully
    # contained and not truncated at the frame edge.
    nrows, ncols = acc_a.shape
    cc, rr = np.meshgrid(np.arange(ncols), np.arange(nrows))
    lon_c, lat_c = grid.affine * (cc + 0.5, rr + 0.5)
    inset = 0.004
    interior = (
        (lon_c >= BBOX[0] + inset) & (lon_c <= BBOX[2] - inset)
        & (lat_c >= BBOX[1] + inset) & (lat_c <= BBOX[3] - inset)
    )
    masked = np.where(interior, acc_a, -1.0)
    r, c = np.unravel_index(int(np.argmax(masked)), masked.shape)
    x_out, y_out = grid.affine * (c + 0.5, r + 0.5)
    log.info("interior outlet (acc=%d) at (%.5f, %.5f)", int(acc_a[r, c]), x_out, y_out)

    # GAP CAPABILITY: distance-to-outlet (flow-path length in cells).
    dist = None
    try:
        dist = grid.distance_to_outlet(
            x=float(x_out), y=float(y_out), fdir=fdir,
            xytype="coordinate", nodata_out=np.float64(np.nan),
        )
        d_arr = np.asarray(dist)
        finite = d_arr[np.isfinite(d_arr)]
        log.info("distance_to_outlet ok finite-max=%.1f cells",
                 float(finite.max()) if finite.size else 0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("distance_to_outlet fell back: %s", exc)

    with rasterio.open(dem_path) as ds:
        extent = (ds.bounds.left, ds.bounds.right, ds.bounds.bottom, ds.bounds.top)

    return {
        "acc": acc_a,
        "dist": None if dist is None else np.asarray(dist),
        "outlet": (float(x_out), float(y_out)),
        "extent": extent,
        "affine": grid.affine,
        "acc_max": int(acc_a.max()),
    }


# --------------------------------------------------------------------------- #
# ESRI basemap tiles + overlay renderer                                       #
# --------------------------------------------------------------------------- #
# Tile + Web-Mercator math lives in the oceanmesh sandbox's merc_render (single
# source of truth for every mesh/watershed proof render).
sys.path.insert(0, str(REPO / "scripts/sandbox/oceanmesh"))
from merc_render import fetch_basemap, ll_to_merc, pick_zoom  # noqa: E402


def render(pg, catchment_fc, streams_fc, snapped, area_km2, stream_km, out_path, caption):
    plon = (BBOX[2] - BBOX[0]) * 0.08
    plat = (BBOX[3] - BBOX[1]) * 0.08
    fbox = (BBOX[0] - plon, BBOX[1] - plat, BBOX[2] + plon, BBOX[3] + plat)
    zoom = pick_zoom(fbox, max_tiles=8, zmax=16, fallback=12)
    basemap, (left, right, bottom, top) = fetch_basemap(fbox, zoom, user_agent="trid3nt-pysheds")

    xlo, ylo = ll_to_merc(fbox[0], fbox[1])
    xhi, yhi = ll_to_merc(fbox[2], fbox[3])
    bx0, by0 = ll_to_merc(BBOX[0], BBOX[1])
    bx1, by1 = ll_to_merc(BBOX[2], BBOX[3])

    map_w = 10.0
    aspect = (yhi - ylo) / (xhi - xlo)
    map_h = float(np.clip(map_w * aspect, 4.0, 14.0))
    cap_h = 1.9
    fig = plt.figure(figsize=(map_w, map_h + cap_h))
    fig.patch.set_facecolor("#111111")
    ax = fig.add_axes([0.0, cap_h / (map_h + cap_h), 1.0, map_h / (map_h + cap_h)])
    ax.imshow(np.asarray(basemap), extent=[left, right, bottom, top], origin="upper")

    # flow accumulation (log) as the signature chapter raster, warped to merc extent.
    acc = pg["acc"]
    ex = pg["extent"]  # (lon_left, lon_right, lat_bottom, lat_top)
    mxl, _ = ll_to_merc(ex[0], ex[2])
    mxr, _ = ll_to_merc(ex[1], ex[2])
    _, myb = ll_to_merc(ex[0], ex[2])
    _, myt = ll_to_merc(ex[0], ex[3])
    # mask below the floor so only channels glow (not a flat wash).
    acc_log = np.log10(np.where(acc >= ACC_RASTER_FLOOR, acc, np.nan))
    ax.imshow(acc_log, extent=[mxl, mxr, myb, myt], origin="upper",
              cmap="cool", alpha=0.9, zorder=2)

    # catchment polygon outline (white/yellow).
    from shapely.geometry import shape

    for feat in catchment_fc["features"]:
        geom = shape(feat["geometry"])
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            xs, ys = poly.exterior.xy
            mx, my = ll_to_merc(np.asarray(xs), np.asarray(ys))
            ax.plot(mx, my, color="#ffd400", linewidth=2.6, zorder=4)

    # stream network vector (registered tool) -- bold white main stems.
    for feat in streams_fc["features"]:
        coords = feat["geometry"]["coordinates"]
        arr = np.asarray(coords)
        mx, my = ll_to_merc(arr[:, 0], arr[:, 1])
        ax.plot(mx, my, color="#ffffff", linewidth=1.4, alpha=0.95, zorder=5)

    # snapped pour point (red).
    sx, sy = ll_to_merc(snapped[0], snapped[1])
    ax.scatter([sx], [sy], s=90, marker="v", color="#ff2b2b",
               edgecolor="white", linewidth=1.2, zorder=6)

    # AOI white box.
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", linewidth=2.4, zorder=7)
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"pysheds watershed analysis  -  {SITE}", color="white",
                 fontsize=14, pad=6)

    cap = fig.add_axes([0.0, 0.0, 1.0, cap_h / (map_h + cap_h)])
    cap.axis("off")
    cap.text(0.012, 0.5, caption, fontsize=9.5, family="monospace",
             color="white", va="center", ha="left", transform=cap.transAxes)
    fig.savefig(out_path, dpi=135, facecolor="#111111")
    plt.close(fig)
    log.info("render -> %s", out_path)


# --------------------------------------------------------------------------- #
def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    PROOF.mkdir(parents=True, exist_ok=True)
    outdir = WORK / "layers"
    outdir.mkdir(exist_ok=True)

    dem4326 = fetch_3dep_4326()
    pg = pysheds_playground(dem4326)
    outlet = pg["outlet"]

    from trid3nt_server.agent.tools.processing.delineate_watershed.delineate_watershed import (
        delineate_watershed,
    )
    from trid3nt_server.agent.tools.processing.extract_stream_network.extract_stream_network import (
        extract_stream_network,
    )

    ws = delineate_watershed(
        pour_point=outlet, bbox=BBOX, dem_uri=str(dem4326),
        snap_threshold=200, _output_dir=str(outdir),
    )
    log.info("delineate_watershed: %.3f km^2, %d cells, snapped=%s",
             ws.area_km2, ws.cell_count, ws.snapped_pour_point)
    sn = extract_stream_network(
        bbox=BBOX, accumulation_threshold=ACC_THRESHOLD, dem_uri=str(dem4326),
        _output_dir=str(outdir),
    )
    log.info("extract_stream_network: %d branches, %.2f km",
             sn.segment_count, sn.total_length_km)

    catchment_fc = json.loads(Path(ws.uri).read_text())
    streams_fc = json.loads(Path(sn.uri).read_text())

    dist_txt = "n/a"
    if pg["dist"] is not None:
        d_arr = pg["dist"]
        finite = d_arr[np.isfinite(d_arr)]
        dmax = float(finite.max()) if finite.size else 0.0
        # ~7 m reprojected cell -> km longest flow path within the AOI.
        dist_txt = f"{dmax:.0f} cells (~{dmax * 7 / 1000:.1f} km longest flow path)"

    caption = (
        f"AOI: {SITE}   bbox={tuple(round(v, 3) for v in BBOX)}\n"
        f"DEM: USGS 3DEP 10 m (EPSG:5070 -> reprojected EPSG:4326)   "
        f"engine: pysheds 0.4 D8\n"
        f"REGISTERED tools:  delineate_watershed -> {ws.area_km2:.2f} km^2, "
        f"{ws.cell_count} cells   |   extract_stream_network -> "
        f"{sn.segment_count} branches, {sn.total_length_km:.2f} km "
        f"(>= {ACC_THRESHOLD} cells)\n"
        f"PLAYGROUND gap-caps:  flow-accumulation raster (max={pg['acc_max']} cells, "
        f"cyan overlay)   |   distance-to-outlet {dist_txt}\n"
        f"yellow=catchment  white lines=stream network  cyan=flow-accumulation "
        f"raster  red v=snapped outlet  white box=AOI"
    )
    render(pg, catchment_fc, streams_fc, ws.snapped_pour_point,
           ws.area_km2, sn.total_length_km,
           PROOF / "pysheds_watershed_coweeta.png", caption)

    summary = {
        "site": SITE, "bbox": BBOX, "dem": "USGS 3DEP 10 m",
        "outlet_maxacc": outlet, "snapped": list(ws.snapped_pour_point),
        "watershed_area_km2": ws.area_km2, "watershed_cells": ws.cell_count,
        "stream_branches": sn.segment_count, "stream_km": sn.total_length_km,
        "acc_max_cells": pg["acc_max"],
        "distance_to_outlet_ok": pg["dist"] is not None,
        "catchment_uri": ws.uri, "streams_uri": sn.uri,
        "render": str(PROOF / "pysheds_watershed_coweeta.png"),
    }
    (WORK / "summary.json").write_text(json.dumps(summary, indent=2))
    print("PROOF_SUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
