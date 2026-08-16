"""Genuine SFINCS QUADTREE build+solve through the worker image, end to end.

Composes a real ``sfincs_build_spec`` over a coastal topobathy DEM, stages it +
the DEM to MinIO, runs the ``trid3nt-local/sfincs`` worker in ``--build-spec-uri``
build+solve mode (cht_sfincs authors the variable-resolution 2:1-balanced grid,
the SFINCS binary solves it, the worker rasterizes the face-indexed output), then
reads the genuine ``sfincs_map.nc`` back, publishes it as the ADR 0159 native
UGRID mesh ``LayerURI`` + a peak-depth COG, and renders the two proof images.

The DEM is a fetched Mexico Beach / Hurricane Michael topobathy COG (EPSG:32616,
NAVD88 m, positive-up, bathymetry negative) already localized under
``data/runs/``. The quadtree config (base resolution + coast refine level) is the
user granularity lever; the design-storm surge water-level boundary rides in via
the deck builder's return-period fallback.

Env: the full MinIO block (AWS_ENDPOINT_URL / AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY / AWS_REGION + TRID3NT_CACHE_BUCKET / TRID3NT_RUNS_BUCKET).
Docker is reached via ``sg docker`` (the invoking user is not in the docker
group). Run from the repo root.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
import numpy as np
import requests
import xarray as xr
from PIL import Image
from pyproj import Transformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_sfincs_quadtree")

REPO = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("TRID3NT_SFINCS_IMAGE", "trid3nt-local/sfincs:latest")
CACHE_BUCKET = os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
ENDPOINT = os.environ["AWS_ENDPOINT_URL"]

# Mexico Beach / Hurricane Michael topobathy (EPSG:32616, 3 m, 86% wet).
DEM_PATH = REPO / "data/runs/01KZ9GNGARV2RHCEJMVWGS69DW/bathy.tif"
BBOX = (-85.5522, 29.6983, -85.3976, 29.8517)  # EPSG:4326 (w, s, e, n)

# Quadtree granularity lever: coarse base, refine to level 3 at the coast; the
# generator's 2:1 balance auto-inserts the intermediate level-2 buffer ring.
BASE_RES_M = 400.0
COAST_REFINE_LEVEL = 3
MAX_REFINE_LEVEL = 3
RETURN_PERIOD_YR = 100
SIM_HOURS = 12.0
OUTPUT_INTERVAL_MIN = 30.0

OUT_DIR = REPO / "docs/proof/templates"
TMP = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
           "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad")
TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def _run_id() -> str:
    return "01QT" + time.strftime("%y%m%d%H%M%S") + "MEXBEACH"


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _ensure_bucket(s3, name: str) -> None:
    try:
        s3.head_bucket(Bucket=name)
    except Exception:
        s3.create_bucket(Bucket=name)
        log.info("created bucket %s", name)


def _build_spec(run_id: str, dem_uri: str) -> dict:
    return {
        "schema_version": 1,
        "engine": "sfincs",
        "run_id": run_id,
        "bbox": list(BBOX),
        "nlcd_vintage_year": None,
        "inputs": {"dem_uri": dem_uri},
        "forcing": {"forcing_type": "waterlevel"},
        "options": {
            "simulation_hours": SIM_HOURS,
            "output_interval_min": OUTPUT_INTERVAL_MIN,
            "return_period_yr": RETURN_PERIOD_YR,
            "quadtree": {
                "base_resolution_m": BASE_RES_M,
                "coast_refine_level": COAST_REFINE_LEVEL,
                "max_refine_level": MAX_REFINE_LEVEL,
            },
        },
    }


def _docker_build_solve(run_id: str, spec_uri: str) -> int:
    env_flags = " ".join(
        f"-e {k}={os.environ[k]}"
        for k in ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID",
                  "AWS_SECRET_ACCESS_KEY", "AWS_REGION")
    )
    cmd = (
        f"docker run --rm --network host --name {run_id} "
        f"{env_flags} "
        f"-e TRID3NT_OBJECT_STORE=s3 "
        f"-e TRID3NT_CACHE_BUCKET={CACHE_BUCKET} "
        f"-e TRID3NT_RUNS_BUCKET={RUNS_BUCKET} "
        f"{IMAGE} --run-id {run_id} --build-spec-uri {spec_uri}"
    )
    log.info("docker build+solve: %s", cmd)
    proc = subprocess.run(["sg", "docker", "-c", cmd], text=True)
    return proc.returncode


# --------------------------------------------------------------------------- #
# Esri basemap tiling (shared with the other proof renderers).
# --------------------------------------------------------------------------- #
def _tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def _tile_bounds_3857(x, y, z):
    n = 2 ** z

    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)

    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def _fetch_basemap(w, s, e, n, zoom):
    x0f, y1f = _tile_xy(w, s, zoom)
    x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                         (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _face_polys_3857(ds, src_epsg):
    """UGRID face polygons (node coords + connectivity) reprojected to 3857."""
    nx = np.asarray(ds["mesh2d_node_x"].values, dtype="float64").ravel()
    ny = np.asarray(ds["mesh2d_node_y"].values, dtype="float64").ravel()
    conn_var = ds["mesh2d_face_nodes"]
    conn = np.asarray(conn_var.values, dtype="float64")
    start = int(conn_var.attrs.get("start_index", 1))
    to3857 = Transformer.from_crs(src_epsg, "EPSG:3857", always_xy=True)
    lx, ly = to3857.transform(nx, ny)
    polys, sizes = [], []
    for row in conn:
        idx = [int(k - start) for k in row
               if np.isfinite(k) and (k - start) >= 0 and (k - start) < nx.shape[0]]
        if len(idx) < 3:
            continue
        ring = list(zip(lx[idx], ly[idx]))
        polys.append(ring)
        # cell size from area in the projected metres CRS
        ax = nx[idx]
        ay = ny[idx]
        area = 0.5 * abs(np.dot(ax, np.roll(ay, 1)) - np.dot(ay, np.roll(ax, 1)))
        sizes.append(math.sqrt(area))
    return polys, np.asarray(sizes)


def _render_mesh(ds, src_epsg, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection

    polys, sizes = _face_polys_3857(ds, src_epsg)
    w, s, e, n = BBOX
    px, py = (e - w) * 0.06, (n - s) * 0.06
    bm, ext = _fetch_basemap(w - px, s - py, e + px, n + py, 12)
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.imshow(bm, extent=ext, origin="upper")
    pc = PolyCollection(polys, facecolors="none", edgecolors="#00e5ff",
                        linewidths=0.35, zorder=5)
    ax.add_collection(pc)
    x0, y0 = TO_3857.transform(w, s)
    x1, y1 = TO_3857.transform(e, n)
    ax.set_xlim(x0 - (x1 - x0) * 0.06, x1 + (x1 - x0) * 0.06)
    ax.set_ylim(y0 - (y1 - y0) * 0.06, y1 + (y1 - y0) * 0.06)
    ax.set_xticks([])
    ax.set_yticks([])
    finest = float(sizes.min()) if sizes.size else 0.0
    coarse = float(sizes.max()) if sizes.size else 0.0
    ax.set_title(
        f"SFINCS native quadtree mesh (cht_sfincs generator) -- "
        f"{len(polys)} cells, 2:1-balanced\n"
        f"cell size {coarse:.0f} m (offshore) -> {finest:.0f} m (coast)  |  "
        f"Mexico Beach FL  |  basemap: Esri World Imagery",
        fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    log.info("wrote %s", out_path)
    return {"cells": len(polys), "finest_m": finest, "coarse_m": coarse}


def _render_depth(cog_path, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from rasterio.warp import transform_bounds, reproject, Resampling, calculate_default_transform

    with rasterio.open(cog_path) as srcds:
        dst_crs = "EPSG:3857"
        tr, wpx, hpx = calculate_default_transform(
            srcds.crs, dst_crs, srcds.width, srcds.height, *srcds.bounds)
        data = np.full((hpx, wpx), np.nan, dtype="float32")
        reproject(source=rasterio.band(srcds, 1), destination=data,
                  src_transform=srcds.transform, src_crs=srcds.crs,
                  dst_transform=tr, dst_crs=dst_crs, resampling=Resampling.bilinear,
                  dst_nodata=np.nan)
        w3, s3, e3, n3 = transform_bounds(srcds.crs, dst_crs, *srcds.bounds)
    w, s, e, n = BBOX
    px, py = (e - w) * 0.06, (n - s) * 0.06
    bm, ext = _fetch_basemap(w - px, s - py, e + px, n + py, 12)
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.imshow(bm, extent=ext, origin="upper")
    finite = data[np.isfinite(data)]
    vmax = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
    im = ax.imshow(np.ma.masked_invalid(data), extent=(w3, e3, s3, n3),
                   origin="upper", cmap="viridis", vmin=0, vmax=vmax,
                   alpha=0.82, zorder=4)
    x0, y0 = TO_3857.transform(w, s)
    x1, y1 = TO_3857.transform(e, n)
    ax.set_xlim(x0 - (x1 - x0) * 0.06, x1 + (x1 - x0) * 0.06)
    ax.set_ylim(y0 - (y1 - y0) * 0.06, y1 + (y1 - y0) * 0.06)
    ax.set_xticks([])
    ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.6)
    cb.set_label("peak water depth (m)")
    ax.set_title(
        "SFINCS quadtree peak depth (rasterized from the face-indexed "
        "sfincs_map.nc)\nMexico Beach FL  |  basemap: Esri World Imagery",
        fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    log.info("wrote %s", out_path)
    return {"vmax_m": vmax}


def main() -> int:
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "contracts"))
    from trid3nt_server.agent.workflows.sfincs.postprocess_sfincs import (
        _extract_peak_depth_geotiff,
        _is_quadtree_output,
        _maybe_native_mesh_layer,
        _native_mesh_source_uri,
    )

    run_id = _run_id()
    s3 = _s3()
    _ensure_bucket(s3, CACHE_BUCKET)
    _ensure_bucket(s3, RUNS_BUCKET)

    dem_key = f"{run_id}/dem.tif"
    log.info("upload DEM %s -> s3://%s/%s", DEM_PATH, CACHE_BUCKET, dem_key)
    s3.upload_file(str(DEM_PATH), CACHE_BUCKET, dem_key)
    dem_uri = f"s3://{CACHE_BUCKET}/{dem_key}"

    spec = _build_spec(run_id, dem_uri)
    spec_key = f"{run_id}/sfincs_build_spec.json"
    s3.put_object(Bucket=CACHE_BUCKET, Key=spec_key,
                  Body=json.dumps(spec).encode("utf-8"))
    spec_uri = f"s3://{CACHE_BUCKET}/{spec_key}"
    log.info("staged build_spec -> %s", spec_uri)

    rc = _docker_build_solve(run_id, spec_uri)
    log.info("container exit code = %s", rc)

    comp = json.loads(s3.get_object(
        Bucket=RUNS_BUCKET, Key=f"{run_id}/completion.json")["Body"].read())
    log.info("completion.json: status=%s exit=%s error=%s",
             comp.get("status"), comp.get("exit_code"), comp.get("error"))
    log.info("deck provenance: %s", json.dumps(comp.get("deck") or {}, indent=2))

    nc_local = TMP / f"{run_id}_sfincs_map.nc"
    s3.download_file(RUNS_BUCKET, f"{run_id}/sfincs_map.nc", str(nc_local))
    log.info("downloaded sfincs_map.nc -> %s (%d bytes)",
             nc_local, nc_local.stat().st_size)

    ds = xr.open_dataset(str(nc_local))
    is_qt = _is_quadtree_output(ds)
    n_nodes = int(ds.sizes.get("mesh2d_nNodes", ds.sizes.get("nmesh2d_node", 0)))
    n_faces = int(ds.sizes.get("nmesh2d_face", 0))
    ugrid_vars = [v for v in ds.variables if "mesh2d" in v]
    log.info("UGRID: quadtree=%s nodes=%s faces=%s vars=%s",
             is_qt, n_nodes, n_faces, ugrid_vars)
    src_epsg = int(spec_from_deck(comp))
    ds.close()

    runs_uri = f"s3://{RUNS_BUCKET}/{run_id}/"
    mesh_uri = _native_mesh_source_uri(runs_uri, nc_local)
    mesh_layer = _maybe_native_mesh_layer(nc_local, mesh_uri, run_id)
    log.info("native mesh LayerURI: %s", mesh_layer.model_dump_json()
             if mesh_layer else None)

    cog_path, depth_meta = _extract_peak_depth_geotiff(nc_local)
    log.info("peak depth COG: %s meta=%s", cog_path, depth_meta)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ds2 = xr.open_dataset(str(nc_local))
    mesh_stats = _render_mesh(ds2, src_epsg,
                              OUT_DIR / "sfincs_native_quadtree_mesh_mesh.png")
    ds2.close()
    depth_stats = _render_depth(cog_path,
                                OUT_DIR / "sfincs_native_quadtree_mesh_depth.png")

    summary = {
        "run_id": run_id,
        "bbox": list(BBOX),
        "aoi": "Mexico Beach FL (Hurricane Michael lineage)",
        "dem": str(DEM_PATH),
        "quadtree_options": spec["options"]["quadtree"],
        "container_exit": rc,
        "completion": comp,
        "ugrid": {"quadtree": is_qt, "nodes": n_nodes, "faces": n_faces,
                  "grid_crs_epsg": src_epsg},
        "native_mesh_layer": mesh_layer.model_dump(mode="json") if mesh_layer else None,
        "depth_meta": depth_meta,
        "mesh_render": mesh_stats,
        "depth_render": depth_stats,
    }
    (TMP / f"{run_id}_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("\n=== QUADTREE RUN SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    return 0 if comp.get("status") == "ok" and mesh_layer is not None else 1


def spec_from_deck(comp: dict) -> int:
    """Grid CRS EPSG from the deck provenance (falls back to the AOI UTM zone)."""
    deck = comp.get("deck") or {}
    epsg = deck.get("grid_crs_epsg")
    if epsg:
        return int(epsg)
    lon_c = 0.5 * (BBOX[0] + BBOX[2])
    lat_c = 0.5 * (BBOX[1] + BBOX[3])
    zone = int((lon_c + 180.0) // 6.0) + 1
    return (32600 if lat_c >= 0 else 32700) + zone


if __name__ == "__main__":
    sys.exit(main())
