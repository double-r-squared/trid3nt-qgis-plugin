"""Ball Creek CHANNEL-RESOLVING mesh (lever 2).

Regenerates the Ball Creek watershed mesh with a TIGHTER channel band -- channel
edge ~18 m (vs the coarse 30 m floor) while RAISING the hillslope
ceiling to 300 m (vs 200 m) so the extra channel resolution is paid for by
coarser hillslopes, keeping the total node count sane (the HEC-RAS
lesson: fewer total cells, more of them in the channel).

The catchment is IDENTICAL to the coarse run (same pour point) so this REUSES
the cached delineation (catchment.geojson), river network (flowlines.fgb), bare-
earth bed (dem_bed.tif) and NLCD (nlcd.tif) staged by the coarse build,
and re-runs ONLY the OceanMesh2D sizing with the tight band -- no pysheds
delineation, no re-fetch (the delineation does not depend on edge length). Stages
watershed.slf + node CN2/Manning against the new mesh, writes a channel-band edge
histogram, reports the stats before/after. Separate rundir so the coarse ladder
rung is preserved. ASCII only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from trid3nt_server.workflows.mesh import watershed as W  # noqa: E402
from trid3nt_server.workflows.mesh.telemac_build import (  # noqa: E402
    write_bottom_selafin)
from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (  # noqa: E402
    landcover_cn_manning, node_curve_numbers)

COARSE_RUNDIR = Path(os.environ.get("ROG_RUNDIR", "/tmp/rog_ballcreek"))
FINE_RUNDIR = Path("/home/nate/rog_ballcreek_fine")
MIN_EDGE_FINE = 18.0   # channel edge target (~15-25 m band); coarse floor was 30 m
MAX_EDGE_FINE = 300.0  # hillslope ceiling raised to hold total node count sane
POUR_POINT = (-83.43131, 35.05701)


def channel_band_histogram(rundir: Path, coarse: Path) -> dict:
    """Edge-length histogram split by distance-to-river, from the staged mesh +
    the (cached) flowlines. Channel band = edges whose midpoint is within 40 m of
    the mapped river network."""
    import geopandas as gpd
    from shapely.geometry import Point
    from shapely.ops import unary_union

    npz = np.load(rundir / "coastal_tin_mesh.npz")
    facts = json.loads((rundir / "mesh_facts.json").read_text())
    pts_ll = npz["points"]
    pts_m, epsg = W.reproject_nodes_to_utm(pts_ll)
    X = np.asarray(pts_m)[:, 0]
    Y = np.asarray(pts_m)[:, 1]
    ikle = np.asarray(npz["cells"], np.int64)
    edges = set()
    for t in ikle:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            edges.add((min(int(a), int(b)), max(int(a), int(b))))
    ea = np.array(sorted(edges), dtype=np.int64)
    L = np.hypot(X[ea[:, 0]] - X[ea[:, 1]], Y[ea[:, 0]] - Y[ea[:, 1]])

    fl = gpd.read_file(coarse / "flowlines.fgb").to_crs(int(facts["utm_epsg"]))
    riv = unary_union(list(fl.geometry))
    mx = 0.5 * (X[ea[:, 0]] + X[ea[:, 1]])
    my = 0.5 * (Y[ea[:, 0]] + Y[ea[:, 1]])
    d = np.array([riv.distance(Point(x, y)) for x, y in zip(mx, my)])
    chan = d <= 40.0
    return {
        "n_nodes": int(X.shape[0]), "n_cells": int(ikle.shape[0]),
        "n_edges": int(L.shape[0]),
        "edge_min_m": round(float(L.min()), 1),
        "edge_median_m": round(float(np.median(L)), 1),
        "edge_max_m": round(float(L.max()), 1),
        "channel_band_edges": int(chan.sum()),
        "channel_edge_min_m": round(float(L[chan].min()), 1) if chan.any() else None,
        "channel_edge_median_m": round(float(np.median(L[chan])), 1) if chan.any() else None,
        "channel_edge_p90_m": round(float(np.percentile(L[chan], 90)), 1) if chan.any() else None,
        "hillslope_edge_median_m": round(float(np.median(L[~chan])), 1) if (~chan).any() else None,
    }


def regen() -> dict:
    import geopandas as gpd
    from shapely.geometry import shape

    FINE_RUNDIR.mkdir(parents=True, exist_ok=True)
    # 1. reuse the cached catchment polygon + river network (same catchment).
    cj = json.loads((COARSE_RUNDIR / "catchment.geojson").read_text())
    catch = shape(cj["features"][0]["geometry"])
    flow = gpd.read_file(COARSE_RUNDIR / "flowlines.fgb")

    # 2. exterior + river sizing points at the TIGHT band, mesh config, mesher.
    boubox, river = W.catchment_exterior_and_river_coords(
        catch, flow, min_edge_length_m=MIN_EDGE_FINE)
    cfg = W.build_mesh_config(
        boubox, river, min_edge_length_m=MIN_EDGE_FINE,
        max_edge_length_m=MAX_EDGE_FINE, grade=W.DEFAULT_GRADE,
        max_iter=W.DEFAULT_MAX_ITER)
    image = os.environ.get("TRID3NT_MESH_IMAGE") or W.DEFAULT_MESH_IMAGE
    sandbox = Path(os.environ.get("TRID3NT_OCEANMESH_SANDBOX")
                   or (REPO / "scripts/sandbox/oceanmesh")).resolve()
    print(f"[finemesh] meshing tight band min={MIN_EDGE_FINE} max={MAX_EDGE_FINE} ...", flush=True)
    points_ll, cells, stats = W._run_mesh_container(
        FINE_RUNDIR, cfg, image=image, sandbox=sandbox)
    points_ll = np.asarray(points_ll, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)

    # 3. bed from the cached bare-earth DEM; project + write the solve SELAFIN.
    bed = W.sample_raster_at_nodes(COARSE_RUNDIR / "dem_bed.tif", points_ll)
    points_m, epsg = W.reproject_nodes_to_utm(points_ll)
    write_bottom_selafin(str(FINE_RUNDIR / "watershed.slf"), points_m, cells, bed)

    # 4. per-node CN2/Manning from the cached NLCD (same NLCD-distributed scheme).
    nlcd_vals = W.sample_raster_at_nodes(COARSE_RUNDIR / "nlcd.tif", points_ll)
    node_nlcd = [int(round(v)) for v in nlcd_vals]
    manning = [landcover_cn_manning(c)[1] for c in node_nlcd]
    cn2 = node_curve_numbers(node_nlcd, uniform_cn=None)
    (FINE_RUNDIR / "node_cn2.txt").write_text("\n".join(f"{v:.3f}" for v in cn2) + "\n")
    (FINE_RUNDIR / "node_manning.txt").write_text("\n".join(f"{v:.3f}" for v in manning) + "\n")

    # area from the UTM triangulation.
    Xm = points_m[:, 0]; Ym = points_m[:, 1]
    a = cells
    area = float(np.abs(0.5 * ((Xm[a[:, 1]] - Xm[a[:, 0]]) * (Ym[a[:, 2]] - Ym[a[:, 0]])
                 - (Xm[a[:, 2]] - Xm[a[:, 0]]) * (Ym[a[:, 1]] - Ym[a[:, 0]]))).sum())
    coarse_outlet = json.loads((COARSE_RUNDIR / "mesh_facts.json").read_text())["outlet_lonlat"]
    (FINE_RUNDIR / "mesh_facts.json").write_text(json.dumps({
        "npoin": int(points_m.shape[0]), "nelem": int(cells.shape[0]),
        "utm_epsg": int(epsg), "area_km2": area / 1e6,
        "outlet_lonlat": coarse_outlet, "pour_point_lonlat": list(POUR_POINT),
        "cn2_min": float(np.min(cn2)), "cn2_max": float(np.max(cn2)),
        "manning_min": float(np.min(manning)), "manning_max": float(np.max(manning)),
        "nlcd_classes": sorted(set(node_nlcd)),
    }, indent=2))

    hist = channel_band_histogram(FINE_RUNDIR, COARSE_RUNDIR)
    (FINE_RUNDIR / "channel_band_histogram.json").write_text(json.dumps(hist, indent=2))
    print("[finemesh] fine:", json.dumps(hist), flush=True)
    # coarse comparison
    ch = json.loads((COARSE_RUNDIR / "mesh_stats.json").read_text())
    print(f"[finemesh] COARSE was: n_points={ch['n_points']} n_cells={ch['n_cells']} "
          f"edge_min={ch['edge_min_m']} edge_median={ch['edge_median_m']} edge_max={ch['edge_max_m']}",
          flush=True)
    return hist


if __name__ == "__main__":
    print(json.dumps(regen(), indent=2))
