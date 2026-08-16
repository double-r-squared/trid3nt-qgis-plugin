"""HEC-RAS 2D riverine-flood run-output postprocessing (engine #11 landing).

``postprocess_hecras(plan_hdf, *, run_id, flow_scale, ...) -> (layers, metrics)``
reads a solved HEC-RAS plan HDF (the ``Muncie.p04.tmp.hdf`` the unsteady solve
appended a ``Results`` group to), computes the PEAK overland WATER DEPTH at each
2D flow-area cell (max water-surface elevation minus the cell's terrain bed
elevation), rasterizes it onto a regular EPSG:4326 grid, and emits the SAME
``(layers, metrics)`` shape as ``postprocess_geoclaw`` / ``postprocess_telemac`` so
the case/plugin render path consumes it unchanged.

The deliverable is the SAME shape as every other flood engine: a peak overland
DEPTH COG (``layers[0]``, ``role="primary"``, style preset
``continuous_flood_depth``) as the map anchor + narration carrier, PLUS the 2D
flow-area MESH-preview vector layer (``layers[1]``, ``role="context"``,
``mesh_grid``) so the modeled domain renders beside the result (the M1/M2/M3 mesh
paradigm; render-mesh-in-proofs norm).

Honesty floor (invariant 1): every depth scalar is computed with plain
arithmetic from the HDF -- no LLM anywhere. The COG carries a LOUD
demonstration-geometry label so a Muncie what-if is never read as a user-AOI study.

Depth math (US Customary -- the Muncie model's Units System):
``depth[c] = max(0, MaxWaterSurface[c] - CellMinimumElevation[c])`` masked to wet
cells (a dry cell stores WSE == 0, so depth = -bed < 0 and is dropped -- dry
terrain is never painted as water). ``h5py``/``pyproj``/``rasterio`` are
lazy-imported so this module stays offline-suite-safe with no new hard dep.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI, LegendKey
from trid3nt_contracts.hecras_contracts import (
    HECRAS_DEPTH_STYLE_PRESET,
    HECRAS_OUTPUT_EMPTY,
    HECRAS_SOLVE_FAILED,
    HecrasDepthLayerURI,
)

from trid3nt_server.mesh.hecras_geometry import read_2d_flow_area_cells
from trid3nt_server.workflows.shared import cog_io
from trid3nt_server.workflows.shared.cog_io import CogIoError

__all__ = [
    "PostprocessHecrasError",
    "postprocess_hecras",
    "make_hecras_mesh_layer_uri",
    "HECRAS_WET_DEPTH_FT",
    "HECRAS_TARGET_GROUND_RES_M",
]

logger = logging.getLogger("trid3nt_server.workflows.hecras.postprocess_hecras")

#: Depth (ft) above which a 2D cell counts as WET. Below it a cell is dry / no-data
#: (a dry HEC-RAS cell stores WSE == 0, so depth resolves negative and is masked
#: regardless; this floor also drops numerically-thin films). ~0.1 ft ~ 3 cm,
#: mirroring the flood engines' 1 cm wet threshold in this US Customary model.
HECRAS_WET_DEPTH_FT: float = 0.1

#: HEC-RAS fill sentinel (large positive) for a no-data cell/face.
_HDF_FILL: float = 1e29

#: Target GROUND resolution (m/px) for the adaptive depth COG + the px caps
#: (mirrors the GeoClaw/TELEMAC adaptive sizing). The Muncie 2D area is a few km
#: across, so ~15 m/px keeps the inundation a smooth sheet, not chunky specks.
HECRAS_TARGET_GROUND_RES_M: float = 15.0
_MIN_PX_PER_SIDE: int = 128
_MAX_PX_PER_SIDE: int = 2500

#: Results path inside a solved plan HDF (the Summary Output 2D max-WSE block).
_RESULTS_2D = (
    "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/"
    "2D Flow Areas"
)
_GEOM_2D = "Geometry/2D Flow Areas"
#: Event Conditions inflow hydrograph group (for the forcing chart series).
_FLOW_HYDROGRAPHS = "Event Conditions/Unsteady/Boundary Conditions/Flow Hydrographs"


class PostprocessHecrasError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the A.6 open-set so the agent emitter renders a typed
    error frame (``HECRAS_OUTPUT_EMPTY`` -- no Results / no wet cells;
    ``HECRAS_SOLVE_FAILED`` -- read/rasterize/write/upload fault).
    """

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


def _first_area_name(f: Any) -> str:
    import h5py  # lazy

    if _GEOM_2D not in f:
        raise PostprocessHecrasError(
            HECRAS_OUTPUT_EMPTY, message=f"no '{_GEOM_2D}' group -- not a 2D model"
        )
    keys = [k for k in f[_GEOM_2D] if isinstance(f[_GEOM_2D][k], h5py.Group)]
    if not keys:
        raise PostprocessHecrasError(
            HECRAS_OUTPUT_EMPTY, message="no 2D flow area sub-group present"
        )
    return keys[0]


