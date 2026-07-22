"""``delineate_watershed``: pysheds D8 catchment upstream of a snapped pour
point -> watershed polygon vector.

Carved out of the original two-tool ``hydrology_primitives`` module in the
tools/ reorg; behavior and the registered tool surface are unchanged. Shared
pysheds/DEM plumbing lives in
``trid3nt_server.tools.processing._hydrology_common``.
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
from trid3nt_server.tools.processing._hydrology_common import (
    HydrologyAoiTooLargeError,
    HydrologyDependencyError,
    HydrologyInputError,
    HydrologyPrimitivesError,
    HydrologyUpstreamError,
    _ENGINE_NOTE,
    _MAX_AOI_DEG,
    _condition_dem,
    _import_pysheds,
    _stage_dem,
    _stage_uri_local,
    _validate_bbox,
    _write_geojson,
)

__all__ = [
    "delineate_watershed",
    "EmptyWatershedError",
    "WatershedLayerURI",
]

logger = logging.getLogger("trid3nt_server.tools.processing.delineate_watershed")


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

#: Auto-bbox half-side (degrees) around the pour point when no bbox is given.
_AUTO_BBOX_DEG: float = 0.1

#: Default snap threshold (upslope cells) for pour-point snapping.
_SNAP_THRESHOLD_CELLS: int = 100

_WATERSHED_METADATA = AtomicToolMetadata(
    name="delineate_watershed",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)

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

    with tempfile.TemporaryDirectory(prefix="trid3nt_watershed_") as tmpdir:
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
