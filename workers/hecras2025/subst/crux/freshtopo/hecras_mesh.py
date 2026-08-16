#!/usr/bin/env python3
"""Standalone HEC-RAS RoG mesh authoring + validation (ADR 0211).

The refined-mesh machinery of ADR 0210 (graded Poisson-disk seeds + channel
breaklines, ``rog_refine.py``) is here made usable INDEPENDENTLY of a solve: this
module builds the channel-refined HEC-RAS cell mesh for an AOI, VALIDATES it through
the in-container ``meshprobe`` (the driver realizes the cells but does NOT prepare or
solve -- fast), and hands back the PORTABLE authoring inputs plus the realized cell
geometry so ``generate_mesh`` can persist a mesh a human inspects in QGIS and a later
``hecras_flood_2d`` rain-on-grid run consumes.

WHAT IS A PORTABLE HEC-RAS MESH? The 2025 managed engine has no single mesh file: it
realizes a Voronoi-like cell mesh INSIDE the project from ``MeshFactory.TryCreateMesh(
perimeter, cell-center seeds, channel breaklines)`` over a local-SI terrain. That
realization is DETERMINISTIC on identical seeds (verified: independent meshprobe runs
on one seed cloud produce byte-identical cell centers, same cell/face counts, clean at
attempt 0). So the portable artifact = the AUTHORING INPUTS (graded seeds + breaklines
+ the local terrain frame + the modeled-domain catchment/channel), and consumption
re-realizes exactly the inspected mesh -- no realized geometry need be stored to solve.
The realized cell polygons ARE stored, but only as the DISPLAY face (so the wireframe
NATE approves is the wireframe that solves).

Offline-first: pure numpy/scipy/shapely/geopandas on the host; the only container step
is the ``meshprobe`` mesh realization (the same authoring image + mounted driver the
solve uses -- no image rebuild). Reuses ``rog_refine`` (seeds/breaklines) +
``rog2025_pipeline`` (local terrain frame); never re-implements either. ASCII only.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# sibling modules in the freshtopo tree (added to sys.path by the caller / driver)
from rog2025_pipeline import (
    AUTHORING_IMAGE_DEFAULT,
    PROBE_DIR_DEFAULT,
    Rog2025Error,
    Rog2025Prep,
    prepare_local_terrain,
)
from rog_refine import RefineConfig, build_refined_inputs


@dataclass
class HecrasMeshResult:
    """A built + validated HEC-RAS RoG cell mesh, ready to persist as an artifact.

    ``*_path`` fields are LOCAL files under the caller's workdir (the durable copies
    the server uploads to the case bucket). ``cell_count`` / ``face_count`` and the
    size histogram are the REALIZED mesh (from the meshprobe), not the seed cloud.
    ``validated`` is True iff ``TryCreateMesh`` realized the cells (HEC's <= 8-sides
    rule passed); ``attempt0_clean`` is True iff it meshed with no seed-drop retry
    (the seed cloud is directly solve-ready)."""
    seeds_path: str
    breaklines_path: str
    local_dem_path: str
    prep_json_path: str
    display_fgb_path: str          # realized cell polygons in EPSG:4326 (the wireframe)
    prep: dict
    utm_epsg: int
    cell_count: int
    face_count: int
    n_seeds: int
    validated: bool
    attempt0_clean: bool
    badcells_attempt0: int
    channel_m: float
    background_m: float
    channel_len_km: float
    breakline_len_km: float
    size_p5: float
    size_p50: float
    size_p95: float
    size_hist_edges: list
    size_hist_counts: list
    lonlat_bbox: tuple
    outlet_edge: str
    probe_stdout_tail: str


def _meshprobe_spec(prep: Rog2025Prep, refine_dir: str, out_dir: str) -> dict:
    """A synthdrv ``meshprobe`` spec (the ParseRog fields the driver requires).

    Only the mesh geometry fields (nx/ny/cell_size/outlet_edge/refine_dir) drive the
    realization; the solve fields are present because ParseRog reads them but are inert
    for meshprobe (it builds the mesh and dumps cell centers, no prepare/solve)."""
    return {
        "out_dir": out_dir, "nx": prep.nx, "ny": prep.ny, "cell_size": prep.cell_size,
        "manning_n": 0.06, "dt_s": 1.5, "sim_seconds": 21600.0, "report_every": 200,
        "outlet_edge": prep.outlet_edge, "outlet_slope": 0.05,
        "outlet_stage": prep.elev_min_m, "outlet_bc": "normal_depth",
        "diffusion": True, "precip_mm_hr": 25.0, "refine_dir": refine_dir,
    }


def run_meshprobe(prep: Rog2025Prep, refine_stage: Path, name: str, *,
                  image: str = AUTHORING_IMAGE_DEFAULT,
                  probe_dir: str = PROBE_DIR_DEFAULT, timeout_s: int = 900):
    """Realize the graded cell mesh in-container (no prepare/solve) -> cell centers.

    ``refine_stage`` holds the staged ``seeds.f64`` + ``breaklines.json`` under
    ``probe_dir`` (so the container's ``/probe`` mount sees them). Returns
    ``(cellcenters_local (Nc,2) numpy, cells, faces, attempt0_clean, badcells0,
    stdout_tail)``. Raises ``Rog2025Error`` if the mesh never realized."""
    import numpy as np

    probe = Path(probe_dir)
    out_dir_host = probe / f"rog_{name}" / "meshprobe"
    out_dir_host.mkdir(parents=True, exist_ok=True)
    refine_container = f"/probe/{refine_stage.relative_to(probe)}"
    out_container = f"/probe/rog_{name}/meshprobe"
    spec = _meshprobe_spec(prep, refine_container, out_container)
    spec_host = probe / f"rog_{name}" / "meshprobe_spec.json"
    spec_host.parent.mkdir(parents=True, exist_ok=True)
    spec_host.write_text(json.dumps(spec))
    runner = f"""
set -e
cd /opt/hecras2025/app
cp /probe/synthdrv.dll .
cp ras.runtimeconfig.json synthdrv.runtimeconfig.json
dotnet synthdrv.dll meshprobe /probe/rog_{name}/meshprobe_spec.json
echo MESHPROBE_DONE
"""
    run_host = probe / f"rog_{name}" / "meshprobe_run.sh"
    run_host.write_text(runner)
    argv = ["docker", "run", "--rm", "-v", f"{probe}:/probe",
            "--entrypoint", "/bin/bash", image, f"/probe/rog_{name}/meshprobe_run.sh"]
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    wall = time.time() - t0
    tail = (proc.stdout + "\n=== STDERR ===\n" + proc.stderr)[-2500:]
    cc_path = out_dir_host / "cellcenters.f64"
    probe_json = out_dir_host / "mesh_probe.json"
    if "MESHPROBE_DONE" not in proc.stdout or not cc_path.exists():
        raise Rog2025Error(
            f"meshprobe did not realize a mesh (exit {proc.returncode}, {wall:.0f}s):\n{tail}")
    cc = np.fromfile(cc_path, dtype="<f8").reshape(-1, 2)
    pj = json.loads(probe_json.read_text())
    cells, faces = int(pj["cells"]), int(pj["faces"])
    # the first attempt's badcells + ok, parsed from the driver's per-attempt log
    attempt0_clean, badcells0 = False, -999
    for line in proc.stdout.splitlines():
        if "attempt=0" in line and "TryCreateMesh" in line:
            attempt0_clean = "ok=True" in line
            try:
                badcells0 = int(line.split("badcells=")[1].split()[0])
            except Exception:  # noqa: BLE001
                badcells0 = -999
            break
    return cc, cells, faces, attempt0_clean, badcells0, tail


def _voronoi_cells_lonlat_fgb(cellcenters, prep: Rog2025Prep, out_fgb: Path) -> tuple:
    """Realized HEC cells = Voronoi of the cell centers clipped to the domain rectangle.

    Writes the cell polygons (reprojected local-SI -> UTM -> EPSG:4326) as a FlatGeobuf
    with a per-cell ``size_m`` (sqrt area) attribute -- the wireframe QGIS renders.
    Returns the lon/lat bbox. (The engine's cells ARE the clipped Voronoi of the
    centers, so this is the true realized mesh, not an approximation.)"""
    import geopandas as gpd
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import MultiPoint, box
    from shapely.ops import transform as shp_transform, voronoi_diagram

    W, H = prep.width_m, prep.height_m
    env = box(0.0, 0.0, W, H)
    mp = MultiPoint([(float(x), float(y)) for x, y in cellcenters])
    polys = [g.intersection(env) for g in voronoi_diagram(mp, envelope=env).geoms]
    to_ll = Transformer.from_crs(f"EPSG:{prep.utm_epsg}", "EPSG:4326", always_xy=True)

    def loc_to_ll(x, y, z=None):
        return to_ll.transform(prep.origin_x + np.asarray(x), prep.origin_y + np.asarray(y))

    geoms, sizes = [], []
    for poly in polys:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        geoms.append(shp_transform(loc_to_ll, poly))
        sizes.append(round(float(poly.area) ** 0.5, 2))
    gdf = gpd.GeoDataFrame({"size_m": sizes}, geometry=geoms, crs="EPSG:4326")
    out_fgb.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_fgb, driver="FlatGeobuf")
    b = gdf.total_bounds
    return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))


def build_hecras_mesh(dem_tif, workdir, *, bbox4326, pour_point, catchment_geojson,
                      flowlines_path, background_m=90.0, channel_m=22.0,
                      image=AUTHORING_IMAGE_DEFAULT,
                      probe_dir=PROBE_DIR_DEFAULT) -> HecrasMeshResult:
    """Build + validate a channel-refined HEC-RAS RoG cell mesh for the AOI.

    DEM + delineated catchment + channel network -> a local-SI terrain frame
    (``prepare_local_terrain``) -> graded seeds + channel breaklines
    (``rog_refine.build_refined_inputs``) -> in-container ``meshprobe`` realization
    (validated, no solve) -> the portable authoring inputs + the realized cell polygons
    (display) copied into ``workdir`` for the server to persist. ``background_m`` /
    ``channel_m`` are the coarse-hillslope + fine-channel target cell sizes (the
    granularity levers). Raises ``Rog2025Error`` if the mesh never realizes."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    name = workdir.name

    prep = prepare_local_terrain(
        dem_tif, workdir / "prep", cell_size=float(background_m), elev_units="m",
        bbox4326=list(bbox4326), pour_point=pour_point)

    cfg = RefineConfig(background_m=float(background_m), channel_m=float(channel_m))
    refine_stage = Path(probe_dir) / f"rog_{name}" / "refine"
    refine = build_refined_inputs(prep, catchment_geojson, flowlines_path, refine_stage, cfg)

    cc, cells, faces, attempt0_clean, badcells0, tail = run_meshprobe(
        prep, refine_stage, name, image=image, probe_dir=probe_dir)

    # durable copies into workdir (the server uploads these as the artifact bundle)
    seeds_dst = workdir / "seeds.f64"
    seeds_dst.write_bytes(Path(refine.seeds_path).read_bytes())
    bl_dst = workdir / "breaklines.json"
    bl_dst.write_bytes(Path(refine.breaklines_path).read_bytes())
    dem_dst = workdir / "local_dem.tif"
    dem_dst.write_bytes(Path(prep.local_dem).read_bytes())
    prep_json = workdir / "prep.json"
    prep_doc = asdict(prep)
    prep_doc["local_dem"] = "local_dem.tif"   # relative -- resolved on consume
    prep_json.write_text(json.dumps(prep_doc, indent=2))

    display_fgb = workdir / "cells_lonlat.fgb"
    lonlat_bbox = _voronoi_cells_lonlat_fgb(cc, prep, display_fgb)

    return HecrasMeshResult(
        seeds_path=str(seeds_dst), breaklines_path=str(bl_dst),
        local_dem_path=str(dem_dst), prep_json_path=str(prep_json),
        display_fgb_path=str(display_fgb), prep=prep_doc, utm_epsg=int(prep.utm_epsg),
        cell_count=cells, face_count=faces, n_seeds=refine.n_seeds,
        validated=True, attempt0_clean=attempt0_clean, badcells_attempt0=badcells0,
        channel_m=float(channel_m), background_m=float(background_m),
        channel_len_km=refine.channel_len_km, breakline_len_km=refine.breakline_len_km,
        size_p5=refine.size_p5, size_p50=refine.size_p50, size_p95=refine.size_p95,
        size_hist_edges=refine.size_hist_edges, size_hist_counts=refine.size_hist_counts,
        lonlat_bbox=lonlat_bbox, outlet_edge=prep.outlet_edge, probe_stdout_tail=tail)
