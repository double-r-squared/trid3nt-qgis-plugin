"""mesh-front STANDALONE sandbox -- OceanMesh2D coastal mesh builder.

RESEARCH / LOCAL-FIRST STANDALONE CAPABILITY (nothing registered, nothing wired
into any workflow/template/engine). Meshes named US coastal AOIs with the
authentic CHLNDDEV ``oceanmesh`` (OceanMesh2D Python port, run inside the
GPL-isolated ``trid3nt-local/mesh:latest`` image) and EMITS the meshes for NATE
to inspect in QGIS. Placement into any pipeline is deferred to NATE (see
docs/research/oceanmesh-front-proposal.md).

Per AOI:
  1. shoreline  : staged GSHHG intermediate L1 land polygons (real NOAA/NGDC
                  shoreline), clipped to the AOI by oceanmesh.
  2. bathymetry : our OWN fetch_topobathy merge logic (ETOPO 2022 base +
                  CUDEM 1/9" + 3DEP) -> local EPSG:4326 dem.tif.
  3. mesh       : oceanmesh feature_sizing (distance-to-shore) + wavelength
                  sizing (bathymetry), gradation-limited, land (exterior) faces
                  deleted -- run in-container (_mesh_incontainer.py).
  4. formats    : <aoi>.2dm (SMS/MDAL) + <aoi>.slf (SELAFIN/TELEMAC geometry,
                  bathymetry as Z) + hgrid.gr3 (SCHISM) + fort.14 (ADCIRC bonus).
  5. verify     : independent quality/inverted/closure QA; MDAL read-back via
                  host QGIS (QgsMeshLayer); SELAFIN read-back via the telemac
                  worker's own data_manip SERAFIN reader.
  6. emit       : ESRI-imagery proof render + copy meshes to docs/proof.

Run (per AOI or all):
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  TMPDIR=/tmp \
  PYTHONPATH=.:contracts:scripts/sandbox/oceanmesh \
    venvs/agent/bin/python scripts/sandbox/oceanmesh/build_coastal_mesh.py --aoi all
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oceanmesh_standalone")

REPO = Path("/home/nate/Documents/trid3nt-local")
SANDBOX = REPO / "scripts/sandbox/oceanmesh"
SHORELINE_SHP_HOST = SANDBOX / "shoreline/GSHHS_i_L1.shp"
OUT_ROOT = SANDBOX / "_runs"
PROOF_RENDERS = REPO / "docs/proof/templates"
PROOF_MESHES = REPO / "docs/proof/templates/oceanmesh_meshes"
MESH_IMAGE = "trid3nt-local/mesh:latest"
TELEMAC_IMAGE = "trid3nt-local/telemac:latest"

AOIS = {
    "delaware_bay": {
        "bbox": (-75.55, 38.78, -74.92, 39.42),
        "min_edge_length_m": 150.0, "max_edge_length_m": 2500.0,
        "grade": 0.20, "open_boundary_side": "east",
        "label": "Delaware Bay estuary (DE/NJ)",
    },
    "duck_nc": {
        "bbox": (-75.83, 36.07, -75.68, 36.28),
        "min_edge_length_m": 60.0, "max_edge_length_m": 1200.0,
        "grade": 0.20, "open_boundary_side": "east",
        "label": "Duck NC / FRF open-coast shelf (Outer Banks)",
    },
    "tampa_bay": {
        "bbox": (-82.85, 27.55, -82.35, 28.05),
        "min_edge_length_m": 100.0, "max_edge_length_m": 2000.0,
        "grade": 0.20, "open_boundary_side": "west",
        "label": "Tampa Bay barrier-island estuary (FL)",
    },
    "puget_sound": {
        "bbox": (-122.75, 47.35, -122.25, 47.85),
        "min_edge_length_m": 90.0, "max_edge_length_m": 1500.0,
        "grade": 0.20, "open_boundary_side": "north",
        "label": "Central Puget Sound complex shoreline (WA)",
    },
}


def sg_docker(args: list[str], timeout: float = 2400.0) -> subprocess.CompletedProcess:
    cmd = "docker " + " ".join(args)
    return subprocess.run(
        ["sg", "docker", "-c", cmd], capture_output=True, text=True, timeout=timeout
    )


def fetch_dem(bbox, rundir: Path) -> Path:
    dem_path = rundir / "dem.tif"
    if dem_path.exists() and dem_path.stat().st_size > 0:
        log.info("dem.tif cached %s", dem_path)
        return dem_path
    import rasterio
    from rasterio.crs import CRS
    from trid3nt_server.tools.fetchers._router.hooks import topobathy as T

    array, transform, _crs, prov = T._select_and_merge(
        tuple(float(v) for v in bbox), 30, "EPSG:4326", None, 180.0, True, False, None
    )
    array = np.asarray(array)
    if array.ndim == 3:
        array = array[0]
    with rasterio.open(
        dem_path, "w", driver="GTiff", height=array.shape[0], width=array.shape[1],
        count=1, dtype="float32", crs=CRS.from_string("EPSG:4326"),
        transform=transform, nodata=-99999.0,
    ) as dst:
        dst.write(array.astype("float32"), 1)
    finite = array[np.isfinite(array)]
    log.info("dem.tif %dx%d elev[%.1f..%.1f] bathy=%s cudem=%s",
             array.shape[1], array.shape[0], float(finite.min()), float(finite.max()),
             prov.get("bathymetry_present"), prov.get("cudem_tile_count"))
    return dem_path


def stage_shoreline(bbox, rundir: Path) -> str:
    """Clip the global GSHHG L1 land polygons to the AOI (+margin) and repair any
    invalid geometry (buffer(0)) so oceanmesh's shoreline classifier does not
    choke on GSHHG self-intersections. Writes rundir/shoreline_clip.shp and
    returns the container-side path (/data/shoreline_clip.shp)."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import box
    from shapely.validation import make_valid

    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    mx = (xmax - xmin) * 0.15
    my = (ymax - ymin) * 0.15
    clip_box = box(xmin - mx, ymin - my, xmax + mx, ymax + my)
    gdf = gpd.read_file(SHORELINE_SHP_HOST, bbox=(xmin - mx, ymin - my, xmax + mx, ymax + my))
    if gdf.empty:
        return "/sandbox/shoreline/GSHHS_i_L1.shp"
    geoms = []
    for g in gdf.geometry:
        if g is None:
            continue
        g = make_valid(g) if not g.is_valid else g
        # Snap coordinates onto a fine grid: eliminates the near-degenerate
        # vertices that make GEOS throw side-location conflicts inside
        # oceanmesh's boubox.difference(shoreline) shoreline classification.
        g = shapely.set_precision(g.buffer(0), 1e-7)
        g = g.intersection(clip_box)
        if not g.is_empty:
            geoms.append(g)
    clean = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")
    clean = clean[clean.geometry.is_valid & ~clean.geometry.is_empty]
    out = rundir / "shoreline_clip.shp"
    clean.to_file(out)
    return "/data/shoreline_clip.shp"