def _read_depth_per_cell(
    plan_hdf: Path, *, allow_dry: bool = False
) -> tuple[Any, Any, str, dict[str, Any]]:
    """Return ``(depth_ft, wet_mask, area_name, stats)`` per 2D cell.

    ``depth_ft`` is the peak WATER DEPTH (max WSE minus bed elevation, feet),
    ``wet_mask`` marks cells with depth > the wet floor. Raises
    ``HECRAS_OUTPUT_EMPTY`` when no Results.

    A solve with ZERO wet cells raises ``HECRAS_OUTPUT_EMPTY`` UNLESS ``allow_dry``
    -- the levee-breach archetype's levee-HOLDS case is a VALID DRY SUCCESS (the
    protected 2D floodplain stayed dry), returned with zeroed stats rather than an
    error.
    """
    import h5py  # lazy
    import numpy as np

    with h5py.File(plan_hdf, "r") as f:
        area = _first_area_name(f)
        res_ws = f.get(f"{_RESULTS_2D}/{area}/Maximum Water Surface")
        if res_ws is None:
            raise PostprocessHecrasError(
                HECRAS_OUTPUT_EMPTY,
                message=f"no Results max-WSE for 2D area {area!r} (solve wrote no results)",
            )
        ws = np.asarray(res_ws[()], dtype=np.float64)
        # Summary max-WSE is (n_cells,) or (1, n_cells); take the first row.
        wse = ws[0] if ws.ndim == 2 else ws
        bed = np.asarray(
            f[f"{_GEOM_2D}/{area}/Cells Minimum Elevation"][()], dtype=np.float64
        )

    wse = np.where(np.abs(wse) > _HDF_FILL, np.nan, wse)
    # A dry cell stores WSE == 0; force it to NaN so depth is never bed-referenced
    # from a zero surface (would read as a huge negative and mask anyway, but this
    # keeps the arithmetic honest).
    wse = np.where(wse <= 0.0, np.nan, wse)
    n = min(wse.shape[0], bed.shape[0])
    wse, bed = wse[:n], bed[:n]
    depth = wse - bed
    depth = np.where(np.isfinite(depth) & (depth > 0.0), depth, np.nan)
    wet = np.isfinite(depth) & (depth > HECRAS_WET_DEPTH_FT)

    wet_count = int(wet.sum())
    if wet_count == 0 and not allow_dry:
        raise PostprocessHecrasError(
            HECRAS_OUTPUT_EMPTY,
            message="the solve produced no wet 2D cells (empty inundation)",
        )
    if wet_count == 0:
        # Valid dry success (levee held): zeroed stats, all-dry depth/mask.
        stats = {
            "n_cells": int(bed.shape[0]),
            "wet_cell_count": 0,
            "depth_max_ft": 0.0,
            "depth_mean_ft": 0.0,
            "wse_max_ft": (float(np.nanmax(wse)) if bool(np.isfinite(wse).any()) else 0.0),
        }
        return depth, wet, area, stats
    stats = {
        "n_cells": int(bed.shape[0]),
        "wet_cell_count": wet_count,
        "depth_max_ft": float(np.nanmax(depth)),
        "depth_mean_ft": float(np.nanmean(depth[wet])),
        "wse_max_ft": float(np.nanmax(wse)),
    }
    return depth, wet, area, stats


def _adaptive_grid(bbox: list[float]) -> tuple[int, int]:
    """Pick ``(width_px, height_px)`` for the depth COG from the 4326 bbox."""
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat_mid)), 1e-6)
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat
    w = int(round(width_m / HECRAS_TARGET_GROUND_RES_M))
    h = int(round(height_m / HECRAS_TARGET_GROUND_RES_M))
    w = max(_MIN_PX_PER_SIDE, min(w, _MAX_PX_PER_SIDE))
    h = max(_MIN_PX_PER_SIDE, min(h, _MAX_PX_PER_SIDE))
    return w, h


