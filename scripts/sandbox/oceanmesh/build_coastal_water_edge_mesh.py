"""ADR 0194 -- coastal water-edge RE-MESH driver (STANDALONE sandbox).

NATE's alignment directive for the estuary cases: the v1 GSHHG-intermediate
shoreline is too coarse ("close but not really" aligned to the river). This
driver re-meshes Delaware Bay and Tampa Bay against a HIGH-RES coastal water edge
(OSM natural=coastline + connected NHDPlus HR areal water; see water_edge.py)
through the EXISTING oceanmesh container path (_mesh_incontainer.py, mounted not
baked -- no image rebuild). The land polygon handed to oceanmesh is
``domain_box - water`` so the engine meshes exactly the real water body.

Full-domain, not the AOI box: each case meshes the WHOLE bay to its natural
closure (land on the inland sides, a straight offshore open boundary on the
seaward side). The tight v1 AOI box is drawn only as a residual overlay and does
not truncate the mesh.

Reuses ADR 0192 machinery unchanged: the container mesher, the format writers,
MDAL + SERAFIN verification, and the topobathy DEM fetch.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  TMPDIR=scripts/sandbox/oceanmesh/_work \
  PYTHONPATH=.:contracts/src:workers/schism:scripts/sandbox/oceanmesh \
    venvs/agent/bin/python scripts/sandbox/oceanmesh/build_coastal_water_edge_mesh.py --aoi all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("coastal_water_edge_mesh")

REPO = Path("/home/nate/Documents/trid3nt-local")
SANDBOX = REPO / "scripts/sandbox/oceanmesh"
OUT_ROOT = SANDBOX / "_runs"
PROOF_RENDERS = REPO / "docs/proof/templates"
PROOF_MESHES = REPO / "docs/proof/templates/oceanmesh_meshes"
MESH_IMAGE = "trid3nt-local/mesh:latest"

# Full-bay domains (NOT the tight v1 AOI boxes). ``aoi_box`` is the v1 tight box,
# drawn only as a residual overlay. ``closeup`` frames a shoreline-tracking
# detail (barrier islands / river arms) that NATE eyeballs against the imagery.
AOIS = {
    "delaware_bay": {
        "label": "Delaware Bay estuary (DE/NJ) -- OSM coastline water edge",
        "domain_bbox": (-75.60, 38.72, -74.82, 39.55),
        "aoi_box": (-75.55, 38.78, -74.92, 39.42),
        "closeup": (-75.10, 39.05, -74.85, 39.33),
        "closeup_label": "NJ bayshore tidal creeks + marsh edge",
        "min_edge_length_m": 150.0, "max_edge_length_m": 2500.0,
        "grade": 0.20, "open_boundary_side": "east",
    },
    "tampa_bay": {
        "label": "Tampa Bay barrier-island estuary (FL) -- OSM coastline water edge",
        "domain_bbox": (-82.90, 27.48, -82.38, 28.06),
        "aoi_box": (-82.85, 27.55, -82.35, 28.05),
        "closeup": (-82.79, 27.66, -82.66, 27.86),
        "closeup_label": "Pinellas barrier islands + passes (Gulf side)",
        "min_edge_length_m": 120.0, "max_edge_length_m": 2000.0,
        "grade": 0.20, "open_boundary_side": "west",
    },
}


def rerender(aoi: str) -> dict:
    """Re-render the proof images from the cached mesh (no re-mesh / no Overpass)."""
    sys.path.insert(0, str(REPO / "workers/schism"))
    sys.path.insert(0, str(SANDBOX))
    from mesh_formats import mesh_quality_report

    cfg = AOIS[aoi]
    domain = cfg["domain_bbox"]
    rundir = OUT_ROOT / aoi
    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    stats = json.loads((rundir / "mesh_stats.json").read_text())
    summ = json.loads((rundir / "summary_water_edge.json").read_text())
    wprov, qa = summ["water_provenance"], mesh_quality_report(points, cells)
    caption = (
        f"WATER EDGE: OSM natural=coastline + connected NHDPlus HR areal water "
        f"(GSHHG-intermediate REPLACED)\n"
        f"domain = full bay {tuple(round(v, 3) for v in domain)} "
        f"(NOT the v1 AOI box, drawn dashed)   water={wprov['water_km2']:.0f} km^2\n"
        f"engine: {stats['engine']}   feature(distance-to-shore)+wavelength(wl=10); "
        f"grade g={cfg['grade']}\n"
        f"nodes={qa['n_vertices']} elements={qa['n_elements']} "
        f"inverted={qa['inverted_elements']} closed={qa['boundary_closed']}   "
        f"resolution {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   qE min={qa['min_quality_qE']} "
        f"median={qa['median_quality_qE']}"
    )
    _render(points, cells, domain, cfg["aoi_box"],
            PROOF_RENDERS / f"oceanmesh_standalone_{aoi}.png",
            f"Water-edge OceanMesh2D  -  {cfg['label']}", caption)
    _render(points, cells, cfg["closeup"], None,
            PROOF_RENDERS / f"oceanmesh_standalone_{aoi}_closeup.png",
            f"Shoreline tracking  -  {cfg['closeup_label']}",
            "cyan mesh edge vs ESRI imagery shoreline (barrier islands / river arms)\n"
            f"closeup {tuple(round(v, 3) for v in cfg['closeup'])} of the "
            f"{cfg['label'].split(' -- ')[0]} water-edge mesh", closeup=True)
    return summ


def run(aoi: str) -> dict:
    sys.path.insert(0, str(REPO / "workers/schism"))
    sys.path.insert(0, str(SANDBOX))
    from build_coastal_mesh import (
        decimate_dem,
        fetch_dem,
        sample_elevation,
        sg_docker,
        verify_mdal,
        verify_serafin,
    )
    from mesh_formats import mesh_quality_report, write_2dm, write_fort14
    from schism_gr3 import tin_to_hgrid
    from selafin_io import write_selafin
    from water_edge import build_coastal_water

    import geopandas as gpd

    cfg = AOIS[aoi]
    domain = cfg["domain_bbox"]
    rundir = OUT_ROOT / aoi
    rundir.mkdir(parents=True, exist_ok=True)
    PROOF_MESHES.mkdir(parents=True, exist_ok=True)
    PROOF_RENDERS.mkdir(parents=True, exist_ok=True)
    log.info("=== AOI %s domain=%s ===", aoi, domain)

    # 1) high-res water polygon (OSM coastline + connected NHD areal water).
    water, wprov = build_coastal_water(domain, use_nhd=True)
    log.info("water edge: %s", json.dumps(wprov))
    water_geojson = rundir / "water_edge.geojson"
    gpd.GeoSeries([water], crs=4326).to_file(water_geojson, driver="GeoJSON")

    # 2) topobathy DEM over the full domain; decimate for the sizing functions.
    dem_path = fetch_dem(domain, rundir)
    dem_container = decimate_dem(rundir)

    # 3) mesh the exact water polygon with the custom-SDF container mesher
    #    (mounted, not baked -- the ADR 0193 watershed pattern; the coastal
    #    Shoreline path smooths/drops holes and cannot hold the real edge).
    conf = {
        "bbox": list(domain),
        "water_geojson": "/data/water_edge.geojson",
        "dem_path": dem_container,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "grade": cfg["grade"],
        "wavelength": True, "wl": 10, "max_iter": 30,
    }
    (rundir / "mesh_config.json").write_text(json.dumps(conf), encoding="utf-8")
    cp = sg_docker([
        "run", "--rm", "-v", f"{SANDBOX}:/sandbox", "-v", f"{rundir}:/data",
        "--entrypoint", "python", MESH_IMAGE,
        "/sandbox/_mesh_water_edge_incontainer.py", "/data/mesh_config.json", "/data",
    ])
    if cp.returncode != 0 or not (rundir / "coastal_tin_mesh.npz").exists():
        raise RuntimeError(f"mesh worker failed for {aoi}:\n{cp.stdout[-2500:]}\n{cp.stderr[-2500:]}")
    stats = json.loads((rundir / "mesh_stats.json").read_text())
    log.info("mesh: %s", json.dumps(stats))

    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    elevation = sample_elevation(dem_path, points)
    depth_down = -elevation
    qa = mesh_quality_report(points, cells)
    log.info("QA: %s", json.dumps(qa))

    # 4) emit the four formats (OVERWRITE the v1 files in place).
    dst2dm = PROOF_MESHES / f"{aoi}.2dm"
    dstslf = PROOF_MESHES / f"{aoi}.slf"
    dstgr3 = PROOF_MESHES / f"{aoi}_hgrid.gr3"
    dstf14 = PROOF_MESHES / f"{aoi}.fort.14"
    dst2dm.write_text(write_2dm(points, cells, z=elevation), encoding="utf-8")
    write_selafin(dstslf, points, cells, elevation)
    dstgr3.write_text(
        tin_to_hgrid(points, cells, depth=depth_down, grid_name=f"trid3nt_{aoi}",
                     open_boundary_side=cfg["open_boundary_side"]),
        encoding="utf-8",
    )
    dstf14.write_text(
        write_fort14(points, cells, depths=depth_down, grid_name=f"trid3nt_{aoi}",
                     open_boundary_side=cfg["open_boundary_side"]),
        encoding="utf-8",
    )

    # 5) verify.
    mdal_2dm = verify_mdal(dst2dm)
    mdal_slf = verify_mdal(dstslf)
    serafin = verify_serafin(dstslf, PROOF_MESHES)
    log.info("MDAL 2dm=%s slf=%s", mdal_2dm, mdal_slf)
    log.info("SERAFIN=%s", serafin)

    # 6) alignment proof renders (full-domain + shoreline-tracking closeup).
    caption = (
        f"WATER EDGE: OSM natural=coastline + connected NHDPlus HR areal water "
        f"(GSHHG-intermediate REPLACED)\n"
        f"domain = full bay {tuple(round(v, 3) for v in domain)} "
        f"(NOT the v1 AOI box, drawn dashed)   water={wprov['water_km2']:.0f} km^2\n"
        f"engine: {stats['engine']}   feature(distance-to-shore)+wavelength(wl=10); "
        f"grade g={cfg['grade']}\n"
        f"nodes={qa['n_vertices']} elements={qa['n_elements']} "
        f"inverted={qa['inverted_elements']} closed={qa['boundary_closed']}   "
        f"resolution {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   qE min={qa['min_quality_qE']} "
        f"median={qa['median_quality_qE']}"
    )
    render_path = PROOF_RENDERS / f"oceanmesh_standalone_{aoi}.png"
    _render(points, cells, domain, cfg["aoi_box"], render_path,
            f"Water-edge OceanMesh2D  -  {cfg['label']}", caption)
    closeup_path = PROOF_RENDERS / f"oceanmesh_standalone_{aoi}_closeup.png"
    _render(points, cells, cfg["closeup"], None, closeup_path,
            f"Shoreline tracking  -  {cfg['closeup_label']}",
            "cyan mesh edge vs ESRI imagery shoreline (barrier islands / river arms)\n"
            f"closeup {tuple(round(v, 3) for v in cfg['closeup'])} of the "
            f"{cfg['label'].split(' -- ')[0]} water-edge mesh", closeup=True)

    summary = {
        "aoi": aoi, "method": "water-edge (OSM coastline + NHD)", "domain_bbox": list(domain),
        "water_provenance": wprov, "mesh_stats": stats, "qa": qa,
        "elevation_range_m": [round(float(elevation.min()), 2), round(float(elevation.max()), 2)],
        "mdal_2dm": mdal_2dm, "mdal_slf": mdal_slf, "serafin": serafin,
        "files": {"twodm": str(dst2dm), "slf": str(dstslf), "gr3": str(dstgr3),
                  "fort14": str(dstf14), "render": str(render_path),
                  "closeup": str(closeup_path), "dem": str(dem_path)},
    }
    (rundir / "summary_water_edge.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("SUMMARY %s\n%s", aoi, json.dumps(summary, indent=2))
    return summary


# --------------------------------------------------------------------------- #
# ESRI-imagery render: mesh (cyan) + residual AOI box (white dashed) on imagery. #
# Tile + Web-Mercator math lives in merc_render (single source of truth).       #
# --------------------------------------------------------------------------- #


def _render(points, cells, extent_bbox, aoi_box, out_path, title, caption, *, closeup=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from merc_render import fetch_basemap, ll_to_merc, pick_zoom

    points = np.asarray(points, float)
    cells = np.asarray(cells, np.int64)
    minx, miny, maxx, maxy = extent_bbox
    pad = 0.02 if closeup else 0.06
    plon, plat = (maxx - minx) * pad, (maxy - miny) * pad
    fbox = (minx - plon, miny - plat, maxx + plon, maxy + plat)
    zoom = pick_zoom(fbox, max_tiles=12 if closeup else 10)
    basemap, (left, right, bottom, top) = fetch_basemap(fbox, zoom)

    mx, my = ll_to_merc(points[:, 0], points[:, 1])
    # Frame to the fetched basemap bounds so no white margin shows past the tiles.
    xlo, xhi, ylo, yhi = left, right, bottom, top

    map_w = 10.0
    aspect = (yhi - ylo) / (xhi - xlo)
    map_h = float(np.clip(map_w * aspect, 4.0, 15.0))
    cap_h = 1.9
    fig = plt.figure(figsize=(map_w, map_h + cap_h))
    fig.patch.set_facecolor("#111111")
    ax = fig.add_axes([0.0, cap_h / (map_h + cap_h), 1.0, map_h / (map_h + cap_h)])
    ax.imshow(np.asarray(basemap), extent=[left, right, bottom, top], origin="upper")
    lw = 0.7 if closeup else 0.35
    ax.triplot(mx, my, cells, color="#00e5ff", linewidth=lw, alpha=0.95, zorder=5)
    if aoi_box is not None:
        ax0, ay0 = ll_to_merc(aoi_box[0], aoi_box[1])
        ax1, ay1 = ll_to_merc(aoi_box[2], aoi_box[3])
        ax.plot([ax0, ax1, ax1, ax0, ax0], [ay0, ay0, ay1, ay1, ay0],
                color="white", linewidth=2.0, linestyle="--", zorder=6)
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, color="white", fontsize=13, pad=6)

    cap = fig.add_axes([0.0, 0.0, 1.0, cap_h / (map_h + cap_h)])
    cap.axis("off")
    extra = "" if closeup else "\nwhite dashed = residual v1 AOI box (does NOT truncate the mesh)"
    cap.text(0.012, 0.5, caption + extra, fontsize=9, family="monospace",
             color="white", va="center", ha="left", transform=cap.transAxes)
    fig.savefig(out_path, dpi=135, facecolor="#111111")
    plt.close(fig)
    log.info("render -> %s", out_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi", choices=list(AOIS) + ["all"], default="all")
    ap.add_argument("--render-only", action="store_true",
                    help="re-render proof images from the cached mesh (no re-mesh)")
    args = ap.parse_args(argv)
    aois = list(AOIS) if args.aoi == "all" else [args.aoi]
    results = {}
    for a in aois:
        try:
            results[a] = rerender(a) if args.render_only else run(a)
        except Exception as exc:  # noqa: BLE001
            log.exception("AOI %s FAILED: %s", a, exc)
            results[a] = {"aoi": a, "error": str(exc)}
    (OUT_ROOT / "WATER_EDGE_SUMMARY.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
