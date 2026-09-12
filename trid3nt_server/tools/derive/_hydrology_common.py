"""Shared core of the pysheds hydrology primitives.

The typed error hierarchy, the pysheds import seam, DEM staging and
conditioning, bbox validation and the shared GeoJSON writer.
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
    "HydrologyDemTooLargeError",
    "HydrologyDependencyError",
    "HydrologyUpstreamError",
    "write_conditioned_dem",
]

logger = logging.getLogger("trid3nt_server.tools.derive._hydrology_common")




class HydrologyPrimitivesError(RuntimeError):
    """Base class for watershed-primitive failures."""

    error_code: str = "HYDROLOGY_PRIMITIVES_ERROR"
    retryable: bool = True

class HydrologyInputError(HydrologyPrimitivesError):
    """Bad inputs (malformed bbox/pour point, bad threshold, unreadable URI)."""

    error_code = "HYDROLOGY_INPUT_INVALID"
    retryable = False

class HydrologyDemTooLargeError(HydrologyInputError):
    """The DEM carries more cells than the CPU-bound D8 clamp admits."""

    error_code = "HYDROLOGY_DEM_TOO_LARGE"
    retryable = False

class HydrologyDependencyError(HydrologyPrimitivesError):
    """pysheds (or rasterio/shapely) is unavailable: pysheds ships with the base
    ``pfdf`` dependency, so this is a broken environment, not a missing extra.
    """

    error_code = "HYDROLOGY_DEPENDENCY_MISSING"
    retryable = False

class HydrologyUpstreamError(HydrologyPrimitivesError):
    """DEM staging, upstream fetch, or artifact write failed."""

    error_code = "HYDROLOGY_UPSTREAM_ERROR"
    retryable = True


#: CPU-bound clamp on the D8 chain, in the DEM's own CELLS - which is what the
#: chain costs. Degrees are not a cost: a projected DEM's degree extent is its
#: map bulge, and one grid of cells conditions in the same time wherever it sits
#: and whatever it spans. A 4000 x 4000 grid conditions in about forty seconds
#: here, measured on the pysheds chain below.
_MAX_DEM_CELLS: int = 16_000_000

_ENGINE_NOTE = (
    "Engine: pysheds D8 (fill_pits -> fill_depressions -> resolve_flats -> "
    "flowdir -> accumulation; catchment for basins). pysheds is a "
    "base-environment dependency (ships with pfdf). Pour-point snapping and "
    "channel vectorization are pure-numpy in-module (pysheds 0.4's "
    "snap_to_mask / extract_river_network are incompatible with NEP-50 numpy)."
)

#: pysheds' default D8 direction map, in [N, NE, E, SE, S, SW, W, NW] order.
_D8_DIRMAP: tuple[int, ...] = (64, 128, 1, 2, 4, 8, 16, 32)

#: Half-window (cells) for max-accumulation outlet snapping in the shared,
#: alignment-invariant delineation. An 8-cell window (~240 m at 30 m) is enough
#: to seat the outlet on the main channel without swallowing a neighbouring
#: basin.
_OUTLET_SNAP_SEARCH_CELLS: int = 8



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
    """Validate + normalize a lon/lat bbox."""
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

def _stage_dem(dem_uri: Any, tmpdir: str, notes: list[str]) -> str:
    """The local path of the DEM the caller handed in; a missing one is a typed
    refusal naming the fetch that supplies it, never a fetch of its own."""
    if not isinstance(dem_uri, str) or not dem_uri.strip():
        raise HydrologyInputError(
            "dem_uri is required: fetch a DEM over the area first "
            "(fetch_copernicus_dem for a geographic 30 m grid, fetch_dem for 3DEP) "
            "and pass its uri."
        )
    local = _stage_uri_local(dem_uri, tmpdir, "dem")
    notes.append(f"DEM from dem_uri ({dem_uri}).")
    return local


def _dem_bbox_4326(dem_path: str) -> tuple[float, float, float, float]:
    """The DEM's own extent in EPSG:4326, whatever projection it was written in."""
    import rasterio
    from rasterio.warp import transform_bounds

    try:
        with rasterio.open(dem_path) as src:
            crs, bounds = src.crs, src.bounds
    except Exception as exc:  # noqa: BLE001
        raise HydrologyInputError(
            f"could not open DEM raster {dem_path!r}: {exc}"
        ) from exc
    if crs is None:
        raise HydrologyInputError(f"DEM raster {dem_path!r} carries no CRS.")
    if crs.to_epsg() == 4326:
        bbox = (bounds.left, bounds.bottom, bounds.right, bounds.top)
    else:
        bbox = transform_bounds(crs, "EPSG:4326", *bounds)
    return _validate_bbox(tuple(float(v) for v in bbox))


def _open_dem(dem_path: str) -> tuple[Any, Any]:
    """``(grid, dem)`` for a DEM raster, held to the D8 cell clamp.

    Every path into the conditioning chain opens its DEM here, so the clamp is
    stated once and no caller can reach the chain around it.
    """
    Grid = _import_pysheds()
    try:
        grid = Grid.from_raster(dem_path)
        dem = grid.read_raster(dem_path)
    except Exception as exc:  # noqa: BLE001
        raise HydrologyInputError(
            f"could not open DEM raster {dem_path!r}: {exc}"
        ) from exc
    rows, cols = int(grid.shape[0]), int(grid.shape[1])
    if rows * cols > _MAX_DEM_CELLS:
        raise HydrologyDemTooLargeError(
            f"DEM {dem_path!r} carries {rows * cols:,} cells "
            f"({rows:,} x {cols:,}), past the D8 clamp of "
            f"{_MAX_DEM_CELLS:,}. Fetch the DEM over a smaller area, or at a "
            "coarser resolution_m, and pass that layer instead."
        )
    return grid, dem


