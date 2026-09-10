#!/usr/bin/env python
"""Live driver: a LAKE domain, meshed from the water body's own polygon.

The om2d mesher cuts its default domain from GSHHG land polygons, which hold no
lake, so it refuses there. The way through is the chain: the water body is
FETCHED, narrowed to the question's extent, and handed in as a POLYGON extent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

__all__ = ["lake_polygon", "main", "render"]

#: Marquette Lower Harbor, Lake Superior - the domain GSHHG cannot describe.
DEFAULT_BBOX = (-87.39234, 46.52812, -87.36788, 46.55021)
DEFAULT_EDGE_LENGTH_M = 60.0

#: The bed a Great Lakes domain is painted from. The coastal CUDEM composite does
#: not reach the lakes at all, so its own ladder refuses there; the NCEI Great
#: Lakes collection is the lake-datum bathymetry that does.
BED = "fetch_greatlakes_bathymetry"


def lake_polygon(bbox: tuple[float, ...], out_dir: Path) -> str:
    """The water body over ``bbox``, narrowed to it -> a GeoJSON path. NHD answers
    with WHOLE features, so the narrowing to the part the question is about is
    the chain's job, never the mesher's."""
    import geopandas as gpd
    from shapely.geometry import box as _box

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_nhd_waterbodies"].fn(bbox=list(bbox))
    local = out_dir / "waterbodies.fgb"
    local.write_bytes(read_object_bytes_s3(layer.uri)
                      if str(layer.uri).startswith("s3://")
                      else Path(layer.uri).read_bytes())
    bodies = gpd.read_file(local).to_crs(4326)
    if bodies.empty:
        raise SystemExit(f"NO WATER BODY: NHD carries none over {tuple(bbox)}")
    clipped = bodies.intersection(_box(*bbox))
    clipped = clipped[~clipped.is_empty]
    out = out_dir / "lake_domain.geojson"
    gpd.GeoSeries([clipped.union_all()], crs=4326).to_file(out, driver="GeoJSON")
    print(json.dumps({
        "water_bodies": int(len(bodies)),
        "named": [n for n in bodies.get("gnis_name", []) if n][:4],
        "domain": str(out)}))
    return str(out)


def render(mesh: Any, domain: str, out_dir: Path, title: str) -> str:
    """The mesh over the water polygon it was cut from, wireframe on."""
    import geopandas as gpd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.tri as mtri
    import numpy as np

    points = np.asarray(mesh.points, dtype=float)
    cells = np.asarray(mesh.cells)
    tri = mtri.Triangulation(points[:, 0], points[:, 1], cells)
    edge = np.linalg.norm(points[cells[:, 0]] - points[cells[:, 1]], axis=1)
    water = gpd.read_file(domain).to_crs(mesh.crs_authid)

    figure, axes = plt.subplots(figsize=(10, 10))
    face = axes.tripcolor(tri, facecolors=edge, cmap="magma_r", edgecolors="none",
                          alpha=0.85)
    axes.triplot(tri, color="#102030", lw=0.25, alpha=0.9)
    water.boundary.plot(ax=axes, color="#00d0ff", lw=1.4)
    figure.colorbar(face, ax=axes, fraction=0.03, label="element edge length (m)")
    axes.set_aspect("equal")
    axes.set_title(title, fontsize=9)
    path = out_dir / "lake_domain_mesh.png"
    figure.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(figure)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=float, nargs=4, default=list(DEFAULT_BBOX),
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
    ap.add_argument("--edge-length-m", type=float, default=DEFAULT_EDGE_LENGTH_M)
    ap.add_argument("--bed", default=BED)
    ap.add_argument("--out", default=os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
    ns = ap.parse_args(argv)

    from trid3nt_server.workflows.mesh.meshers import MeshToolError, get_mesher
    from trid3nt_server.workflows.mesh.recipe import build_recipe, mesh_op

    out_dir = Path(ns.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bbox = tuple(ns.bbox)

    # The refusal the bbox domain owes, recorded beside the build that answers it.
    refusal: dict[str, Any] = {}
    try:
        get_mesher("om2d").build(build_recipe(
            mesher="om2d", kind="unstructured_tri", extent=bbox,
            resolution_m=ns.edge_length_m, ops=[]))
        refusal = {"refused": False,
                   "note": "the shoreline described this extent after all"}
    except MeshToolError as exc:
        refusal = {"refused": True, "error_code": exc.error_code,
                   "message": str(exc), "escalation": exc.escalation}
    print(json.dumps({"bbox_domain": refusal}, indent=2, default=str))

    domain = lake_polygon(bbox, out_dir)
    mesh = get_mesher("om2d").build(build_recipe(
        mesher="om2d", kind="unstructured_tri", extent=domain,
        resolution_m=ns.edge_length_m,
        ops=[mesh_op("set_rim_size"),
             mesh_op("delete_boundary_faces"),
             mesh_op("delete_faces_connected_to_one_face"),
             mesh_op("laplacian2"),
             mesh_op("make_mesh_boundaries_traversable"),
             mesh_op("fix_mesh", delete_unused=True),
             mesh_op("set_bed", source=ns.bed)]))
    figure = render(
        mesh, domain, out_dir,
        f"lake domain: {mesh.node_count} nodes / {mesh.element_count} elements, "
        f"{mesh.crs_authid}\ncyan = the fetched water body the domain was cut "
        f"from; ask {ns.edge_length_m:.0f} m")
    print(json.dumps({
        "nodes": mesh.node_count, "elements": mesh.element_count,
        "crs": mesh.crs_authid, "has_bed": mesh.has_bed,
        "bed_source": mesh.meta.get("bed_source"),
        "domain_source": mesh.meta.get("domain_source"),
        "probes": mesh.meta.get("probes"),
        "figure": figure}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
