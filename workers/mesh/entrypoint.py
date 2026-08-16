"""Mesh worker entrypoint -- oceanmesh coastal_tin generator (leg 2).

Volume-mount envelope (TELEMAC/SFINCS-canonical): the caller bind-mounts a
rundir at /data carrying manifest.json; this reads it, runs the oceanmesh
coastal-TIN pipeline, and writes coastal_tin.geojson (EPSG:4326 triangle
wireframe) + mesh_stats.json (vertex/element counts, quality, edge bounds) back
into /data. Exit 0 on success, non-zero on failure (with a typed mesh_stats.json
error). NO object-store I/O here (a supervisor uploads /data) -- keeps the GPL
oceanmesh image lean.

Manifest schema::

    {
      "shoreline_shp": "/opt/gshhg/.../GSHHS_f_L1.shp",  # required vector polygons
      "bbox": [min_lon, min_lat, max_lon, max_lat],       # required, EPSG:4326
      "min_edge_length_m": 30.0,
      "max_edge_length_m": 1000.0,
      "dem_path": "/data/dem.tif",     # optional; enables wavelength/slope sizing
      "grade": 0.15,
      "feature_size": true,
      "wavelength": false,
      "slope": false,
      "run_id": "<ulid>"               # optional, echoed
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("trid3nt-mesh")

DEFAULT_DATA_DIR = Path("/data")


def _write(data_dir: Path, name: str, payload: dict[str, Any]) -> None:
    (data_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_pipeline(data_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    import coastal_tin_build as C

    shoreline_shp = manifest.get("shoreline_shp")
    bbox = manifest.get("bbox")
    if not shoreline_shp or not Path(str(shoreline_shp)).exists():
        raise FileNotFoundError(f"shoreline_shp missing or not found: {shoreline_shp!r}")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        raise ValueError(f"bbox must be [min_lon,min_lat,max_lon,max_lat]: {bbox!r}")

    t0 = time.time()
    points, cells, stats = C.build_coastal_tin(
        shoreline_shp=str(shoreline_shp),
        bbox=tuple(float(v) for v in bbox),
        min_edge_length_m=float(manifest.get("min_edge_length_m", 30.0)),
        max_edge_length_m=float(manifest.get("max_edge_length_m", 1000.0)),
        dem_path=(str(manifest["dem_path"]) if manifest.get("dem_path") else None),
        grade=float(manifest.get("grade", 0.15)),
        feature_size=bool(manifest.get("feature_size", True)),
        wavelength=bool(manifest.get("wavelength", False)),
        slope=bool(manifest.get("slope", False)),
    )
    geojson = C.mesh_to_geojson(points, cells)
    (data_dir / "coastal_tin.geojson").write_text(
        json.dumps(geojson), encoding="utf-8"
    )
    # RAW nodes + triangles (ADR 0118): the geojson is an edge-wireframe PREVIEW
    # only; a downstream solver-mesh bridge (SCHISM tin_to_hgrid) needs the raw
    # (N,2) lon/lat node table + the (M,3) triangle connectivity. Additive, numpy
    # already present, tiny -- written as a compressed .npz alongside the preview.
    import numpy as _np

    _np.savez(
        data_dir / "coastal_tin_mesh.npz",
        points=_np.asarray(points, dtype=float),
        cells=_np.asarray(cells, dtype=_np.int64),
    )
    stats.update({
        "status": "ok",
        "run_id": manifest.get("run_id"),
        "preview_geojson": "coastal_tin.geojson",
        "mesh_npz": "coastal_tin_mesh.npz",
        "wall_s": round(time.time() - t0, 1),
    })
    LOG.info(
        "coastal_tin done: %d verts / %d elems, minQ=%.3f 3sigma_lcl=%.3f wall=%.1fs",
        stats["n_vertices"], stats["n_elements"], stats["min_quality"],
        stats["quality_3sigma_lcl"], stats["wall_s"],
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="oceanmesh coastal_tin worker")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--manifest", default=None)
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else (data_dir / "manifest.json")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
    except Exception as exc:  # noqa: BLE001
        LOG.exception("manifest read failed")
        _write(data_dir, "mesh_stats.json", {
            "status": "error", "error_code": "MESH_MANIFEST_INVALID",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 2

    try:
        stats = run_pipeline(data_dir, manifest)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("coastal_tin pipeline failed")
        _write(data_dir, "mesh_stats.json", {
            "status": "error", "error_code": "COASTAL_TIN_BUILD_FAILED",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 1

    _write(data_dir, "mesh_stats.json", stats)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
