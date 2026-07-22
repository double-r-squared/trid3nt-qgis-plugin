"""Watershed primitives -- ``delineate_watershed`` + ``extract_stream_network``.

Two registered hydrology primitives over a DEM (fetched or override):

    delineate_watershed(pour_point, bbox=None, dem_uri=None)
        -> watershed POLYGON vector LayerURI upstream of a pour point
    extract_stream_network(bbox, accumulation_threshold=500, dem_uri=None)
        -> stream LINESTRING vector LayerURI (D8 accumulation >= threshold)

ENGINE PATH TAKEN (documented per the kickoff): **pysheds** (0.4, the
``pysheds.grid.Grid`` sgrid API) for the heavy lifting -- DEM conditioning
(``fill_pits`` -> ``fill_depressions`` -> ``resolve_flats``), D8
``flowdir``, ``accumulation``, and ``catchment``. pysheds is ALREADY
importable in ``services/agent/.venv`` -- it ships transitively with the
``pfdf`` BASE dependency (pfdf's watershed module is pysheds-backed;
``model_debris_flow`` already exercises it on the box) -- so NO new pip
dependency was added (the ``local-engines`` extra is untouched). The import
is still guarded with the typed honest ``HydrologyDependencyError`` so a
broken environment degrades to a clear error, never a silent fabrication.

TWO SMALL PIECES ARE PURE NUMPY IN-MODULE (the kickoff's numpy-D8 fallback,
applied surgically): pysheds 0.4's ``snap_to_mask`` and
``extract_river_network`` crash under NEP-50 numpy (they build Rasters whose
``nodata`` is a PYTHON scalar; ``np.can_cast`` now rejects Python scalars --
verified empirically in this venv). So the pour-point snap
(``_snap_to_stream``: nearest cell with accumulation >= ``snap_threshold``)
and the channel vectorization (``_trace_stream_network``: walk the D8
directions downstream from every headwater/junction, splitting LineStrings
at junctions -- the same segmentation pysheds produces) are implemented here
directly on the numpy arrays. ``nodata_out`` is passed as numpy-typed
scalars to the pysheds calls that DO work for the same NEP-50 reason.

The pour point is SNAPPED to the nearest cell whose accumulation reaches
``snap_threshold`` cells (the usual snap-to-stream practice -- a
hand-clicked outlet rarely lands exactly on the flow line); the snap is
recorded in ``notes`` and the snapped coordinates ride on the result.

DEM substrate: ``dem_uri`` override (s3:// or local GeoTIFF; tests pass a
synthetic valley), else ``fetch_copernicus_dem`` (GLO-30, EPSG:4326). For
``delineate_watershed`` the fetch bbox defaults to a 0.1-degree box centered
on the pour point when no bbox is given. Both tools clamp the AOI to
<= 0.3 degrees per side (CPU-bound D8 on a ~1100x1100 grid at 30 m).

Outputs are GeoJSON written to the runs bucket (or ``_output_dir`` for
offline tests) and returned as typed ``LayerURI`` subclasses (the
``FaultSourcesResult`` house pattern) so the ``emit_tool_call`` wrap-site
persists the layer to the case record while summary scalars + honest
``notes`` ride along for the LLM.

``cacheable=False`` (``ttl_class="live-no-cache"``): modeling composers, not
fetchers -- artifacts go to the runs bucket, not the cache.
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

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "delineate_watershed",
    "extract_stream_network",
    "WatershedLayerURI",
    "StreamNetworkLayerURI",
    "HydrologyPrimitivesError",
    "HydrologyInputError",
    "HydrologyAoiTooLargeError",
    "HydrologyDependencyError",
    "HydrologyUpstreamError",
    "NoStreamsError",
    "EmptyWatershedError",
]

logger = logging.getLogger("grace2_agent.tools.hydrology_primitives")


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


class NoStreamsError(HydrologyPrimitivesError):
    """No cell reaches the accumulation threshold -- no stream to extract."""

    error_code = "HYDROLOGY_NO_STREAMS"
    retryable = False


class EmptyWatershedError(HydrologyPrimitivesError):
    """The delineated catchment is empty (pour point off the flow grid)."""

    error_code = "HYDROLOGY_EMPTY_WATERSHED"
    retryable = False


# ---------------------------------------------------------------------------
# Result types -- LayerURI subclasses carrying summaries (house side-channel).
# ---------------------------------------------------------------------------


class WatershedLayerURI(LayerURI):
    """Watershed polygon ``LayerURI`` plus delineation summary.

    Extra fields beyond ``LayerURI``: ``area_km2`` (catchment area),
    ``cell_count`` (catchment cells), ``pour_point`` (as requested, lon/lat),
    ``snapped_pour_point`` (the accumulation-snapped outlet actually used),
    ``notes`` (engine path + every adjustment made).
    """

    area_km2: float = 0.0
    cell_count: int = 0
    pour_point: tuple[float, float] | None = None
    snapped_pour_point: tuple[float, float] | None = None
    notes: list[str] = []


class StreamNetworkLayerURI(LayerURI):
    """Stream-network line ``LayerURI`` plus extraction summary.

    Extra fields beyond ``LayerURI``: ``segment_count`` (LineString branches),
    ``accumulation_threshold`` (cells), ``total_length_km`` (approximate sum
    of branch lengths), ``notes`` (engine path + provenance).
    """

    segment_count: int = 0
    accumulation_threshold: int = 500
    total_length_km: float = 0.0
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CPU-bound AOI clamp (degrees per side): ~1100x1100 cells at 30 m.
_MAX_AOI_DEG: float = 0.3

#: Auto-bbox half-side (degrees) around the pour point when no bbox is given.
_AUTO_BBOX_DEG: float = 0.1

#: Default snap threshold (upslope cells) for pour-point snapping.
_SNAP_THRESHOLD_CELLS: int = 100

_ENGINE_NOTE = (
    "Engine: pysheds D8 (fill_pits -> fill_depressions -> resolve_flats -> "
    "flowdir -> accumulation; catchment for basins). pysheds is a "
    "base-environment dependency (ships with pfdf). Pour-point snapping and "
    "channel vectorization are pure-numpy in-module (pysheds 0.4's "
    "snap_to_mask / extract_river_network are incompatible with NEP-50 numpy)."
)

#: pysheds' default D8 direction map, in [N, NE, E, SE, S, SW, W, NW] order.
_D8_DIRMAP: tuple[int, ...] = (64, 128, 1, 2, 4, 8, 16, 32)

#: direction value -> (row_offset, col_offset). Row 0 is the NORTH edge.
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    64: (-1, 0),  # N
    128: (-1, 1),  # NE
    1: (0, 1),  # E
    2: (1, 1),  # SE
    4: (1, 0),  # S
    8: (1, -1),  # SW
    16: (0, -1),  # W
    32: (-1, -1),  # NW
}

_WATERSHED_METADATA = AtomicToolMetadata(
    name="delineate_watershed",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)

_STREAMS_METADATA = AtomicToolMetadata(
    name="extract_stream_network",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


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


def _validate_pour_point(pour_point: Any) -> tuple[float, float]:
    if not isinstance(pour_point, (tuple, list)) or len(pour_point) != 2:
        raise HydrologyInputError(
            f"pour_point must be (lon, lat); got {pour_point!r}"
        )
    try:
        lon, lat = float(pour_point[0]), float(pour_point[1])
    except (TypeError, ValueError) as exc:
        raise HydrologyInputError(
            f"pour_point contains non-numeric values: {pour_point!r}"
        ) from exc
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise HydrologyInputError(f"pour_point is non-finite: {pour_point!r}")
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise HydrologyInputError(
            f"pour_point out of lon/lat range: {pour_point!r}"
        )
    return (lon, lat)


def _auto_bbox(
    lon: float, lat: float, half_side_deg: float = _AUTO_BBOX_DEG / 2.0
) -> tuple[float, float, float, float]:
    """The 0.1-degree default box centered on the pour point."""
    return (
        max(lon - half_side_deg, -180.0),
        max(lat - half_side_deg, -90.0),
        min(lon + half_side_deg, 180.0),
        min(lat + half_side_deg, 90.0),
    )


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

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
        from .fetch_copernicus_dem import fetch_copernicus_dem

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


def _snap_to_stream(
    acc: np.ndarray,
    affine: Any,
    lon: float,
    lat: float,
    threshold: int,
) -> tuple[float, float] | None:
    """Nearest cell CENTER (lon, lat) with accumulation >= ``threshold``.

    Pure numpy (pysheds 0.4's ``snap_to_mask`` is NEP-50-broken): distance is
    measured in cell-index space, which is adequate for a snap over a small
    AOI. Returns ``None`` when no cell reaches the threshold.
    """
    mask = acc >= threshold
    if not bool(mask.any()):
        return None
    col_f, row_f = ~affine * (lon, lat)
    rows, cols = np.nonzero(mask)
    d2 = (rows + 0.5 - row_f) ** 2 + (cols + 0.5 - col_f) ** 2
    i = int(np.argmin(d2))
    x, y = affine * (cols[i] + 0.5, rows[i] + 0.5)
    return (float(x), float(y))


def _downstream_cell(
    fdir: np.ndarray, r: int, c: int
) -> tuple[int, int] | None:
    """The D8 downstream neighbor of ``(r, c)``, or None (pit/edge/nodata)."""
    off = _D8_OFFSETS.get(int(fdir[r, c]))
    if off is None:
        return None
    rr, cc = r + off[0], c + off[1]
    if not (0 <= rr < fdir.shape[0] and 0 <= cc < fdir.shape[1]):
        return None
    return (rr, cc)


def _trace_stream_network(
    fdir: np.ndarray, mask: np.ndarray, affine: Any
) -> list[list[tuple[float, float]]]:
    """Vectorize the channel cells into LineStrings (pure numpy D8 walk).

    Mirrors pysheds' ``extract_river_network`` segmentation (which is
    NEP-50-broken in 0.4): a segment starts at every HEADWATER (no channel
    inflow) and every JUNCTION (>= 2 channel inflows) and follows the D8
    directions downstream until the next junction / channel exit, so branches
    join exactly at confluences. Coordinates are cell centers.
    """
    in_degree = np.zeros(mask.shape, dtype=np.int32)
    rows, cols = np.nonzero(mask)
    for r, c in zip(rows.tolist(), cols.tolist()):
        ds = _downstream_cell(fdir, r, c)
        if ds is not None and mask[ds]:
            in_degree[ds] += 1
    lines: list[list[tuple[float, float]]] = []
    for r, c in zip(rows.tolist(), cols.tolist()):
        if in_degree[r, c] == 1:
            continue  # mid-segment cell -- covered by an upstream walk
        path = [(r, c)]
        cur = (r, c)
        while True:
            ds = _downstream_cell(fdir, *cur)
            if ds is None or not mask[ds]:
                break
            path.append(ds)
            if in_degree[ds] >= 2:
                break  # junction: the next segment starts there
            cur = ds
        if len(path) >= 2:
            lines.append(
                [
                    (float(x), float(y))
                    for x, y in (affine * (cc + 0.5, rr + 0.5) for rr, cc in path)
                ]
            )
    return lines


def _cell_area_km2(grid: Any) -> float:
    """Approximate cell area (km^2), converting degrees at the center latitude
    for a geographic grid (adequate over a <=0.3-degree AOI)."""
    affine = grid.affine
    res_x, res_y = abs(affine.a), abs(affine.e)
    try:
        geographic = bool(getattr(grid.crs, "is_geographic", False))
    except Exception:  # noqa: BLE001
        geographic = False
    if not geographic:
        # pysheds' crs is a pyproj CRS; a projected grid is already meters.
        return (res_x * res_y) / 1.0e6
    x0, y0 = grid.affine * (0, 0)
    x1, y1 = grid.affine * (grid.shape[1], grid.shape[0])
    lat_c = 0.5 * (y0 + y1)
    dx_m = res_x * 111_320.0 * max(math.cos(math.radians(lat_c)), 0.01)
    dy_m = res_y * 110_540.0
    return (dx_m * dy_m) / 1.0e6


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
        from .solver import _get_runs_bucket, _get_s3_client

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


# ---------------------------------------------------------------------------
# delineate_watershed
# ---------------------------------------------------------------------------


@register_tool(
    _WATERSHED_METADATA,
    # Writes only its own run artifact; open-world when fetching the DEM.
    open_world_hint=True,
)
def delineate_watershed(
    pour_point: tuple[float, float],
    bbox: tuple[float, float, float, float] | None = None,
    dem_uri: str | None = None,
    snap_threshold: int = _SNAP_THRESHOLD_CELLS,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> WatershedLayerURI:
    """Delineate the watershed (drainage basin) upstream of a pour point.

    Runs a D8 flow analysis (pysheds: pit/depression filling, flat
    resolution, flow direction, flow accumulation) on a DEM over the AOI,
    snaps the pour point to the nearest flow line, and returns the upstream
    catchment as a polygon layer on the map.

    When to use:
        - "What drains to this point / gauge / outfall / dam site",
          "delineate the watershed above here", "catchment boundary for this
          stream crossing".
        - As the AOI mask for downstream analyses (pass the polygon to
          ``compute_zonal_statistics`` / ``clip_raster_to_polygon``).

    When NOT to use:
        - The stream network itself (use ``extract_stream_network``).
        - Named-basin boundaries at regional scale (use
          ``fetch_nhdplus_nldi_navigate`` -- this tool is a local DEM
          delineation, clamped to a 0.3-degree AOI).

    Parameters:
        pour_point: ``(lon, lat)`` EPSG:4326 outlet. Snapped to the nearest
            cell with >= ``snap_threshold`` upslope cells (noted), so a click
            near -- not exactly on -- the channel still works.
        bbox: optional ``(min_lon, min_lat, max_lon, max_lat)`` analysis
            extent, <= 0.3 degrees per side. Default: a 0.1-degree box
            centered on the pour point. The watershed is TRUNCATED at the
            bbox edge -- enlarge the bbox if the basin looks clipped.
        dem_uri: optional override DEM (s3:// or local GeoTIFF). Default:
            Copernicus GLO-30 via ``fetch_copernicus_dem``.
        snap_threshold: upslope-cell count defining "a flow line" for the
            pour-point snap (default 100 cells).

    Returns:
        ``WatershedLayerURI`` -- the catchment polygon as a vector layer
        (GeoJSON) carrying ``area_km2``, ``cell_count``, the requested and
        snapped pour points, and honest ``notes`` (engine path, snap
        distance, truncation caveat).

    Errors (FR-AS-11): ``HydrologyAoiTooLargeError`` (bbox over the clamp),
    ``HydrologyInputError`` (bad pour point / bbox / URI),
    ``EmptyWatershedError`` (pour point produced an empty catchment),
    ``HydrologyDependencyError`` (pysheds missing),
    ``HydrologyUpstreamError`` (fetch/write failed).
    """
    lon, lat = _validate_pour_point(pour_point)
    if bbox is None:
        q_bbox = _validate_bbox(_auto_bbox(lon, lat))
        auto_note = (
            f"bbox auto-derived: {_AUTO_BBOX_DEG:g}-degree box centered on the "
            "pour point (pass bbox to widen)."
        )
    else:
        q_bbox = _validate_bbox(bbox)
        auto_note = None
    if not (q_bbox[0] <= lon <= q_bbox[2] and q_bbox[1] <= lat <= q_bbox[3]):
        raise HydrologyInputError(
            f"pour_point {(lon, lat)} is outside the analysis bbox {q_bbox!r}."
        )
    try:
        snap_cells = int(snap_threshold)
    except (TypeError, ValueError) as exc:
        raise HydrologyInputError(
            f"snap_threshold must be an integer; got {snap_threshold!r}"
        ) from exc
    if snap_cells < 1:
        raise HydrologyInputError(
            f"snap_threshold must be >= 1 cell; got {snap_threshold!r}"
        )

    notes: list[str] = [_ENGINE_NOTE]
    if auto_note:
        notes.append(auto_note)

    with tempfile.TemporaryDirectory(prefix="grace2_watershed_") as tmpdir:
        dem_path = _stage_dem(q_bbox, dem_uri, tmpdir, notes)
        grid, fdir, acc = _condition_dem(dem_path)

        # Snap the pour point to the nearest flow line (>= snap_cells).
        acc_arr = np.asarray(acc)
        snapped = _snap_to_stream(acc_arr, grid.affine, lon, lat, snap_cells)
        if snapped is not None:
            x_snap, y_snap = snapped
            notes.append(
                f"Pour point snapped to the nearest cell with >= "
                f"{snap_cells} upslope cells: ({x_snap:.6f}, {y_snap:.6f})."
            )
        else:
            x_snap, y_snap = lon, lat
            notes.append(
                f"No cell reaches the {snap_cells}-cell snap threshold; using "
                "the raw pour point (the AOI may be too small or too flat)."
            )

        try:
            catch = grid.catchment(
                x=x_snap,
                y=y_snap,
                fdir=fdir,
                xytype="coordinate",
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
            raise EmptyWatershedError(
                f"The pour point {(lon, lat)} produced an EMPTY catchment -- "
                "it likely sits on the AOI edge or off the flow grid. Move the "
                "pour point onto the channel or enlarge the bbox."
            )

        # Polygonize the catchment mask.
        try:
            from rasterio import features as rio_features
            from shapely.geometry import mapping, shape
            from shapely.ops import unary_union
        except ImportError as exc:
            raise HydrologyDependencyError(
                f"rasterio/shapely unavailable: {exc}"
            ) from exc
        try:
            geoms = [
                shape(geom)
                for geom, val in rio_features.shapes(
                    mask.astype(np.uint8), mask=mask, transform=grid.affine
                )
                if val == 1
            ]
            polygon = unary_union(geoms)
        except Exception as exc:  # noqa: BLE001
            raise HydrologyUpstreamError(
                f"polygonizing the catchment mask failed: {exc}"
            ) from exc

        area_km2 = cell_count * _cell_area_km2(grid)
        # Honest truncation caveat when the basin touches the AOI edge.
        edge = (
            mask[0, :].any()
            or mask[-1, :].any()
            or mask[:, 0].any()
            or mask[:, -1].any()
        )
        if edge:
            notes.append(
                "Catchment touches the AOI edge -- the TRUE watershed may "
                "extend beyond the bbox (area is a lower bound). Re-run with "
                "a larger bbox for the full basin."
            )

        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": mapping(polygon),
                    "properties": {
                        "area_km2": round(area_km2, 4),
                        "cell_count": cell_count,
                        "pour_point_lon": lon,
                        "pour_point_lat": lat,
                        "snapped_lon": x_snap,
                        "snapped_lat": y_snap,
                    },
                }
            ],
        }

    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "watershed", seed, _output_dir)
    minx, miny, maxx, maxy = polygon.bounds
    logger.info(
        "delineate_watershed: pour=(%.5f,%.5f) snapped=(%.5f,%.5f) -> "
        "%d cells, %.3f km^2",
        lon,
        lat,
        x_snap,
        y_snap,
        cell_count,
        area_km2,
    )
    return WatershedLayerURI(
        layer_id=f"watershed-{seed}",
        name=f"Watershed upstream of ({lon:.4f}, {lat:.4f}) -- {area_km2:.2f} km^2",
        layer_type="vector",
        uri=uri,
        style_preset="watershed_boundary",
        role="primary",
        units="km^2",
        bbox=(float(minx), float(miny), float(maxx), float(maxy)),
        area_km2=round(area_km2, 4),
        cell_count=cell_count,
        pour_point=(lon, lat),
        snapped_pour_point=(x_snap, y_snap),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# extract_stream_network
# ---------------------------------------------------------------------------


@register_tool(
    _STREAMS_METADATA,
    # Writes only its own run artifact; open-world when fetching the DEM.
    open_world_hint=True,
)
def extract_stream_network(
    bbox: tuple[float, float, float, float],
    accumulation_threshold: int = 500,
    dem_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> StreamNetworkLayerURI:
    """Extract the stream network from a DEM by D8 flow accumulation.

    Runs the pysheds D8 chain (pit/depression filling, flat resolution, flow
    direction, flow accumulation) and traces every flow path whose upslope
    area reaches ``accumulation_threshold`` cells, returning the channel
    network as a line layer on the map.

    When to use:
        - "Where are the streams / drainage lines in this area", "trace the
          channels on this DEM", terrain-derived drainage where NHD mapping
          is missing/coarse, headwater channels below mapped rivers.
        - Upstream of ``delineate_watershed`` (streams show WHERE to put the
          pour point) or paired with a flood/erosion layer.

    When NOT to use:
        - Mapped river geometry / named rivers (use ``fetch_river_geometry``
          or ``fetch_nhdplus_nldi_navigate`` -- those are surveyed, this is
          DEM-derived).
        - The basin boundary (use ``delineate_watershed``).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326, clamped to
            <= 0.3 degrees per side.
        accumulation_threshold: minimum upslope CELL COUNT for a cell to be
            channel (default 500 cells ~ 0.45 km^2 at 30 m). LOWER -> denser
            network with more headwater channels; HIGHER -> only the main
            stems.
        dem_uri: optional override DEM (s3:// or local GeoTIFF). Default:
            Copernicus GLO-30 via ``fetch_copernicus_dem``.

    Returns:
        ``StreamNetworkLayerURI`` -- the channel network as a vector layer
        (GeoJSON LineStrings, one per branch) carrying ``segment_count``,
        the threshold used, ``total_length_km`` (approximate), and honest
        ``notes`` (engine path + provenance).

    Errors (FR-AS-11): ``HydrologyAoiTooLargeError`` (bbox over the clamp),
    ``HydrologyInputError`` (bad bbox / threshold / URI), ``NoStreamsError``
    (no cell reaches the threshold -- flat AOI or threshold too high),
    ``HydrologyDependencyError`` (pysheds missing),
    ``HydrologyUpstreamError`` (fetch/write failed).
    """
    q_bbox = _validate_bbox(bbox)
    try:
        threshold = int(accumulation_threshold)
    except (TypeError, ValueError) as exc:
        raise HydrologyInputError(
            f"accumulation_threshold must be an integer cell count; "
            f"got {accumulation_threshold!r}"
        ) from exc
    if threshold < 2:
        raise HydrologyInputError(
            f"accumulation_threshold must be >= 2 cells; got {accumulation_threshold!r}"
        )

    notes: list[str] = [_ENGINE_NOTE]

    with tempfile.TemporaryDirectory(prefix="grace2_streams_") as tmpdir:
        dem_path = _stage_dem(q_bbox, dem_uri, tmpdir, notes)
        grid, fdir, acc = _condition_dem(dem_path)

        acc_arr = np.asarray(acc)
        if not bool((acc_arr >= threshold).any()):
            raise NoStreamsError(
                f"No cell reaches the {threshold}-cell accumulation threshold "
                f"over {q_bbox!r} (max accumulation "
                f"{int(acc_arr.max()) if acc_arr.size else 0} cells). Lower "
                "accumulation_threshold or enlarge the bbox."
            )
        try:
            lines = _trace_stream_network(
                np.asarray(fdir), acc_arr >= threshold, grid.affine
            )
        except Exception as exc:  # noqa: BLE001
            raise HydrologyUpstreamError(
                f"stream-network vectorization failed: {exc}"
            ) from exc
        if not lines:
            raise NoStreamsError(
                f"River-network extraction produced zero traceable branches "
                f"at the {threshold}-cell threshold over {q_bbox!r} (channel "
                "cells exist but form no 2+-cell path; lower the threshold)."
            )
        features = [
            {
                "type": "Feature",
                "id": idx,
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"branch_id": idx},
            }
            for idx, coords in enumerate(lines)
        ]
        fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
        notes.append(
            f"{len(features)} branch(es) at accumulation >= {threshold} cells."
        )

        # Approximate total length (degrees -> km at the AOI center lat for a
        # geographic grid; meters -> km for projected grids).
        total_len_km = 0.0
        lat_c = 0.5 * (q_bbox[1] + q_bbox[3])
        kx = 111.320 * max(math.cos(math.radians(lat_c)), 0.01)
        ky = 110.540
        try:
            geographic = bool(getattr(grid.crs, "is_geographic", False))
        except Exception:  # noqa: BLE001
            geographic = False
        for feat in features:
            coords = feat.get("geometry", {}).get("coordinates", [])
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                if geographic:
                    total_len_km += math.hypot((x1 - x0) * kx, (y1 - y0) * ky)
                else:
                    total_len_km += math.hypot(x1 - x0, y1 - y0) / 1000.0

    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "stream_network", seed, _output_dir)
    logger.info(
        "extract_stream_network: bbox=%s threshold=%d -> %d branch(es), "
        "~%.2f km",
        q_bbox,
        threshold,
        len(features),
        total_len_km,
    )
    return StreamNetworkLayerURI(
        layer_id=f"stream-network-{seed}",
        name=(
            f"Stream network (>= {threshold} cells) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=uri,
        style_preset="stream_network",
        role="primary",
        units="upslope cells",
        bbox=q_bbox,
        segment_count=len(features),
        accumulation_threshold=threshold,
        total_length_km=round(total_len_km, 3),
        notes=notes,
    )