def decimate_dem(rundir: Path, max_px: int = 2500) -> str:
    """Write a coarsened dem_mesh.tif (<= max_px on the long side) for the sizing
    functions -- the full-res dem.tif is kept for per-node depth sampling. Keeps
    oceanmesh's wavelength/slope sizing fast and memory-light. Returns the
    container path used for meshing."""
    import rasterio
    from rasterio.enums import Resampling

    src_path = rundir / "dem.tif"
    with rasterio.open(src_path) as ds:
        long_px = max(ds.width, ds.height)
        if long_px <= max_px:
            return "/data/dem.tif"
        scale = max_px / long_px
        nh, nw = max(1, int(ds.height * scale)), max(1, int(ds.width * scale))
        data = ds.read(1, out_shape=(nh, nw), resampling=Resampling.average)
        transform = ds.transform * ds.transform.scale(ds.width / nw, ds.height / nh)
        prof = ds.profile
    prof.update(width=nw, height=nh, transform=transform)
    with rasterio.open(rundir / "dem_mesh.tif", "w", **prof) as dst:
        dst.write(data, 1)
    log.info("dem_mesh.tif %dx%d (from %d px)", nw, nh, long_px)
    return "/data/dem_mesh.tif"


def run_mesh(aoi: str, cfg: dict, rundir: Path) -> dict:
    shoreline_container = stage_shoreline(cfg["bbox"], rundir)
    dem_container = decimate_dem(rundir)
    conf = {
        "bbox": list(cfg["bbox"]),
        "shoreline_shp": shoreline_container,
        "dem_path": dem_container,
        "min_edge_length_m": cfg["min_edge_length_m"],
        "max_edge_length_m": cfg["max_edge_length_m"],
        "grade": cfg["grade"], "feature_r": 3,
        "wavelength": True, "wl": 10, "slope": False, "max_iter": 25,
    }
    (rundir / "mesh_config.json").write_text(json.dumps(conf), encoding="utf-8")
    cp = sg_docker([
        "run", "--rm", "-v", f"{SANDBOX}:/sandbox", "-v", f"{rundir}:/data",
        "--entrypoint", "python", MESH_IMAGE,
        "/sandbox/_mesh_incontainer.py", "/data/mesh_config.json", "/data",
    ])
    if cp.returncode != 0 or not (rundir / "coastal_tin_mesh.npz").exists():
        raise RuntimeError(f"mesh worker failed for {aoi}:\n{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    return json.loads((rundir / "mesh_stats.json").read_text())


def sample_elevation(dem_path: Path, points: np.ndarray) -> np.ndarray:
    import rasterio
    with rasterio.open(dem_path) as ds:
        vals = np.array(
            [v[0] for v in ds.sample([(float(x), float(y)) for x, y in points[:, :2]])],
            dtype=float,
        )
    return np.where(np.isfinite(vals) & (vals > -9000), vals, 0.0)


def verify_mdal(mesh_path: Path) -> dict:
    script = (
        "from qgis.core import QgsApplication, QgsMeshLayer;"
        "a=QgsApplication([],False);a.initQgis();"
        f"l=QgsMeshLayer(r'{mesh_path}','m','mdal');"
        "import json;print('MDAL_JSON'+json.dumps({'valid':bool(l.isValid()),"
        "'vertices':l.dataProvider().vertexCount() if l.isValid() else 0,"
        "'faces':l.dataProvider().faceCount() if l.isValid() else 0}));a.exitQgis()"
    )
    cp = subprocess.run(["/usr/bin/python3", "-c", script],
                        capture_output=True, text=True, timeout=300)
    for line in (cp.stdout + cp.stderr).splitlines():
        if line.startswith("MDAL_JSON"):
            return json.loads(line[len("MDAL_JSON"):])
    return {"valid": False, "error": (cp.stderr or cp.stdout)[-400:]}


def verify_serafin(slf_host: Path, rundir: Path) -> dict:
    pycode = (
        "from data_manip.extraction.telemac_file import TelemacFile;"
        f"t=TelemacFile('/data/{slf_host.name}');"
        "import json;"
        "print('SLF_JSON'+json.dumps({'npoin':int(t.npoin2),'nelem':int(t.nelem2),"
        "'nvar':len(t.varnames),'varnames':[v.strip() for v in t.varnames],"
        "'x_range':[float(t.meshx.min()),float(t.meshx.max())],"
        "'y_range':[float(t.meshy.min()),float(t.meshy.max())]}));t.close()"
    )
    cp = sg_docker([
        "run", "--rm", "-v", f"{rundir}:/data", "--entrypoint", "bash",
        TELEMAC_IMAGE, "-lc", f"\"python3 -c \\\"{pycode}\\\"\"",
    ])
    for line in (cp.stdout + cp.stderr).splitlines():
        if line.startswith("SLF_JSON"):
            return json.loads(line[len("SLF_JSON"):])
    return {"ok": False, "error": (cp.stderr or cp.stdout)[-600:]}


def run(aoi: str) -> dict:
    sys.path.insert(0, str(SANDBOX))
    from mesh_formats import mesh_quality_report, write_2dm, write_fort14
    from schism_gr3 import tin_to_hgrid
    from selafin_io import write_selafin

    cfg = AOIS[aoi]
    rundir = OUT_ROOT / aoi
    rundir.mkdir(parents=True, exist_ok=True)
    PROOF_MESHES.mkdir(parents=True, exist_ok=True)
    PROOF_RENDERS.mkdir(parents=True, exist_ok=True)
    log.info("=== AOI %s bbox=%s ===", aoi, cfg["bbox"])

    dem_path = fetch_dem(cfg["bbox"], rundir)
    stats = run_mesh(aoi, cfg, rundir)
    log.info("mesh: %s", json.dumps(stats))

    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    elevation = sample_elevation(dem_path, points)   # positive-up (m)
    depth_down = -elevation                           # SCHISM/ADCIRC positive-down

    qa = mesh_quality_report(points, cells)
    log.info("QA: %s", json.dumps(qa))

    # --- format writers -------------------------------------------------------
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

    # --- verification ---------------------------------------------------------
    mdal_2dm = verify_mdal(dst2dm)
    mdal_slf = verify_mdal(dstslf)
    serafin = verify_serafin(dstslf, PROOF_MESHES)
    log.info("MDAL 2dm=%s slf=%s", mdal_2dm, mdal_slf)
    log.info("SERAFIN=%s", serafin)

    # --- proof render ---------------------------------------------------------
    from render_mesh import render
    caption = (
        f"AOI: {cfg['label']}   bbox={tuple(round(v,3) for v in cfg['bbox'])}\n"
        f"engine: {stats['engine']}\n"
        f"sizing: feature(distance-to-shore) + wavelength(bathymetry, wl=10);"
        f" gradation g={cfg['grade']}\n"
        f"nodes={qa['n_vertices']}  elements={qa['n_elements']}  "
        f"inverted={qa['inverted_elements']}  closed={qa['boundary_closed']}\n"
        f"resolution: {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   "
        f"quality qE min={qa['min_quality_qE']} median={qa['median_quality_qE']}"
    )
    render_path = PROOF_RENDERS / f"oceanmesh_standalone_{aoi}.png"
    try:
        render(points, cells, cfg["bbox"], render_path, aoi_name=cfg["label"], caption=caption)
        render_ok = True
    except Exception as exc:  # noqa: BLE001
        log.error("render failed: %s", exc)
        render_ok = False

    summary = {
        "aoi": aoi, "bbox": list(cfg["bbox"]), "label": cfg["label"],
        "mesh_stats": stats, "qa": qa,
        "elevation_range_m": [round(float(elevation.min()), 2), round(float(elevation.max()), 2)],
        "depth_down_range_m": [round(float(depth_down.min()), 2), round(float(depth_down.max()), 2)],
        "mdal_2dm": mdal_2dm, "mdal_slf": mdal_slf, "serafin": serafin,
        "files": {
            "twodm": str(dst2dm), "slf": str(dstslf), "gr3": str(dstgr3),
            "fort14": str(dstf14), "render": str(render_path) if render_ok else None,
            "dem": str(dem_path),
        },
    }
    (rundir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info("SUMMARY %s\n%s", aoi, json.dumps(summary, indent=2))
    return summary


def rerender(aoi: str) -> dict:
    """Re-render the proof image from the cached mesh + summary (no re-mesh)."""
    sys.path.insert(0, str(SANDBOX))
    from render_mesh import render

    rundir = OUT_ROOT / aoi
    s = json.loads((rundir / "summary.json").read_text())
    npz = np.load(rundir / "coastal_tin_mesh.npz")
    points, cells = npz["points"], npz["cells"]
    qa, stats = s["qa"], s["mesh_stats"]
    caption = (
        f"AOI: {s['label']}   bbox={tuple(round(v, 3) for v in s['bbox'])}\n"
        f"engine: {stats['engine']}\n"
        f"sizing: feature(distance-to-shore) + wavelength(bathymetry, wl=10);"
        f" gradation g={stats['grade']}\n"
        f"nodes={qa['n_vertices']}  elements={qa['n_elements']}  "
        f"inverted={qa['inverted_elements']}  closed={qa['boundary_closed']}\n"
        f"resolution: {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
        f"(median {qa['edge_median_m']:.0f} m)   "
        f"quality qE min={qa['min_quality_qE']} median={qa['median_quality_qE']}"
    )
    out = PROOF_RENDERS / f"oceanmesh_standalone_{aoi}.png"
    render(points, cells, s["bbox"], out, aoi_name=s["label"], caption=caption)
    return s


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
    if not args.render_only:
        (OUT_ROOT / "ALL_SUMMARY.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