def _read_inflow_series(plan_hdf: Path, flow_scale: float) -> list[dict[str, float]]:
    """The scaled inflow hydrograph as ``[{t_hr, q_cfs}]`` for the forcing chart.

    Reads the Event Conditions hydrograph (time, baseline flow) and multiplies the
    flow by ``flow_scale`` so the chart shows the forcing the run ACTUALLY used
    (invariant 1). Best-effort: returns ``[]`` on any read fault (the chart is a
    nice-to-have, never a correctness gate).
    """
    import h5py  # lazy
    import numpy as np

    try:
        with h5py.File(plan_hdf, "r") as f:
            if _FLOW_HYDROGRAPHS not in f:
                return []
            grp = f[_FLOW_HYDROGRAPHS]
            keys = [k for k in grp if isinstance(grp[k], h5py.Dataset)]
            if not keys:
                return []
            arr = np.asarray(grp[keys[0]][()], dtype=np.float64)  # (n, 2) [t_days, q]
        series = []
        for t_days, q in arr:
            series.append(
                {"t_hr": round(float(t_days) * 24.0, 3), "q_cfs": round(float(q) * flow_scale, 1)}
            )
        return series
    except Exception as exc:  # noqa: BLE001 -- chart is best-effort
        logger.warning("hecras inflow-series read failed (non-fatal): %s", exc)
        return []


