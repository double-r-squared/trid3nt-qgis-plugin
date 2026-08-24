"""Part B -- WATERSHED-FIRST mesh driver (STANDALONE sandbox).

NATE's watershed-then-mesh method, end to end: delineate the watershed with
pysheds (registered ``delineate_watershed``), pull the NHDPlus HR / OSM river
flowlines inside it, buffer them into a river corridor clipped to the catchment,
and mesh THAT domain with the authentic OceanMesh2D engine (in the GPL-isolated
``trid3nt-local/mesh:latest`` image). The catchment -- not a bbox -- is the
domain, so the mesh is never cookie-cut mid-water; the AOI box is only a residual
render overlay.

Reuses the mesh machinery unchanged (docker mesher, format writers,
MDAL + SERAFIN verification) via build_coastal_mesh + water_edge.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  TMPDIR=scripts/sandbox/oceanmesh/_work \
  PYTHONPATH=.:contracts:workers/schism:scripts/sandbox/oceanmesh \
    venvs/agent/bin/python scripts/sandbox/oceanmesh/build_watershed_mesh.py --case coweeta_river
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watershed_mesh")

REPO = Path("/home/nate/Documents/trid3nt-local")
SANDBOX = REPO / "scripts/sandbox/oceanmesh"
PYSHEDS_WORK = REPO / "scripts/sandbox/pysheds_watershed/_work"
OUT_ROOT = SANDBOX / "_runs"
PROOF_RENDERS = REPO / "docs/proof/templates"
PROOF_MESHES = REPO / "docs/proof/templates/oceanmesh_meshes"
MESH_IMAGE = "trid3nt-local/mesh:latest"

# Watershed-first river cases. The DEM is a real US 3DEP tile reprojected to
# EPSG:4326 (staged by the pysheds proof or fetched here).
CASES = {
    "coweeta_river": {
        "label": "Coweeta Creek river corridor (Nantahala Mtns, NC) -- watershed-first",
        "aoi_bbox": (-83.50, 35.00, -83.40, 35.09),
        "pour_point": (-83.40402, 35.05746),  # interior main-stem outlet (Part A)
        "snap_threshold": 200,
        "buffer_m": 70.0,
        "min_edge_length_m": 40.0,
        "max_edge_length_m": 400.0,
        "grade": 0.20,
        "shoreline_source": "pysheds catchment polygon = the meshing domain; mesh "
        "refined by DISTANCE TO the NHDPlus HR/OSM river network within it",
        "open_boundary_side": "east",
    },
}


def fetch_dem_4326(aoi_bbox, rundir: Path) -> Path:
    """Real US 3DEP DEM reprojected to EPSG:4326 (reuse the pysheds proof stage
    if present, else fetch + reproject here)."""
    cached = PYSHEDS_WORK / "coweeta_dem_4326.tif"
    dst = rundir / "dem.tif"
    if cached.exists() and cached.stat().st_size > 0:
        shutil.copy(cached, dst)
        log.info("reused 3DEP 4326 DEM %s", cached)
        return dst
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_dem"].fn(bbox=aoi_bbox, source="3dep", resolution_m=10)
    raw = rundir / "dem_5070.tif"
    raw.write_bytes(read_object_bytes_s3(layer.uri) if layer.uri.startswith("s3://")
                    else Path(layer.uri).read_bytes())
    with rasterio.open(raw) as src:
        dst_crs = CRS.from_epsg(4326)
        transform, w, h = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        prof = src.profile.copy()
        prof.update(crs=dst_crs, transform=transform, width=w, height=h)
        with rasterio.open(dst, "w", **prof) as d:
            reproject(source=rasterio.band(src, 1), destination=rasterio.band(d, 1),
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=transform, dst_crs=dst_crs,
                      resampling=Resampling.bilinear)
    log.info("fetched+reprojected 3DEP DEM -> %s", dst)
    return dst


def delineate(cfg, dem_path: Path, rundir: Path):
    from trid3nt_server.tools.processing.delineate_watershed.delineate_watershed import (
        delineate_watershed,
    )
    ws = delineate_watershed(
        pour_point=cfg["pour_point"], bbox=cfg["aoi_bbox"], dem_uri=str(dem_path),
        snap_threshold=cfg["snap_threshold"], _output_dir=str(rundir),
    )
    from shapely.geometry import shape
    from shapely.ops import unary_union

    fc = json.loads(Path(ws.uri).read_text())
    catch = unary_union([shape(f["geometry"]) for f in fc["features"]])
    log.info("catchment %.2f km^2, %d cells, snapped=%s",
             ws.area_km2, ws.cell_count, ws.snapped_pour_point)
    return ws, catch


def fetch_flowlines(aoi_bbox, rundir: Path):
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    rv = TOOL_REGISTRY["fetch_river_geometry"].fn(bbox=tuple(aoi_bbox), source="nhdplus_hr")
    p = rundir / "flowlines.fgb"
    p.write_bytes(read_object_bytes_s3(rv.uri) if rv.uri.startswith("s3://")
                  else Path(rv.uri).read_bytes())
    g = gpd.read_file(p)
    log.info("flowlines: %d feats, %.1f km",
             len(g), float(g.geometry.to_crs(3857).length.sum() / 1e3))
    return g


def build_watershed_mesh(case: str) -> dict:
    sys.path.insert(0, str(REPO / "workers/schism"))
    sys.path.insert(0, str(SANDBOX))
    from build_coastal_mesh import sample_elevation, sg_docker, verify_mdal, verify_serafin
    from mesh_formats import mesh_quality_report, write_2dm, write_fort14
    from schism_gr3 import tin_to_hgrid
    from selafin_io import write_selafin
    from water_edge import river_corridor_water

    cfg = CASES[case]
    rundir = OUT_ROOT / case
    rundir.mkdir(parents=True, exist_ok=True)
    PROOF_MESHES.mkdir(parents=True, exist_ok=True)
    log.info("=== WATERSHED-FIRST case %s ===", case)

    dem_path = fetch_dem_4326(cfg["aoi_bbox"], rundir)
    ws, catch = delineate(cfg, dem_path, rundir)
    flow = fetch_flowlines(cfg["aoi_bbox"], rundir)
    # The WATERSHED-FIRST domain is the whole (connected) catchment polygon --
    # meshed as one body so the AOI box never truncates it, and thin river
    # ribbons are not destroyed by oceanmesh's disconnected-area cleanup. The
    # river corridor is computed only to report its extent.
    corridor = river_corridor_water(flow, catch, buffer_m=cfg["buffer_m"])
    corridor_km2 = float(gpd.GeoSeries([corridor], crs=4326).to_crs(3857).area.iloc[0] / 1e6)
    water = catch
    log.info("meshing full catchment %.2f km^2 (river corridor %.2f km^2)",
             ws.area_km2, corridor_km2)

    dom = catch.bounds  # domain = catchment bounds (NOT the AOI box)

    # Catchment exterior = the oceanmesh domain polygon (mesh its interior).
    from shapely.geometry import MultiPolygon
    largest = max(catch.geoms, key=lambda p: p.area) if isinstance(catch, MultiPolygon) else catch
    ext = largest.simplify(cfg["min_edge_length_m"] / 111_320.0).exterior
    boubox_coords = [[float(x), float(y)] for x, y in ext.coords]

    # River-network vertices INSIDE the catchment -> the distance-to-river sizing
    # source (fine mesh along the valleys, coarse on the ridges).
    river_coords: list[list[float]] = []
    for geom in flow.geometry:
        if geom is None or geom.is_empty:
            continue
        clipped = geom.intersection(catch)
        if clipped.is_empty:
            continue
        lines = clipped.geoms if clipped.geom_type in ("MultiLineString", "GeometryCollection") else [clipped]
        for ln in lines:
            if getattr(ln, "geom_type", "") != "LineString":
                continue
            river_coords.extend([[float(x), float(y)] for x, y in ln.coords])
    log.info("boubox %d verts; river sizing points %d", len(boubox_coords), len(river_coords))

    conf = {
        "boubox_coords": boubox_coords,
        "river_coords": river_coords,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "grade": cfg["grade"], "max_iter": 60,
    }
    (rundir / "mesh_config.json").write_text(json.dumps(conf))
    cp = sg_docker([
        "run", "--rm", "-v", f"{SANDBOX}:/sandbox", "-v", f"{rundir}:/data",
        "--entrypoint", "python", MESH_IMAGE,
        "/sandbox/_mesh_watershed_incontainer.py", "/data/mesh_config.json", "/data",
    ])
    if cp.returncode != 0 or not (rundir / "coastal_tin_mesh.npz").exists():
        raise RuntimeError(f"mesh worker failed:\n{cp.stdout[-2500:]}\n{cp.stderr[-2500:]}")
    stats = json.loads((rundir / "mesh_stats.json").read_text())
    log.info("mesh: %s", json.dumps(stats))

    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    elevation = sample_elevation(dem_path, points)
    depth_down = -elevation
    qa = mesh_quality_report(points, cells)
    log.info("QA: %s", json.dumps(qa))

    dst2dm = PROOF_MESHES / f"{case}.2dm"
    dstslf = PROOF_MESHES / f"{case}.slf"
    dstgr3 = PROOF_MESHES / f"{case}_hgrid.gr3"
    dstf14 = PROOF_MESHES / f"{case}.fort.14"
    dst2dm.write_text(write_2dm(points, cells, z=elevation))
    write_selafin(dstslf, points, cells, elevation)
    dstgr3.write_text(tin_to_hgrid(points, cells, depth=depth_down,
                                   grid_name=f"trid3nt_{case}",
                                   open_boundary_side=cfg["open_boundary_side"]))
    dstf14.write_text(write_fort14(points, cells, depths=depth_down,
                                   grid_name=f"trid3nt_{case}",
                                   open_boundary_side=cfg["open_boundary_side"]))

    mdal_2dm = verify_mdal(dst2dm)
    mdal_slf = verify_mdal(dstslf)
    serafin = verify_serafin(dstslf, PROOF_MESHES)
    log.info("MDAL 2dm=%s slf=%s", mdal_2dm, mdal_slf)
    log.info("SERAFIN=%s", serafin)

    caption = (
        f"WATERSHED-FIRST: {cfg['label']}\n"
        f"domain = pysheds catchment {ws.area_km2:.1f} km^2 (NOT the AOI box); "
        f"river corridor within it {corridor_km2:.2f} km^2\n"
        f"domain/sizing: {cfg['shoreline_source']}\n"
        f"engine: {stats['engine']}   catchment-interior SDF + distance-to-river "
        f"sizing; grade g={cfg['grade']}\n"
        f"nodes={qa['n_vertices']} elements={qa['n_elements']} "
        f"inverted={qa['inverted_elements']} closed={qa['boundary_closed']}   "
        f"resolution {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   qE min={qa['min_quality_qE']} "
        f"median={qa['median_quality_qE']}"
    )
    render_path = PROOF_RENDERS / f"oceanmesh_standalone_{case}.png"
    _render_watershed(points, cells, catch, cfg["aoi_bbox"], render_path,
                      cfg["label"], caption)

    summary = {
        "case": case, "label": cfg["label"], "method": "watershed-first",
        "aoi_bbox": list(cfg["aoi_bbox"]), "catchment_km2": ws.area_km2,
        "corridor_km2": round(corridor_km2, 3),
        "shoreline_source": cfg["shoreline_source"],
        "mesh_stats": stats, "qa": qa,
        "mdal_2dm": mdal_2dm, "mdal_slf": mdal_slf, "serafin": serafin,
        "files": {"twodm": str(dst2dm), "slf": str(dstslf), "gr3": str(dstgr3),
                  "fort14": str(dstf14), "render": str(render_path)},
    }
    (rundir / "summary.json").write_text(json.dumps(summary, indent=2))
    print("WATERSHED_MESH_SUMMARY " + json.dumps(summary))
    return summary


# --------------------------------------------------------------------------- #
# render: mesh (cyan) + catchment (yellow) + AOI residual box (white) on ESRI  #
# Tile + Web-Mercator math lives in merc_render (single source of truth).      #
# --------------------------------------------------------------------------- #
def _render_watershed(points, cells, catch, aoi_bbox, out_path, aoi_name, caption):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from merc_render import fetch_basemap, ll_to_merc, pick_zoom

    points = np.asarray(points, float)
    cells = np.asarray(cells, np.int64)
    minx, miny, maxx, maxy = catch.bounds
    plon, plat = (maxx - minx) * 0.08, (maxy - miny) * 0.08
    fbox = (minx - plon, miny - plat, maxx + plon, maxy + plat)
    zoom = pick_zoom(fbox, max_tiles=8, zmax=16, fallback=12)
    basemap, (left, right, bottom, top) = fetch_basemap(fbox, zoom)

    # frame to the fetched basemap bounds (no white letterbox bands).
    xlo, xhi, ylo, yhi = left, right, bottom, top
    mx, my = ll_to_merc(points[:, 0], points[:, 1])
    ax0, ay0 = ll_to_merc(aoi_bbox[0], aoi_bbox[1])
    ax1, ay1 = ll_to_merc(aoi_bbox[2], aoi_bbox[3])

    map_w = 10.0
    aspect = (yhi - ylo) / (xhi - xlo)
    map_h = float(np.clip(map_w * aspect, 4.0, 14.0))
    cap_h = 2.0
    fig = plt.figure(figsize=(map_w, map_h + cap_h))
    fig.patch.set_facecolor("#111111")
    ax = fig.add_axes([0.0, cap_h / (map_h + cap_h), 1.0, map_h / (map_h + cap_h)])
    ax.imshow(np.asarray(basemap), extent=[left, right, bottom, top], origin="upper")

    polys = catch.geoms if catch.geom_type == "MultiPolygon" else [catch]
    for poly in polys:
        xs, ys = poly.exterior.xy
        cx, cy = ll_to_merc(np.asarray(xs), np.asarray(ys))
        ax.plot(cx, cy, color="#ffd400", linewidth=2.4, zorder=4)
    ax.triplot(mx, my, cells, color="#00e5ff", linewidth=0.45, alpha=0.95, zorder=5)
    ax.plot([ax0, ax1, ax1, ax0, ax0], [ay0, ay0, ay1, ay1, ay0],
            color="white", linewidth=2.0, linestyle="--", zorder=6)
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"Watershed-first OceanMesh2D  -  {aoi_name}", color="white",
                 fontsize=13, pad=6)

    cap = fig.add_axes([0.0, 0.0, 1.0, cap_h / (map_h + cap_h)])
    cap.axis("off")
    cap.text(0.012, 0.5, caption + "\nyellow=catchment  cyan=mesh  "
             "white dashed=residual AOI box (does NOT truncate the mesh)",
             fontsize=9, family="monospace", color="white", va="center",
             ha="left", transform=cap.transAxes)
    fig.savefig(out_path, dpi=135, facecolor="#111111")
    plt.close(fig)
    log.info("render -> %s", out_path)


def rerender(case: str) -> None:
    """Re-render the proof image from the cached mesh (no Docker re-mesh).

    The catchment polygon is re-derived from the cached DEM via the registered
    delineate tool (offline); everything else is read from the cached npz + summary.
    """
    sys.path.insert(0, str(SANDBOX))
    cfg = CASES[case]
    rundir = OUT_ROOT / case
    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    summ = json.loads((rundir / "summary.json").read_text())
    dem_path = fetch_dem_4326(cfg["aoi_bbox"], rundir)
    _, catch = delineate(cfg, dem_path, rundir)
    qa, stats = summ["qa"], summ["mesh_stats"]
    caption = (
        f"WATERSHED-FIRST: {cfg['label']}\n"
        f"domain = pysheds catchment {summ['catchment_km2']:.1f} km^2 (NOT the AOI "
        f"box); river corridor within it {summ['corridor_km2']:.2f} km^2\n"
        f"domain/sizing: {cfg['shoreline_source']}\n"
        f"engine: {stats['engine']}   catchment-interior SDF + distance-to-river "
        f"sizing; grade g={cfg['grade']}\n"
        f"nodes={qa['n_vertices']} elements={qa['n_elements']} "
        f"inverted={qa['inverted_elements']} closed={qa['boundary_closed']}   "
        f"resolution {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   qE min={qa['min_quality_qE']} "
        f"median={qa['median_quality_qE']}"
    )
    _render_watershed(points, cells, catch, cfg["aoi_bbox"],
                      PROOF_RENDERS / f"oceanmesh_standalone_{case}.png",
                      cfg["label"], caption)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=list(CASES) + ["all"], default="coweeta_river")
    ap.add_argument("--render-only", action="store_true",
                    help="re-render the proof image from the cached mesh (no re-mesh)")
    args = ap.parse_args(argv)
    cases = list(CASES) if args.case == "all" else [args.case]
    for c in cases:
        if args.render_only:
            rerender(c)
        else:
            build_watershed_mesh(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
