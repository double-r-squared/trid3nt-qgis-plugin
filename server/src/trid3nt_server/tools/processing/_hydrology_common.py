"""Shared core of the pysheds hydrology primitives (split from the original
two-tool ``hydrology_primitives`` module): the typed error hierarchy, the
pysheds import seam, DEM staging/conditioning, bbox validation and the
GeoJSON writer shared by ``delineate_watershed`` + ``extract_stream_network``.

This module registers nothing; the two tool modules are siblings.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "HydrologyPrimitivesError",
    "HydrologyInputError",
    "HydrologyAoiTooLargeError",
    "HydrologyDependencyError",
    "HydrologyUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.processing._hydrology_common")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HydrologyPrimitivesError(RuntimeError):
    """Base class for watershed-primitive failures."""

    error_code: str = "HYDROLOGY_PRIMITIVES_ERROR"
    retryable: bool = True

class HydrologyInputError(HydrologyPrimitivesError):
    """Bad inputs (malformed bbox/pour point, bad threshold, unreadable URI)."""

    error_code = "HYDROLOGY_INPUT_INVALID"
    retryable = False

class HydrologyAoiTooLargeError(HydrologyInputError):
    """The AOI exceeds the CPU-bound clamp (> 0.3 degrees per side)."""

    error_code = "HYDROLOGY_AOI_TOO_LARGE"
    retryable = False

class HydrologyDependencyError(HydrologyPrimitivesError):
    """pysheds (or rasterio/shapely) is unavailable -- honest, typed.

    pysheds ships transitively with the base ``pfdf`` dependency; this error
    means the environment is broken, not that an optional extra is missing.
    """

    error_code = "HYDROLOGY_DEPENDENCY_MISSING"
    retryable = False

class HydrologyUpstreamError(HydrologyPrimitivesError):
    """DEM staging, upstream fetch, or artifact write failed."""

    error_code = "HYDROLOGY_UPSTREAM_ERROR"
    retryable = True

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CPU-bound AOI clamp (degrees per side): ~1100x1100 cells at 30 m.
_MAX_AOI_DEG: float = 0.3

_ENGINE_NOTE = (
    "Engine: pysheds D8 (fill_pits -> fill_depressions -> resolve_flats -> "
    "flowdir -> accumulation; catchment for basins). pysheds is a "
    "base-environment dependency (ships with pfdf). Pour-point snapping and "
    "channel vectorization are pure-numpy in-module (pysheds 0.4's "
    "snap_to_mask / extract_river_network are incompatible with NEP-50 numpy)."
)

#: pysheds' default D8 direction map, in [N, NE, E, SE, S, SW, W, NW] order.
_D8_DIRMAP: tuple[int, ...] = (64, 128, 1, 2, 4, 8, 16, 32)

# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _import_pysheds() -> Any:
    """Import ``pysheds.grid.Grid`` behind the typed honest error."""
    try:
        from pysheds.grid import Grid
    except Exception as exc:  # noqa: BLE001 -- honest typed dependency error
        raise HydrologyDependencyError(
            "pysheds is not importable in this environment "
            f"({type(exc).__name__}: {exc}). pysheds ships with the base pfdf "
            "dependency -- reinstall the agent environment (pip install -e "
            "services/agent) rather than adding a new dependency."
        ) from exc
    return Grid

def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Validate + normalize the bbox; enforce the CPU-bound AOI clamp."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise HydrologyInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise HydrologyInputError(
            f"bbox contains non-numeric values: {bbox!r}"
        ) from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise HydrologyInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HydrologyInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HydrologyInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise HydrologyInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if (east - west) > _MAX_AOI_DEG or (north - south) > _MAX_AOI_DEG:
        raise HydrologyAoiTooLargeError(
            f"AOI {bbox!r} exceeds the watershed-primitive clamp of "
            f"{_MAX_AOI_DEG} degrees per side "
            f"(got {east - west:.3f} x {north - south:.3f} deg). D8 analysis "
            "is CPU-bounded; pick a single-watershed AOI."
        )
    return (west, south, east, north)

def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise HydrologyUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise HydrologyInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise HydrologyInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri

def _stage_dem(
    bbox: tuple[float, float, float, float],
    dem_uri: str | None,
    tmpdir: str,
    notes: list[str],
) -> str:
    """Local DEM path (override or fetch_copernicus_dem)."""
    if dem_uri is not None:
        local = _stage_uri_local(dem_uri, tmpdir, "dem")
        notes.append(f"DEM from caller-supplied dem_uri ({dem_uri}).")
        return local
    try:
        from trid3nt_server.tools.fetchers.terrain.fetch_copernicus_dem import fetch_copernicus_dem

        layer = fetch_copernicus_dem(bbox=bbox)
    except HydrologyPrimitivesError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"fetch_copernicus_dem failed for bbox={bbox}: {exc}"
        ) from exc
    local = _stage_uri_local(layer.uri, tmpdir, "dem")
    notes.append("DEM: Copernicus GLO-30 (30 m) via fetch_copernicus_dem.")
    return local

def _condition_dem(dem_path: str) -> tuple[Any, Any, Any]:
    """pysheds conditioning chain -> ``(grid, fdir, acc)``."""
    Grid = _import_pysheds()
    try:
        grid = Grid.from_raster(dem_path)
        dem = grid.read_raster(dem_path)
    except Exception as exc:  # noqa: BLE001
        raise HydrologyInputError(
            f"could not open DEM raster {dem_path!r}: {exc}"
        ) from exc
    try:
        pit_filled = grid.fill_pits(dem)
        flooded = grid.fill_depressions(pit_filled)
        inflated = grid.resolve_flats(flooded)
        # nodata_out MUST be numpy-typed scalars: pysheds 0.4 hands them to
        # np.can_cast, which rejects Python ints/floats under NEP-50 numpy.
        fdir = grid.flowdir(inflated, nodata_out=np.int64(0))
        acc = grid.accumulation(fdir, nodata_out=np.float64(0))
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"pysheds DEM conditioning / flow analysis failed: {exc}"
        ) from exc
    return grid, fdir, acc

def _write_geojson(
    fc: dict[str, Any], prefix: str, seed: str, output_dir: str | None
) -> str:
    """Persist a FeatureCollection; return its URI (local for tests, runs
    bucket live -- same convention as model_debris_flow)."""
    payload = json.dumps(fc).encode("utf-8")
    filename = f"{prefix}_{seed}.geojson"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server.tools.simulation.solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"{prefix}-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/geo+json",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"failed to upload {prefix} GeoJSON to the runs bucket: {exc}"
        ) from exc