def postprocess_hecras(
    plan_hdf: str | Path,
    *,
    run_id: str,
    flow_scale: float = 1.0,
    peak_inflow_cfs: float | None = None,
    volume_error_pct: float | None = None,
    runs_bucket: str | None = None,
    fallback_note: str | None = None,
    allow_dry: bool = False,
    breach_enabled: bool | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Rasterize a solved HEC-RAS 2D result to a peak-depth COG + mesh preview.

    Args:
        plan_hdf: local path to the solved plan HDF (Results-bearing).
        run_id: the run id (COG / mesh S3 prefix).
        flow_scale / peak_inflow_cfs / volume_error_pct: the run provenance the
            headline layer carries (invariant 1).
        runs_bucket: override; else ``TRID3NT_RUNS_BUCKET``.
        fallback_note: the demonstration-geometry honesty floor stamped on the
            layer (the composer supplies the LOUD wording).
        allow_dry: when ``True``, a zero-wet-cell solve is a VALID DRY SUCCESS (the
            levee-breach levee-HOLDS case) -- an all-nodata depth COG + zeroed stats
            rather than a ``HECRAS_OUTPUT_EMPTY`` error.
        breach_enabled: the levee scenario the layer carries (``True`` failed /
            ``False`` held / ``None`` riverine) -- surfaced so a dry result reads as
            "levee held", never a failure.

    Returns:
        ``(layers, metrics)``: ``layers[0]`` the ``HecrasDepthLayerURI`` peak-depth
        COG (primary), ``layers[1]`` the mesh-preview vector layer (context, may be
        absent on a mesh-read fault). ``metrics`` carries the depth stats + the
        inflow series for the forcing chart.

    Raises:
        PostprocessHecrasError: no Results / no wet cells / rasterize / write /
            upload fault.
    """
    plan_hdf = Path(plan_hdf)
    depth, wet, area_name, stats = _read_depth_per_cell(plan_hdf, allow_dry=allow_dry)
    is_dry = stats["wet_cell_count"] == 0

    # Cell polygons (EPSG:4326) from the shared mesh reader -- reused for BOTH the
    # rasterization (paint each cell its depth) and the mesh-preview layer.
    try:
        fc, mesh_stats = read_2d_flow_area_cells(str(plan_hdf), area_name=area_name, max_cells=100_000)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessHecrasError(
            HECRAS_SOLVE_FAILED, message=f"2D mesh read failed: {exc}"
        ) from exc

    import numpy as np
    import rasterio.features
    from rasterio.transform import from_bounds

    bbox = list(mesh_stats["bbox"])  # [min_lon, min_lat, max_lon, max_lat]
    width_px, height_px = _adaptive_grid(bbox)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px)

    if is_dry:
        # Levee-HELD: the protected side stayed dry -- an all-nodata depth sheet
        # (nothing paints; the empty map IS the answer "the levee held").
        grid = np.full((height_px, width_px), float("nan"), dtype="float32")
    else:
        # Build (geometry, depth) shapes for the WET cells only (dry cells
        # contribute nothing so the sheet edge is crisp).
        shapes: list[tuple[dict, float]] = []
        for feat in fc["features"]:
            if feat["properties"].get("role") != "cell":
                continue
            cid = int(feat["properties"]["cell_id"])
            if cid >= depth.shape[0] or not wet[cid]:
                continue
            d = float(depth[cid])
            if d > HECRAS_WET_DEPTH_FT:
                shapes.append((feat["geometry"], d))

        if not shapes:
            raise PostprocessHecrasError(
                HECRAS_OUTPUT_EMPTY, message="no wet cell polygons to rasterize"
            )

        grid = rasterio.features.rasterize(
            shapes,
            out_shape=(height_px, width_px),
            transform=transform,
            fill=float("nan"),
            dtype="float32",
            all_touched=False,
        )

    try:
        cog_path = cog_io.write_cog_4326_from_grid(
            grid,
            src_crs="EPSG:4326",
            src_transform=transform,
            reproject=False,
            crs_roundtrip_guard=False,
        )
    except CogIoError as exc:
        raise PostprocessHecrasError(
            HECRAS_SOLVE_FAILED, message=f"depth COG write failed: {exc}"
        ) from exc

    try:
        cog_uri = cog_io.upload_cog(
            cog_path,
            run_id,
            runs_bucket,
            dest_filename="hecras_depth_peak.tif",
            log_label="HEC-RAS depth COG",
        )
    except CogIoError as exc:
        raise PostprocessHecrasError(
            HECRAS_SOLVE_FAILED, message=f"depth COG upload failed: {exc}"
        ) from exc
    finally:
        cog_io.safe_unlink(cog_path)

    # A dry sheet has no depth range; give the legend a nominal wet-floor span so
    # the LegendKey stays valid (the raster is all-nodata, so nothing paints).
    legend_vmax = round(stats["depth_max_ft"], 3) if not is_dry else HECRAS_WET_DEPTH_FT
    dry_name = " -- LEVEE HELD (protected side dry)" if is_dry else ""
    depth_layer = HecrasDepthLayerURI(
        layer_id=f"hecras-depth-peak-{run_id}",
        name=f"Peak flood depth (HEC-RAS 2D, {area_name}){dry_name}",
        layer_type="raster",
        uri=cog_uri,
        style_preset=HECRAS_DEPTH_STYLE_PRESET,
        role="primary",
        units="ft",
        bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
        legend=LegendKey(
            kind="continuous",
            colormap="blues",
            vmin=0.0,
            vmax=legend_vmax,
            units="ft",
            label="Peak water depth (ft)",
        ),
        fallback_note=fallback_note,
        depth_max_ft=round(stats["depth_max_ft"], 3),
        depth_mean_ft=round(stats["depth_mean_ft"], 3),
        wet_cell_count=stats["wet_cell_count"],
        wse_max_ft=round(stats["wse_max_ft"], 3),
        flow_scale=float(flow_scale),
        peak_inflow_cfs=(round(float(peak_inflow_cfs), 1) if peak_inflow_cfs is not None else None),
        volume_error_pct=(round(float(volume_error_pct), 6) if volume_error_pct is not None else None),
        n_cells=stats["n_cells"],
        breach_enabled=breach_enabled,
    )

    layers: list[LayerURI] = [depth_layer]
    mesh_layer = make_hecras_mesh_layer_uri(fc, mesh_stats, run_id=run_id, runs_bucket=runs_bucket)
    if mesh_layer is not None:
        layers.append(mesh_layer)

    metrics = {
        **stats,
        "area_name": area_name,
        "flow_scale": float(flow_scale),
        "peak_inflow_cfs": peak_inflow_cfs,
        "volume_error_pct": volume_error_pct,
        "breach_enabled": breach_enabled,
        "is_dry": is_dry,
        "bbox": bbox,
        "cog_uri": cog_uri,
        "inflow_hydrograph": _read_inflow_series(plan_hdf, float(flow_scale)),
    }
    logger.info(
        "postprocess_hecras run_id=%s area=%s depth_max=%.2f ft wet_cells=%d "
        "flow_scale=%.3f peak=%s cfs uri=%s",
        run_id, area_name, stats["depth_max_ft"], stats["wet_cell_count"],
        flow_scale, peak_inflow_cfs, cog_uri,
    )
    return layers, metrics


def make_hecras_mesh_layer_uri(
    fc: dict,
    mesh_stats: dict,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """Upload the 2D flow-area mesh ``FeatureCollection`` to S3, return a LayerURI.

    Mirrors ``make_swmm_mesh_layer_uri``: writes ``mesh.geojson`` to the durable
    runs bucket at ``s3://<runs_bucket>/<run_id>/mesh.geojson`` and returns a
    ``style_preset="mesh_grid"``, ``role="context"``, ``bbox=None`` LayerURI (the
    mesh must not fight the flood camera). Best-effort: ``None`` on empty FC or an
    S3 fault (a missing mesh preview never voids the depth result).

    SYNC boto3 put -- the caller wraps this in ``asyncio.to_thread``.
    """
    import json as _json

    features = fc.get("features") or []
    if not features:
        return None
    try:
        from trid3nt_server.data.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/mesh.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=_json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 -- best-effort mesh preview
        logger.warning(
            "make_hecras_mesh_layer_uri: mesh.geojson S3 upload failed (non-fatal, "
            "run_id=%s): %s", run_id, exc,
        )
        return None

    n_cells = int(mesh_stats.get("n_cells", 0) or 0)
    return LayerURI(
        layer_id=f"hecras-mesh-{run_id}",
        name=f"Computational mesh ({n_cells} 2D cells)",
        layer_type="vector",
        uri=s3_uri,
        style_preset="mesh_grid",
        role="context",
        bbox=None,
    )