def _conditioned(grid: Any, dem: Any) -> Any:
    """The pit -> depression -> flat chain: ONE conditioning, so no two consumers
    of a conditioned surface can describe different grounds.
    """
    return grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))


def _condition_dem(dem_path: str) -> tuple[Any, Any, Any]:
    """pysheds conditioning chain -> ``(grid, fdir, acc)``."""
    grid, dem = _open_dem(dem_path)
    try:
        inflated = _conditioned(grid, dem)
        # nodata_out MUST be numpy-typed scalars: pysheds 0.4 hands them to
        # np.can_cast, which rejects Python ints/floats under NEP-50 numpy.
        fdir = grid.flowdir(inflated, nodata_out=np.int64(0))
        acc = grid.accumulation(fdir, nodata_out=np.float64(0))
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"pysheds DEM conditioning / flow analysis failed: {exc}"
        ) from exc
    return grid, fdir, acc


def write_conditioned_dem(dem_path: str, out_path: str) -> str:
    """The delineator's own conditioning chain, written back out as a raster.
    The profile is the source raster's, so nothing but the elevations moves.
    """
    import rasterio

    grid, dem = _open_dem(dem_path)
    # A mesh bed painted from the RAW DEM ponds in exactly the pits the
    # delineation already fills to route through: the deepest water in the run is
    # then a single node inside an unfilled sink, and the published depth map is
    # scaled by a terrain artifact rather than by the storm.
    try:
        inflated = np.asarray(_conditioned(grid, dem), dtype="float32")
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"pysheds DEM conditioning failed for {dem_path!r}: {exc}"
        ) from exc
    with rasterio.open(dem_path) as src:
        profile = src.profile
    profile.update(dtype="float32", count=1)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(inflated, 1)
    return out_path

def snap_and_delineate_index_space(
    grid: Any,
    fdir: Any,
    acc: Any,
    lon: float,
    lat: float,
    *,
    snap_search_cells: int = _OUTLET_SNAP_SEARCH_CELLS,
) -> tuple[Any, Any, tuple[float, float], int]:
    """Alignment-invariant catchment upstream of ``(lon, lat)``: ``(mask, polygon,
    (x_snap, y_snap), cells)``, ``polygon`` in the grid CRS or None when empty.
    """
    acc_arr = np.asarray(acc)
    affine = grid.affine
    col_f, row_f = ~affine * (float(lon), float(lat))
    r0, c0 = int(row_f), int(col_f)
    radius = int(snap_search_cells)
    rmin, rmax = max(0, r0 - radius), min(acc_arr.shape[0], r0 + radius + 1)
    cmin, cmax = max(0, c0 - radius), min(acc_arr.shape[1], c0 + radius + 1)
    if rmin >= rmax or cmin >= cmax:
        raise HydrologyInputError(
            f"pour point ({lon}, {lat}) falls outside the DEM window; supply a "
            "bbox/pour point inside the analysis AOI."
        )
    # Snapping the outlet to the max-accumulation cell of the window is what
    # guarantees it sits on the main channel rather than on a hillslope cell.
    win = acc_arr[rmin:rmax, cmin:cmax]
    di, dj = np.unravel_index(int(np.argmax(win)), win.shape)
    rr, cc = rmin + int(di), cmin + int(dj)
    x_snap, y_snap = affine * (cc + 0.5, rr + 0.5)

    # Tracing in INDEX space is alignment-invariant; xytype="coordinate" is not.
    # Its coordinate->cell round-trip can land on a neighbour cell and collapse
    # the basin to a 1-cell sliver on certain grid alignments: 33.7-34.0k cells
    # in index space across box quantizations against 1-14 for the coordinate
    # path.
    try:
        catch = grid.catchment(
            x=cc,
            y=rr,
            fdir=fdir,
            xytype="index",
            # numpy-typed for the same NEP-50 reason as _condition_dem.
            nodata_out=np.bool_(False),
        )
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"pysheds catchment delineation failed: {exc}"
        ) from exc
    mask = np.asarray(catch, dtype=bool)
    cell_count = int(mask.sum())
    if cell_count == 0:
        return mask, None, (float(x_snap), float(y_snap)), 0

    try:
        from rasterio import features as rio_features
        from shapely.geometry import shape
        from shapely.ops import unary_union
    except ImportError as exc:
        raise HydrologyDependencyError(
            f"rasterio/shapely unavailable: {exc}"
        ) from exc
    try:
        geoms = [
            shape(geom)
            for geom, val in rio_features.shapes(
                mask.astype(np.uint8), mask=mask, transform=affine
            )
            if val == 1
        ]
        polygon = unary_union(geoms) if geoms else None
    except Exception as exc:  # noqa: BLE001
        raise HydrologyUpstreamError(
            f"polygonizing the catchment mask failed: {exc}"
        ) from exc
    return mask, polygon, (float(x_snap), float(y_snap)), cell_count


def _write_geojson(
    fc: dict[str, Any], prefix: str, seed: str, output_dir: str | None
) -> str:
    """Persist a FeatureCollection; return its URI (a local path when
    ``output_dir`` is given, else an ``s3://`` key in the runs bucket).
    """
    payload = json.dumps(fc).encode("utf-8")
    filename = f"{prefix}_{seed}.geojson"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server import storage

        bucket = storage.runs_bucket()
        key = f"{prefix}-{seed}/{filename}"
        storage.client().put_object(
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
