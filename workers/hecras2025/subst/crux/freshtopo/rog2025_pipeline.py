#!/usr/bin/env python3
"""Rain-on-grid on the HEC-RAS 2025 managed engine.

The productionized RoG backend: a fetched-AOI DEM -> a structured 2D area +
constant design-storm precipitation + a NormalDepth outlet, authored + prepared +
SOLVED entirely on Linux in the 2025 authoring image (mounted-driver pattern, no
image rebuild -- the managed CPU solver is pure C#). This is the path the
6.6 Fortran solver could NOT take (its rain-on-grid needs a Windows-preprocessing
hydrology scaffold).

Pipeline:
    DEM (any CRS, elevation in `elev_units`)                          [host]
      -> reproject to a LOCAL SI grid (origin 0,0; metres; elevation m),
         resampled to the model cell size                            [rasterio]
      -> author a 2025 project (synthdrv `realrog`): structured mesh over the
         extent + constant precip (mm/hr) + NormalDepth outlet on the pour-point
         wall; OVERWRITE the exported synthetic Terrain.tif with the real DEM  [docker]
      -> `ras prepare` (subgrid property tables over the real terrain) +
         `ras solve --solver CPU`                                    [docker]
      -> metrics from the result HDF (outlet Q by mass balance, max depth/
         velocity, runoff volume, mass balance)                      [host]

INFILTRATION: the 2025 managed engine exposes NO infiltration layer (decompile:
no Infiltration/CurveNumber/GreenAmpt in Ras.Core or Ras.Engine; the 26 Ras.Mapper
hits are the decoupled 6.6 geometry/UI layer). RoG here is RAIN-ONLY (gross
rainfall, no SCS-CN loss) -- stated honestly, an upper-bound runoff.

UNITS: in an SI project ConstantValue IS the rate in mm/hr (scale 1/3600*0.001 ->
m/s, decoded from PrecipitationLayer.cs and mass-checked: rate 25 -> 25.0 mm/hr
uniform rise, rate 50 -> 50.0). Factor = 1.0.

Run in the agent venv with the MinIO env block (never ambient AWS). ASCII only.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent

AUTHORING_IMAGE_DEFAULT = "trid3nt-local/hecras2025-authoring:latest"
#: Host dir carrying the compiled synthdrv.dll + the decompiled app dlls (built
#: once via REPRODUCE.md; the mounted-driver pattern -- no image rebuild).
PROBE_DIR_DEFAULT = "/home/nate/hecras_probe2025"


class Rog2025Error(RuntimeError):
    """A stage failed (reproject / author / prepare / solve / extract)."""


@dataclass
class Rog2025Prep:
    local_dem: str          # local-SI DEM tif (origin 0,0; metres)
    nx: int
    ny: int
    cell_size: float
    width_m: float
    height_m: float
    outlet_edge: str
    utm_epsg: int           # the metric CRS the local grid was cut from
    origin_x: float         # UTM x of local (0,0)
    origin_y: float         # UTM y of local (0,0) == the SOUTH edge
    elev_min_m: float
    elev_max_m: float
    valid_frac: float


def _pick_utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _edge_from_pour_point(pp_lonlat, bbox4326) -> str:
    """Nearest domain wall to the pour point (where the catchment drains)."""
    if pp_lonlat is None:
        return "s"
    lon, lat = pp_lonlat
    min_lon, min_lat, max_lon, max_lat = bbox4326
    d = {"w": abs(lon - min_lon), "e": abs(max_lon - lon),
         "s": abs(lat - min_lat), "n": abs(max_lat - lat)}
    return min(d, key=d.get)


def prepare_local_terrain(dem_tif, workdir, *, cell_size=60.0, elev_units="m",
                          bbox4326=None, pour_point=None, outlet_edge=None,
                          terrain_res=None) -> Rog2025Prep:
    """Reproject DEM -> local SI grid (origin 0,0; metres).

    The TERRAIN raster is written at a FINE resolution (``terrain_res``, well below
    the mesh cell), so ``ras prepare`` can sub-sample each face/cell profile over the
    subgrid -- a terrain at the mesh cell size makes it report "Missing terrain data
    at Face". The MESH cell count derives from the coarse ``cell_size``."""
    import numpy as np
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_bounds
    from rasterio.transform import from_origin
    from rasterio.crs import CRS

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    elev_scale = 0.3048 if str(elev_units).lower() in ("ft", "ftus", "feet") else 1.0
    if terrain_res is None:
        terrain_res = max(5.0, cell_size / 6.0)   # terrain far finer than the mesh

    with rasterio.open(dem_tif) as src:
        src_crs = src.crs
        if bbox4326 is None:
            b = transform_bounds(src_crs, "EPSG:4326", *src.bounds)
            bbox4326 = list(b)
        cx = 0.5 * (bbox4326[0] + bbox4326[2])
        cy = 0.5 * (bbox4326[1] + bbox4326[3])
        utm_epsg = _pick_utm_epsg(cx, cy)
        dst_crs = CRS.from_epsg(utm_epsg)
        transform, w, h = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds, resolution=terrain_res)
        utm = np.full((h, w), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=utm,
            src_transform=src.transform, src_crs=src_crs,
            dst_transform=transform, dst_crs=dst_crs,
            src_nodata=src.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear)

    utm = utm * elev_scale
    valid = np.isfinite(utm)
    valid_frac = float(valid.mean())
    if valid_frac < 0.2:
        raise Rog2025Error(f"reprojected DEM mostly nodata (valid {valid_frac:.2f})")
    elev_min = float(np.nanmin(utm))
    elev_max = float(np.nanmax(utm))
    # nodata -> nearest valid (rotation corners + holes): a cliff/sentinel makes
    # face-profile sampling fail; nearest-fill leaves a fully-valid terrain.
    from scipy import ndimage
    idx = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    utm = utm[tuple(idx)].astype("float32")

    ny_t, nx_t = utm.shape
    origin_x = transform.c                      # UTM x of terrain pixel (0,0)
    # MESH grid from the coarse cell size, fitting INSIDE the fine terrain footprint.
    nx = int((nx_t * terrain_res) // cell_size)
    ny = int((ny_t * terrain_res) // cell_size)
    width_m = nx * cell_size
    height_m = ny * cell_size
    terr_h = ny_t * terrain_res
    origin_y = transform.f - terr_h             # UTM y of local y=0 (terrain south edge)

    # Terrain tif in LOCAL coords: top-left at (0, terr_h); the mesh (0..W,0..H) sits
    # in the bottom-left. Pad a small edge margin so boundary faces have terrain.
    # It MUST match the HEC terrain format the engine reads: TILED (256) + NoData +
    # OVERVIEW pyramids (a plain striped tif with no overviews -> "Missing terrain
    # data at Face"; the synthetic TiffExportEngine.ExportWithOverviews reference).
    from rasterio.enums import Resampling as RIOResampling
    PAD = 3
    utm = np.pad(utm, PAD, mode="edge")
    local_tif = workdir / "local_dem.tif"
    with rasterio.open(
        local_tif, "w", driver="GTiff", height=ny_t + 2 * PAD, width=nx_t + 2 * PAD, count=1,
        dtype="float32", crs=None,
        transform=from_origin(-PAD * terrain_res, terr_h + PAD * terrain_res, terrain_res, terrain_res),
        nodata=-9999.0, tiled=True, blockxsize=256, blockysize=256,
    ) as dst:
        dst.write(utm, 1)
        dst.build_overviews([2, 4, 8, 16], RIOResampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    if outlet_edge is None:
        outlet_edge = _edge_from_pour_point(pour_point, bbox4326)

    return Rog2025Prep(
        local_dem=str(local_tif), nx=nx, ny=ny, cell_size=cell_size,
        width_m=width_m, height_m=height_m, outlet_edge=outlet_edge,
        utm_epsg=utm_epsg, origin_x=origin_x, origin_y=origin_y,
        elev_min_m=elev_min, elev_max_m=elev_max, valid_frac=valid_frac)


def _author_prepare_solve(prep: Rog2025Prep, workdir, *, precip_mm_hr, storm_hours,
                          sim_hours, dt_s, report_every, outlet_slope, diffusion,
                          outlet_bc, outlet_stage, image, probe_dir, timeout_s=14400,
                          refine=None) -> Path:
    """Run synthdrv realrog + ras prepare + ras solve in the authoring image.

    The project is authored under /probe/<name>; the exported synthetic Terrain.tif
    is overwritten with the real local DEM before prepare. Returns the result HDF.

    ``refine`` = a ``RefineResult`` from ``rog_refine.build_refined_inputs``;
    when present the driver builds the graded ``TryCreateMesh`` mesh from the staged
    seeds/breaklines instead of the uniform structured grid (spec ``refine_dir``)."""
    workdir = Path(workdir)
    name = workdir.name
    probe = Path(probe_dir)
    # stage the local DEM + spec where the container (mounts /probe=probe_dir) sees them
    stage = probe / f"rog_{name}"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "local_dem.tif").write_bytes(Path(prep.local_dem).read_bytes())

    out_dir = f"/probe/rog_{name}/proj"
    spec = {
        "out_dir": out_dir, "nx": prep.nx, "ny": prep.ny, "cell_size": prep.cell_size,
        "manning_n": 0.06, "dt_s": dt_s, "sim_seconds": sim_hours * 3600.0,
        "report_every": report_every, "outlet_edge": prep.outlet_edge,
        "outlet_slope": outlet_slope, "diffusion": bool(diffusion),
        "outlet_bc": outlet_bc, "outlet_stage": outlet_stage,
        "precip_mm_hr": precip_mm_hr,
    }
    if refine is not None:
        # seeds.f64/breaklines.json are authored into stage/refine by the caller
        # (build_refined_inputs); the driver reads them via spec["refine_dir"].
        spec["refine_dir"] = f"/probe/rog_{name}/refine"
    (stage / "spec.json").write_text(json.dumps(spec))

    storm_seconds = storm_hours * 3600.0
    runner = f"""
set -e
cd /opt/hecras2025/app
cp /probe/synthdrv.dll .
cp ras.runtimeconfig.json synthdrv.runtimeconfig.json
rm -rf {out_dir} {out_dir}_r2r
dotnet synthdrv.dll realrog /probe/rog_{name}/spec.json
# overwrite the exported synthetic terrain with the REAL local DEM
cp /probe/rog_{name}/local_dem.tif "{out_dir}/Terrains/Terrain.tif"
RAS=$(ls {out_dir}/*.ras | head -1)
mkdir -p {out_dir}_r2r
dotnet ras.dll prepare -s "$RAS" -o {out_dir}_r2r -f
R2R=$(ls {out_dir}_r2r/*.r2r.h5 | head -1)
# clamp the storm: precip BC end time already caps rain at sim end; storm<sim is
# handled by the caller choosing sim_hours (rain runs for the whole BC window).
dotnet ras.dll solve "$R2R" /probe/rog_{name}/result.h5 --solver CPU -f
echo SOLVE_OK
"""
    (stage / "run.sh").write_text(runner)
    argv = ["docker", "run", "--rm", "-v", f"{probe}:/probe",
            "--entrypoint", "/bin/bash", image, f"/probe/rog_{name}/run.sh"]
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    wall = time.time() - t0
    log = (stage / "run.log")
    log.write_text(proc.stdout + "\n=== STDERR ===\n" + proc.stderr)
    result = stage / "result.h5"
    if "SOLVE_OK" not in proc.stdout or not result.exists():
        raise Rog2025Error(
            f"author/prepare/solve failed (exit {proc.returncode}, {wall:.0f}s):\n"
            f"{proc.stdout[-2500:]}\n{proc.stderr[-1500:]}")
    return result, wall


def _catchment_mask(cell_xy_local, prep, catchment_geojson):
    """Boolean per-cell mask: which mesh cells fall inside the catchment polygon.

    Cell local coords -> UTM (origin_x/origin_y from the reproject) -> point-in-poly
    against the delineated catchment (transformed to the same UTM). Restricts the
    metrics to the SAME domain TELEMAC meshed (a fair like-for-like)."""
    import json
    import numpy as np
    from shapely.geometry import shape, Point
    from shapely.prepared import prep as shapely_prep
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer

    g = json.load(open(catchment_geojson))
    feats = g["features"] if isinstance(g, dict) and "features" in g else [g]
    geom = shape(feats[0]["geometry"])
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{prep.utm_epsg}", always_xy=True).transform
    geom_utm = shp_transform(tr, geom)
    pg = shapely_prep(geom_utm)
    ux = prep.origin_x + cell_xy_local[:, 0]
    uy = prep.origin_y + cell_xy_local[:, 1]
    return np.array([pg.contains(Point(x, y)) for x, y in zip(ux, uy)]), geom_utm.area


def _voronoi_cell_areas(cell_xy, W, H):
    """True per-cell area (m2) for the graded mesh: the Voronoi cell of each center
    clipped to the domain rectangle IS the HEC cell (its cells are the clipped Voronoi
    of the centers). Matched back to cell order by which polygon contains the center."""
    import numpy as np
    from shapely.geometry import MultiPoint, box, Point
    from shapely.ops import voronoi_diagram
    from shapely.strtree import STRtree

    env = box(0.0, 0.0, W, H)
    mp = MultiPoint([(float(x), float(y)) for x, y in cell_xy])
    polys = [g.intersection(env) for g in voronoi_diagram(mp, envelope=env).geoms]
    tree = STRtree(polys)
    areas = np.zeros(len(cell_xy))
    for i, (x, y) in enumerate(cell_xy):
        p = Point(float(x), float(y))
        got = False
        for idx in tree.query(p):
            if polys[idx].covers(p):
                areas[i] = polys[idx].area
                got = True
                break
        if not got:                                 # center on a shared edge -> nearest
            areas[i] = polys[tree.nearest(p)].area
    return areas


def extract_metrics(result_h5, prep: Rog2025Prep, *, precip_mm_hr, storm_hours,
                    catchment_geojson=None, unstructured=False) -> dict:
    """Outlet Q + max depth/velocity + runoff volume + mass balance.

    TRUE cell volume comes from the engine's ``DEBUG/CellVolume`` (the subgrid
    volume-elevation integral) -- ``depth * cell_area`` overcounts on relief (a cell
    with a deep sub-cell channel reports a large depth but a small volume). Outlet Q
    is the mass-balance residual ``R_in - dV/dt`` (a single NormalDepth outlet, so all
    outflow leaves there), cross-checked against the direct ``DEBUG/FaceFlow`` sum
    over the outlet-edge boundary faces. The core metrics are mesh-structure-agnostic
    (subgrid volume + point-in-polygon catchment mask + field maxes); ``unstructured``
    switches the wet-/domain-AREA reporting from a uniform cell_size**2 to true per-cell
    Voronoi areas (the graded mesh has non-uniform cells)."""
    import h5py
    import numpy as np

    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    gbase = "/Geometry/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        depth = f[f"{base}/Cell Depth"][:]                 # (Nt, Nc) m
        vel = f[f"{base}/Face Velocity"][:]                # (Nt, Nf) m/s
        t_days = f["/Results/Output Blocks/Base Output/Time"][:]
        cell_vol = f[f"{base}/DEBUG/CellVolume"][:]        # (Nt, Nc) m3 (TRUE subgrid)
        face_flow = f[f"{base}/DEBUG/FaceFlow"][:]         # (Nt, Nf) m3/s
        face_data = f[f"{gbase}/Face Data"][:]             # (Nf, 4)
        node_xy = f[f"{gbase}/Node Coordinates"][:]        # (Nn, 2)
        cell_xy = f[f"{gbase}/Cell Coordinates"][:]        # (Nc, 2) local m
    t_s = (t_days - t_days[0]) * 86400.0
    nt, nc = depth.shape
    if unstructured:
        cell_areas = _voronoi_cell_areas(cell_xy, prep.width_m, prep.height_m)  # (Nc,) m2
    else:
        cell_areas = np.full(nc, prep.cell_size * prep.cell_size)
    domain_area = float(cell_areas.sum())

    # Restrict the metrics to the delineated catchment (fair like-for-like with the
    # TELEMAC catchment mesh): net flux leaving the catchment cells == pour-point Q.
    if catchment_geojson:
        inmask, catch_area = _catchment_mask(cell_xy, prep, catchment_geojson)
        contrib_area = float(catch_area)
    else:
        inmask = np.ones(nc, dtype=bool)
        contrib_area = domain_area * prep.valid_frac

    # TRUE storage from the subgrid cell-volume tables (catchment cells only).
    V = cell_vol[:, inmask].sum(axis=1)                     # (Nt,) m3
    dVdt = np.gradient(V, t_s)                              # m3/s
    rain_rate_ms = precip_mm_hr / 1000.0 / 3600.0
    storm_s = storm_hours * 3600.0
    R_in = np.where(t_s <= storm_s, rain_rate_ms * contrib_area, 0.0)  # m3/s
    Q_out = np.clip(R_in - dVdt, 0.0, None)                 # outlet hydrograph (mass bal.)
    # the peak is at end-of-storm equilibrium; drop the first two steps (the dry-start
    # dV/dt gradient artifact) for the reported peak.
    hold = np.ones_like(Q_out, dtype=bool); hold[:2] = False
    peak_i = int(np.where(hold)[0][np.argmax(Q_out[hold])])
    peak_q = float(Q_out[peak_i])
    peak_t_hr = float(t_s[peak_i] / 3600.0)

    # DIRECT cross-check: sum |FaceFlow| over boundary faces on the outlet edge.
    # Face Data col1 == -1 marks a perimeter (boundary) face; select those whose
    # midpoint lies on the outlet wall. Node A/B are cols 0.. via the FacePoints;
    # fall back silently if the layout differs.
    q_direct_peak = None
    try:
        na, nb = face_data[:, 0], face_data[:, 2]
        # boundary faces: FaceFlow nonzero across time only at perimeter for outflow
        edge = {"s": ("y", 0.0), "n": ("y", prep.height_m), "w": ("x", 0.0),
                "e": ("x", prep.width_m)}[prep.outlet_edge]
        axis = 0 if edge[0] == "x" else 1
        fmid = 0.5 * (node_xy[na] + node_xy[nb])
        on_edge = np.abs(fmid[:, axis] - edge[1]) < prep.cell_size
        if on_edge.any():
            q_direct = np.abs(face_flow[:, on_edge]).sum(axis=1)
            q_direct_peak = round(float(q_direct.max()), 3)
    except Exception:  # noqa: BLE001
        pass

    max_depth = float(depth[:, inmask].max())
    max_vel = float(np.abs(vel).max())                      # domain max (faces unmasked)
    total_rain_vol = rain_rate_ms * contrib_area * min(storm_s, t_s[-1])
    Q_int = Q_out.copy(); Q_int[:1] = 0.0                  # drop the t=0 spike for volume
    outlet_vol = float(np.trapz(Q_int, t_s)) if hasattr(np, "trapz") else float(np.trapezoid(Q_int, t_s))
    final_storage = float(V[-1])
    # INDEPENDENT rain-application check: early dV/dt (before appreciable outflow)
    # must track R_in -- confirms the constant precip is applied at the set rate.
    early = min(3, nt - 1)
    early_dvdt = float((V[early] - V[0]) / (t_s[early] - t_s[0])) if t_s[early] > 0 else 0.0
    rin = float(rain_rate_ms * contrib_area)
    rain_apply_ratio = round(early_dvdt / rin, 3) if rin > 0 else None
    peak_frame = int(depth[:, inmask].max(axis=1).argmax())
    wet = (depth[peak_frame] > 0.01) & inmask
    wet_cells = int(wet.sum())
    wet_km2 = float(cell_areas[wet].sum()) / 1e6

    return {
        "peak_outlet_q_m3s": round(peak_q, 3),
        "peak_time_hr": round(peak_t_hr, 3),
        "peak_outlet_q_faceflow_m3s": q_direct_peak,        # independent cross-check
        "max_depth_m": round(max_depth, 4),
        "max_velocity_ms": round(max_vel, 4),
        "runoff_volume_1e3_m3": round(outlet_vol / 1e3, 2),
        "total_rain_volume_1e3_m3": round(total_rain_vol / 1e3, 2),
        "final_storage_1e3_m3": round(final_storage / 1e3, 2),
        "rain_apply_ratio_early": rain_apply_ratio,         # ~1.0 confirms 25 mm/hr applied
        "wet_km2": round(wet_km2, 4),
        "contrib_area_km2": round(contrib_area / 1e6, 4),
        "domain_area_km2": round(domain_area / 1e6, 4),
        "n_cells": nc, "n_catchment_cells": int(inmask.sum()),
        "n_steps": nt, "sim_hours": round(t_s[-1] / 3600.0, 3),
        "runoff_coeff": round(outlet_vol / total_rain_vol, 4) if total_rain_vol > 0 else None,
        "hydrograph_t_hr": [round(float(x / 3600.0), 3) for x in t_s],
        "hydrograph_q_m3s": [round(float(x), 3) for x in Q_out],
    }


def build_depth_cog_unstructured(result_h5, prep, out_tif, catchment_geojson=None,
                                 depth_scale=1.0, out_res_m=None):
    """Rasterize the GRADED-mesh per-cell MAX depth to a 4326 COG.

    The structured mapping (cell -> (row,col) by cell_size) is invalid for the graded
    mesh (multiple fine cells collapse into one pixel; coarse gaps). Instead paint a fine
    UTM raster (at ``out_res_m``, ~ the channel cell size) by NEAREST cell center (KDTree),
    mask dry + out-of-catchment, then warp to 4326 -- the same COG tail as the structured
    path. Mesh-agnostic; resolves the fine channel water the coarse raster would smear."""
    import h5py
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.crs import CRS
    from scipy.spatial import cKDTree

    if isinstance(prep, Rog2025Prep):
        prep = asdict(prep)
    W, H = prep["width_m"], prep["height_m"]
    ox, oy, epsg = prep["origin_x"], prep["origin_y"], prep["utm_epsg"]
    if out_res_m is None:
        out_res_m = max(8.0, prep["cell_size"] / 5.0)
    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        depth = f[f"{base}/Cell Depth"][:].max(axis=0)         # (Nc,) per-cell max
        cxy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
    nx = max(1, int(round(W / out_res_m)))
    ny = max(1, int(round(H / out_res_m)))
    xs = (np.arange(nx) + 0.5) * (W / nx)
    ys = (np.arange(ny) + 0.5) * (H / ny)
    gx, gy = np.meshgrid(xs, ys)                                # local metres, y up
    _, idx = cKDTree(cxy).query(np.c_[gx.ravel(), gy.ravel()])  # nearest cell per pixel
    d = depth[idx].astype("float32")
    d[d <= 0.02] = np.nan
    d = d * float(depth_scale)
    grid = d.reshape(ny, nx)[::-1, :]                           # raster row 0 = north
    if catchment_geojson:
        # mask pixels whose UTM location is outside the catchment
        import json as _json
        from shapely.geometry import shape, Point
        from shapely.prepared import prep as shapely_prep
        from shapely.ops import transform as shp_transform
        from pyproj import Transformer
        g = _json.load(open(catchment_geojson))
        feats = g["features"] if isinstance(g, dict) and "features" in g else [g]
        geom = shape(feats[0]["geometry"])
        tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
        pg = shapely_prep(shp_transform(tr, geom))
        uxg = ox + gx[::-1, :]; uyg = oy + gy[::-1, :]
        inside = np.array([[pg.contains(Point(x, y)) for x, y in zip(rx, ry)]
                           for rx, ry in zip(uxg, uyg)])
        grid = np.where(inside, grid, np.nan)

    src_crs = CRS.from_epsg(epsg)
    src_transform = from_origin(ox, oy + ny * out_res_m, W / nx, H / ny)
    dst_crs = CRS.from_epsg(4326)
    dt, dw, dh = calculate_default_transform(src_crs, dst_crs, nx, ny,
                                             ox, oy, ox + W, oy + H)
    out = np.full((dh, dw), np.nan, dtype="float32")
    reproject(source=grid, destination=out, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dt, dst_crs=dst_crs, src_nodata=np.nan, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    prof = {"driver": "COG", "dtype": "float32", "width": dw, "height": dh, "count": 1,
            "crs": dst_crs, "transform": dt, "nodata": np.nan, "compress": "deflate"}
    try:
        with rasterio.open(out_tif, "w", **prof) as dst:
            dst.write(out, 1)
    except Exception:  # noqa: BLE001
        prof.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256)
        with rasterio.open(out_tif, "w", **prof) as dst:
            dst.write(out, 1)
    finite = out[np.isfinite(out)]
    bounds = rasterio.transform.array_bounds(dh, dw, dt)
    return {
        "cog": str(out_tif),
        "bbox4326": [bounds[0], bounds[1], bounds[2], bounds[3]],
        "depth_max": float(finite.max()) if finite.size else 0.0,
        "depth_mean": float(finite.mean()) if finite.size else 0.0,
        "wet_px": int(finite.size),
    }


def build_depth_cog(result_h5, prep, out_tif, catchment_geojson=None, depth_scale=1.0):
    """Rasterize per-cell MAX depth (m) to an EPSG:4326 COG for the depth layer.

    The structured mesh cells map to a regular (ny,nx) grid; per-cell max depth is
    placed into a UTM raster (origin_x/origin_y from the reproject) and warped to
    4326. Cells outside the catchment (if given) are masked. Pure rasterio (no server
    deps) so the server RoG branch just uploads + wraps this in the depth LayerURI.
    (Graded meshes use ``build_depth_cog_unstructured`` instead.)"""
    import h5py
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.crs import CRS

    if isinstance(prep, Rog2025Prep):
        prep = asdict(prep)
    nx, ny, cs = prep["nx"], prep["ny"], prep["cell_size"]
    ox, oy, epsg = prep["origin_x"], prep["origin_y"], prep["utm_epsg"]
    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        depth = f[f"{base}/Cell Depth"][:].max(axis=0)     # (Nc,) per-cell max
        cxy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
    col = np.clip((cxy[:, 0] / cs).astype(int), 0, nx - 1)
    row = np.clip((ny - 1 - (cxy[:, 1] / cs)).astype(int), 0, ny - 1)  # raster row 0 = north
    grid = np.full((ny, nx), np.nan, dtype="float32")
    d = depth.copy(); d[d <= 0.02] = np.nan
    d = d * float(depth_scale)                             # e.g. m->ftUS for the depth preset
    grid[row, col] = d
    if catchment_geojson:
        inmask, _ = _catchment_mask(cxy, Rog2025Prep(**prep), catchment_geojson)
        g2 = np.full((ny, nx), np.nan, dtype="float32")
        g2[row[inmask], col[inmask]] = d[inmask]
        grid = g2

    src_crs = CRS.from_epsg(epsg)
    src_transform = from_origin(ox, oy + ny * cs, cs, cs)
    dst_crs = CRS.from_epsg(4326)
    dt, dw, dh = calculate_default_transform(src_crs, dst_crs, nx, ny,
                                             ox, oy, ox + nx * cs, oy + ny * cs)
    out = np.full((dh, dw), np.nan, dtype="float32")
    reproject(source=grid, destination=out, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dt, dst_crs=dst_crs, src_nodata=np.nan, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    prof = {"driver": "COG", "dtype": "float32", "width": dw, "height": dh, "count": 1,
            "crs": dst_crs, "transform": dt, "nodata": np.nan, "compress": "deflate"}
    try:
        with rasterio.open(out_tif, "w", **prof) as dst:
            dst.write(out, 1)
    except Exception:  # COG driver may be unavailable -> plain tiled GTiff
        prof.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256)
        with rasterio.open(out_tif, "w", **prof) as dst:
            dst.write(out, 1)
    finite = out[np.isfinite(out)]
    bounds = rasterio.transform.array_bounds(dh, dw, dt)  # (w,s,e,n)
    return {
        "cog": str(out_tif),
        "bbox4326": [bounds[0], bounds[1], bounds[2], bounds[3]],
        "depth_max": float(finite.max()) if finite.size else 0.0,     # in the scaled unit
        "depth_mean": float(finite.mean()) if finite.size else 0.0,
        "wet_px": int(finite.size),
    }


def build_depth_frames(result_h5, prep, out_dir, catchment_geojson=None,
                       depth_scale=1.0, refined=False, out_res_m=None):
    """Rasterize EVERY per-step Cell Depth to a 4326 COG (ADR 0287 emit-on-solve).

    The frame sibling of ``build_depth_cog`` / ``build_depth_cog_unstructured``: same
    georef, but reads ``Cell Depth`` per STEP (``[i]``) instead of ``.max(axis=0)``,
    and reads the parallel ``/Results/Output Blocks/Base Output/Time`` (DAYS). The
    placement (structured (row,col) OR the graded-mesh KDTree ``idx``) + the
    catchment mask + the UTM->4326 warp transform are computed ONCE; every step is a
    cheap re-fill + write, so N frames cost N COG writes, not N georef solves.

    NEVER-OMIT (ADR 0287): every managed-engine mapping step is written -- there is
    NO subsample cap. Returns a list of ``{"cog", "bbox4326", "t_days", "depth_max"}``
    (one per step, ascending ``t_days``). Pure rasterio/scipy (no server deps) -- the
    server RoG composer uploads each COG + writes ``outputs.json``. A step that fails
    to write stops the loop (the frames already written stand; the caller degrades to
    whatever landed + the peak)."""
    import os
    import h5py
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.crs import CRS

    if isinstance(prep, Rog2025Prep):
        prep = asdict(prep)
    ox, oy, epsg = prep["origin_x"], prep["origin_y"], prep["utm_epsg"]
    base = "/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh"
    with h5py.File(result_h5, "r") as f:
        depth_steps = f[f"{base}/Cell Depth"][:]                 # (Nt, Nc) m
        cxy = f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
        t_days = f["/Results/Output Blocks/Base Output/Time"][:]  # (Nt,) DAYS
    nt = int(min(depth_steps.shape[0], len(t_days)))

    src_crs = CRS.from_epsg(epsg)
    dst_crs = CRS.from_epsg(4326)

    if refined:
        # graded mesh -> fine UTM raster by nearest cell center (build_depth_cog_
        # unstructured georef); the KDTree idx + inside-mask are step-invariant.
        from scipy.spatial import cKDTree
        W, H = prep["width_m"], prep["height_m"]
        if out_res_m is None:
            out_res_m = max(8.0, prep["cell_size"] / 5.0)
        nx = max(1, int(round(W / out_res_m)))
        ny = max(1, int(round(H / out_res_m)))
        xs = (np.arange(nx) + 0.5) * (W / nx)
        ys = (np.arange(ny) + 0.5) * (H / ny)
        gx, gy = np.meshgrid(xs, ys)                             # local metres, y up
        _, idx = cKDTree(cxy).query(np.c_[gx.ravel(), gy.ravel()])
        inside = None
        if catchment_geojson:
            import json as _json
            from shapely.geometry import shape, Point
            from shapely.prepared import prep as shapely_prep
            from shapely.ops import transform as shp_transform
            from pyproj import Transformer
            g = _json.load(open(catchment_geojson))
            feats = g["features"] if isinstance(g, dict) and "features" in g else [g]
            geom = shape(feats[0]["geometry"])
            tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
            pg = shapely_prep(shp_transform(tr, geom))
            uxg = ox + gx[::-1, :]; uyg = oy + gy[::-1, :]
            inside = np.array([[pg.contains(Point(x, y)) for x, y in zip(rx, ry)]
                               for rx, ry in zip(uxg, uyg)])
        src_transform = from_origin(ox, oy + ny * out_res_m, W / nx, H / ny)
        dt, dw, dh = calculate_default_transform(src_crs, dst_crs, nx, ny,
                                                 ox, oy, ox + W, oy + H)

        def _grid_for(step):
            d = depth_steps[step][idx].astype("float32")
            d[d <= 0.02] = np.nan
            d = d * float(depth_scale)
            grid = d.reshape(ny, nx)[::-1, :]
            if inside is not None:
                grid = np.where(inside, grid, np.nan)
            return grid, src_transform, nx, ny, dt, dw, dh
    else:
        nx, ny, cs = prep["nx"], prep["ny"], prep["cell_size"]
        col = np.clip((cxy[:, 0] / cs).astype(int), 0, nx - 1)
        row = np.clip((ny - 1 - (cxy[:, 1] / cs)).astype(int), 0, ny - 1)
        inmask = None
        if catchment_geojson:
            inmask, _ = _catchment_mask(cxy, Rog2025Prep(**prep), catchment_geojson)
        src_transform = from_origin(ox, oy + ny * cs, cs, cs)
        dt, dw, dh = calculate_default_transform(src_crs, dst_crs, nx, ny,
                                                 ox, oy, ox + nx * cs, oy + ny * cs)

        def _grid_for(step):
            d = depth_steps[step].copy()
            d[d <= 0.02] = np.nan
            d = d * float(depth_scale)
            grid = np.full((ny, nx), np.nan, dtype="float32")
            if inmask is not None:
                grid[row[inmask], col[inmask]] = d[inmask]
            else:
                grid[row, col] = d
            return grid, src_transform, nx, ny, dt, dw, dh

    frames = []
    for step in range(nt):
        grid, s_transform, gnx, gny, d_t, d_w, d_h = _grid_for(step)
        out = np.full((d_h, d_w), np.nan, dtype="float32")
        reproject(source=grid, destination=out, src_transform=s_transform, src_crs=src_crs,
                  dst_transform=d_t, dst_crs=dst_crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
        out_tif = os.path.join(str(out_dir), f"rog_depth_frame_{step + 1:02d}.tif")
        prof = {"driver": "COG", "dtype": "float32", "width": d_w, "height": d_h, "count": 1,
                "crs": dst_crs, "transform": d_t, "nodata": np.nan, "compress": "deflate"}
        try:
            with rasterio.open(out_tif, "w", **prof) as dst:
                dst.write(out, 1)
        except Exception:  # noqa: BLE001 -- COG driver may be unavailable
            prof.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256)
            with rasterio.open(out_tif, "w", **prof) as dst:
                dst.write(out, 1)
        finite = out[np.isfinite(out)]
        bounds = rasterio.transform.array_bounds(d_h, d_w, d_t)  # (w,s,e,n)
        frames.append({
            "cog": str(out_tif),
            "bbox4326": [bounds[0], bounds[1], bounds[2], bounds[3]],
            "t_days": float(t_days[step]),
            "depth_max": float(finite.max()) if finite.size else 0.0,
        })
    return frames


def run_rog2025(dem_tif, workdir, *, precip_mm_hr=25.0, storm_hours=6.0,
                sim_hours=None, cell_size=60.0, elev_units="m", bbox4326=None,
                pour_point=None, outlet_edge=None, dt_s=None, report_every=None,
                outlet_slope=0.05, outlet_bc="normal_depth", diffusion=True,
                catchment_geojson=None, channel_refinement=None, flowlines_path=None,
                image=AUTHORING_IMAGE_DEFAULT, probe_dir=PROBE_DIR_DEFAULT) -> dict:
    """Full RoG-2025 pipeline; returns prep + metrics + provenance.

    ``channel_refinement``: None -> the uniform structured mesh (cell_size
    everywhere). Otherwise paper-style graded refinement -- a ``rog_refine.RefineConfig``
    OR a float target channel cell size (m). The channel network comes from
    ``flowlines_path`` (a river-geometry vector the caller fetched for the AOI); the
    mesh grades from ``cell_size`` (background) down to the channel size along it. Finer
    channel cells drive a smaller CFL time step."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if sim_hours is None:
        # constant design storm for the whole window (matches the paper's 25mm/hr
        # x 6h forcing); the outlet hydrograph peaks at end-of-storm equilibrium.
        sim_hours = storm_hours

    prep = prepare_local_terrain(
        dem_tif, workdir, cell_size=cell_size, elev_units=elev_units,
        bbox4326=bbox4326, pour_point=pour_point, outlet_edge=outlet_edge,
        terrain_res=_terrain_res_for(cell_size, channel_refinement))

    refine = None
    if channel_refinement is not None:
        if flowlines_path is None or catchment_geojson is None:
            raise Rog2025Error(
                "channel_refinement needs flowlines_path + catchment_geojson (the channel "
                "network + modeled domain to grade toward)")
        from rog_refine import build_refined_inputs, RefineConfig
        if isinstance(channel_refinement, RefineConfig):
            cfg = channel_refinement
        else:  # a target channel cell size (m)
            cfg = RefineConfig(background_m=float(cell_size), channel_m=float(channel_refinement))
        stage = Path(probe_dir) / f"rog_{workdir.name}" / "refine"
        refine = build_refined_inputs(prep, catchment_geojson, flowlines_path, stage, cfg)
        finest = refine.size_p5                              # realized channel cell size
    else:
        finest = cell_size

    if dt_s is None:
        # CFL-ish: shallow overland flow, keep courant < ~1 for the FINEST cell
        dt_s = max(1.0, min(10.0, finest / 15.0))
    if report_every is None:
        report_every = max(1, int(round((300.0) / dt_s)))   # ~5-min output

    # outlet tailwater = the bed elevation on the pour-point edge (a free-ish
    # outfall for rain-on-grid); ConstantStage is the proven-external outlet BC.
    outlet_stage = prep.elev_min_m
    result_h5, wall = _author_prepare_solve(
        prep, workdir, precip_mm_hr=precip_mm_hr, storm_hours=storm_hours,
        sim_hours=sim_hours, dt_s=dt_s, report_every=report_every,
        outlet_slope=outlet_slope, diffusion=diffusion, outlet_bc=outlet_bc,
        outlet_stage=outlet_stage, image=image, probe_dir=probe_dir, refine=refine)
    # rain forces the whole plan window (the constant precip BC spans start..end),
    # so the effective storm length for the mass balance is the sim length.
    metrics = extract_metrics(result_h5, prep, precip_mm_hr=precip_mm_hr,
                              storm_hours=sim_hours, catchment_geojson=catchment_geojson,
                              unstructured=(refine is not None))
    out = {
        "result_h5": str(result_h5), "wall_s": round(wall, 1),
        "prep": asdict(prep), "metrics": metrics, "dt_s": dt_s,
        "precip_mm_hr": precip_mm_hr, "storm_hours": storm_hours, "sim_hours": sim_hours,
        "engine": "HEC-RAS 2025 managed (CPU, beta)", "infiltration": "absent (rain-only)",
        "mesh": "graded (channel-refined)" if refine is not None else "uniform structured",
    }
    if refine is not None:
        out["refine"] = asdict(refine)
    return out


def run_rog2025_prebuilt(prep_doc, local_dem, seeds_path, breaklines_path, workdir, *,
                         precip_mm_hr=25.0, storm_hours=6.0, sim_hours=None,
                         diffusion=True, catchment_geojson=None,
                         image=AUTHORING_IMAGE_DEFAULT, probe_dir=PROBE_DIR_DEFAULT) -> dict:
    """Consume a PRE-BUILT channel-refined mesh: re-realize + solve.

    The ``generate_mesh`` / gate consume path. Skips ``prepare_local_terrain`` AND
    ``rog_refine.build_refined_inputs`` -- the local terrain frame (``prep_doc`` +
    ``local_dem``) and the graded seeds/breaklines are the STORED artifact inputs, so
    NO fresh DEM reprojection, delineation, or re-seeding happens. The 2025 driver
    re-realizes the SAME cell mesh from the identical seeds (deterministic) and solves
    it. Metrics are the graded-mesh (unstructured) path. Returns the run_rog2025 shape."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if sim_hours is None:
        sim_hours = storm_hours

    # reconstruct the frame; point local_dem at the downloaded terrain
    doc = dict(prep_doc)
    doc["local_dem"] = str(local_dem)
    prep = Rog2025Prep(**{k: doc[k] for k in Rog2025Prep.__dataclass_fields__ if k in doc})

    # stage the stored seeds/breaklines where the container sees them (the refine dir
    # _author_prepare_solve points the driver at); no build_refined_inputs call.
    stage_refine = Path(probe_dir) / f"rog_{workdir.name}" / "refine"
    stage_refine.mkdir(parents=True, exist_ok=True)
    (stage_refine / "seeds.f64").write_bytes(Path(seeds_path).read_bytes())
    (stage_refine / "breaklines.json").write_bytes(Path(breaklines_path).read_bytes())

    finest = doc.get("channel_m_realized") or max(1.0, prep.cell_size / 4.0)
    dt_s = max(1.0, min(10.0, float(finest) / 15.0))
    report_every = max(1, int(round(300.0 / dt_s)))
    outlet_stage = prep.elev_min_m
    result_h5, wall = _author_prepare_solve(
        prep, workdir, precip_mm_hr=precip_mm_hr, storm_hours=storm_hours,
        sim_hours=sim_hours, dt_s=dt_s, report_every=report_every,
        outlet_slope=0.05, diffusion=diffusion, outlet_bc="normal_depth",
        outlet_stage=outlet_stage, image=image, probe_dir=probe_dir, refine=True)
    metrics = extract_metrics(result_h5, prep, precip_mm_hr=precip_mm_hr,
                              storm_hours=sim_hours, catchment_geojson=catchment_geojson,
                              unstructured=True)
    return {
        "result_h5": str(result_h5), "wall_s": round(wall, 1),
        "prep": asdict(prep), "metrics": metrics, "dt_s": dt_s,
        "precip_mm_hr": precip_mm_hr, "storm_hours": storm_hours, "sim_hours": sim_hours,
        "engine": "HEC-RAS 2025 managed (CPU, beta)", "infiltration": "absent (rain-only)",
        "mesh": "graded (channel-refined, consumed from a prebuilt generate_mesh artifact)",
    }


def _terrain_res_for(cell_size, channel_refinement):
    """Terrain raster step: must stay FINER than the finest mesh cell so face profiles
    sub-sample (a terrain at the mesh cell size makes prepare report 'Missing terrain
    data'). Keep the prepare_local_terrain DEFAULT (max(5, cell/6)) whenever it is already
    finer than the channel cell -- overriding it shifts the reprojection origin sub-pixel,
    which perturbs the graded seed cloud; only go finer when the channel cell demands it."""
    if channel_refinement is None:
        return None                                         # prepare_local_terrain default
    from rog_refine import RefineConfig
    ch = (channel_refinement.channel_m if isinstance(channel_refinement, RefineConfig)
          else float(channel_refinement))
    default_tr = max(5.0, cell_size / 6.0)
    if default_tr <= 0.9 * ch:
        return None                                         # default already fine enough
    return max(4.0, ch / 2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dem_tif")
    ap.add_argument("workdir")
    ap.add_argument("--precip-mm-hr", type=float, default=25.0)
    ap.add_argument("--storm-hours", type=float, default=6.0)
    ap.add_argument("--sim-hours", type=float, default=None)
    ap.add_argument("--cell-size", type=float, default=60.0)
    ap.add_argument("--elev-units", default="m")
    ap.add_argument("--outlet-edge", default=None)
    ap.add_argument("--pour-point", default=None, help="lon,lat")
    ap.add_argument("--dt-s", type=float, default=None)
    ap.add_argument("--full-swe", action="store_true")
    ap.add_argument("--catchment", default=None, help="catchment geojson (restrict metrics)")
    args = ap.parse_args()
    pp = None
    if args.pour_point:
        pp = tuple(float(x) for x in args.pour_point.split(","))
    out = run_rog2025(
        args.dem_tif, args.workdir, precip_mm_hr=args.precip_mm_hr,
        storm_hours=args.storm_hours, sim_hours=args.sim_hours, cell_size=args.cell_size,
        elev_units=args.elev_units, outlet_edge=args.outlet_edge, pour_point=pp,
        dt_s=args.dt_s, diffusion=not args.full_swe, catchment_geojson=args.catchment)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
