"""coastal_tin -- the server-side oceanmesh coastal-TIN component (mesh wave,
oceanmesh leg, ADR 0101).

Thin M1/M2-paradigm component: it composes the OceanMesh2D sizing-function SPEC +
inputs (shoreline + optional DEM + edge bounds + which sizing functions), dispatches
the GPL-isolated mesh worker (``trid3nt-local/mesh``), and consumes the worker's
mesh GeoJSON back through the shared ``mesh_preview`` styling contract
(``style_preset="mesh_grid"``). oceanmesh (GPL-3) + gmsh + CGAL live ONLY in the
worker image, never this venv -- so this module lazy-imports nothing heavy and is
offline-suite-safe (mirrors ``hecras_geometry``: h5py/pyproj lazy).

NO solver consumes the coastal TIN yet (future SCHISM / TELEMAC-coastal). This
component lands the GENERATOR + drives the paper-first validation case. The
sizing functions (distance / feature-size / wavelength / slope) are the
OceanMesh2D tried-and-true graded surface -- the census's no-graded-sizing gap
closer.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.agent.mesh.coastal_tin")

__all__ = [
    "CoastalTinSpec",
    "compose_coastal_tin_manifest",
    "run_coastal_tin_worker",
    "CoastalTinError",
]

DEFAULT_MESH_IMAGE = "trid3nt-local/mesh:latest"


class CoastalTinError(RuntimeError):
    """The coastal-TIN worker failed to produce a mesh."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class CoastalTinSpec:
    """The OceanMesh2D sizing-function SPEC + inputs for one coastal TIN.

    ``bbox`` is EPSG:4326 ``(min_lon, min_lat, max_lon, max_lat)``. ``shoreline_shp``
    is a vector polygon shapefile PATH VISIBLE INSIDE THE WORKER container (bind-
    mounted under the rundir, or a path baked in the image). ``dem_path`` (optional,
    also worker-visible) enables the wavelength + slope (bathymetric-gradient)
    sizing functions. Edge bounds are metres; ``grade`` is the mesh-gradation
    limit (User Guide Eq. 11, typ. 0.15-0.30)."""

    bbox: tuple[float, float, float, float]
    shoreline_shp: str
    min_edge_length_m: float = 30.0
    max_edge_length_m: float = 1000.0
    dem_path: str | None = None
    grade: float = 0.15
    feature_size: bool = True
    wavelength: bool = False
    slope: bool = False

    def to_manifest(self, run_id: str | None = None) -> dict[str, Any]:
        return {
            "run_id": run_id or uuid.uuid4().hex,
            "shoreline_shp": self.shoreline_shp,
            "bbox": [float(v) for v in self.bbox],
            "min_edge_length_m": float(self.min_edge_length_m),
            "max_edge_length_m": float(self.max_edge_length_m),
            "dem_path": self.dem_path,
            "grade": float(self.grade),
            "feature_size": bool(self.feature_size),
            "wavelength": bool(self.wavelength),
            "slope": bool(self.slope),
        }


def compose_coastal_tin_manifest(
    spec: CoastalTinSpec, run_id: str | None = None
) -> dict[str, Any]:
    """Compose the mesh-worker manifest from the sizing SPEC (M1/M2 paradigm)."""
    return spec.to_manifest(run_id)


def run_coastal_tin_worker(
    spec: CoastalTinSpec,
    rundir: str | Path,
    *,
    image: str = DEFAULT_MESH_IMAGE,
    mounts: dict[str, str] | None = None,
    timeout_s: float = 900.0,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Dispatch the mesh worker (local docker, volume-mount envelope) and read back.

    Stages ``manifest.json`` into ``rundir``, bind-mounts it at ``/data`` (plus any
    extra read-only ``mounts`` {host: container} carrying the shoreline/DEM), runs
    the worker, and returns ``(mesh_stats, coastal_tin_geojson_path)``. Raises
    :class:`CoastalTinError` on a non-zero exit or an error-status stats file.

    NO S3 here -- the caller publishes the returned GeoJSON through
    ``mesh_preview`` (``style_preset="mesh_grid"``) like every other paradigm."""
    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    manifest = compose_coastal_tin_manifest(spec, run_id)
    (rundir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    argv = ["docker", "run", "--rm", "-v", f"{rundir}:/data", "-w", "/data"]
    for host, container in (mounts or {}).items():
        argv += ["-v", f"{host}:{container}:ro"]
    argv += [image]
    logger.info("coastal_tin dispatch: %s", " ".join(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise CoastalTinError(
            "COASTAL_TIN_TIMEOUT",
            f"mesh worker exceeded {timeout_s:.0f}s for bbox={spec.bbox}",
        ) from exc

    stats_path = rundir / "mesh_stats.json"
    stats: dict[str, Any] = {}
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            stats = {}
    if proc.returncode != 0 or str(stats.get("status")) != "ok":
        raise CoastalTinError(
            str(stats.get("error_code") or "COASTAL_TIN_BUILD_FAILED"),
            stats.get("error")
            or f"mesh worker exit={proc.returncode}; stderr tail: "
            + "\n".join(proc.stderr.splitlines()[-8:]),
        )
    return stats, rundir / "coastal_tin.geojson"
