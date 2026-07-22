"""PySWMM quasi-2D urban-flood run-output postprocessing (sprint-16 P3, Path A).

``postprocess_swmm(run, build, *, run_id, ...) -> (layers, metrics)`` reads the
per-timestep node ``INVERT_DEPTH`` from a solved SWMM ``.out`` (via the pyswmm
``Output`` binary API), SCATTERS each storage node's depth back onto the
mesh-cell ``(H, W)`` grid the deck was built from, masks dropped/building cells
+ sub-threshold cells to NaN, and emits the SAME ``(layers, metrics)`` shape as
``postprocess_flood`` so the Phase-1 flood-animation scrubber path consumes it
UNCHANGED:

  - ``layers[0]`` = the PEAK overland-depth COG, role ``"primary"``, name
    ``"Peak flood depth"``, style preset ``continuous_flood_depth``. It is a
    :class:`~grace2_contracts.swmm_contracts.SWMMDepthLayerURI` carrying the
    three narration scalars (``max_depth_m`` / ``flooded_area_km2`` /
    ``n_buildings_affected``) + the tagged barrier geometry echoed back.
  - ``layers[1:]`` = up to ``MAX_FLOOD_FRAMES`` per-timestep depth COGs, role
    ``"context"``, names ``"Flood depth step N"`` (N = 1..k, contiguous,
    1-based) — the EXACT web ``parseFrameToken`` / ``detectSequentialGroups``
    token so the LayerPanel collapses them into one bottom-center-scrubber
    temporal group. Each frame lands at a DISTINCT runs-bucket key so its
    TiTiler ``url=`` (hence ``_layer_identity_key``) is unique (no dedup
    collapse). The frames are also ``SWMMDepthLayerURI`` (the depth scalars on a
    frame describe THAT frame; the agent narrates from ``layers[0]``).

This is the SWMM analogue of ``postprocess_flood`` (SFINCS) and
``postprocess_modflow`` (MF6-GWT). The defining difference: SWMM emits
NODE/LINK results, NOT a raster. There is no ``zs(time,...)`` field to slice —
we rasterize per-timestep node depth onto the mesh grid ourselves. The
cell<->node mapping is already FULLY EXPOSED by the builder: every active cell
``(i, j)`` owns the storage node named ``S_{i}_{j}`` (``swmm_mesh_builder._cell_node``),
and ``BuildResult`` carries the ``(grid_shape, crs, transform, resolution_m,
outfall_cell, n_buildings_dropped, barriers_geojson)`` provenance the scatter +
georegistration need. No builder change is required.

Reuse (do NOT reinvent): the COG-write + CRS round-trip guard pattern from
``postprocess_flood._write_verified_cog`` (adapted for a projected-metres grid
reprojected to EPSG:4326, like ``postprocess_modflow._write_reprojected_cog``,
since the MapLibre basemap is web-mercator/4326), the even-subsample frame
selector ``_select_frame_time_indices`` (MAX_FLOOD_FRAMES=24), the
``NODATA_DEPTH_M=0.05`` wet threshold, and the
``continuous_flood_depth`` style preset. The honesty floor (Invariant 1 /
FR-AS-7): the depth scalars are computed with plain arithmetic from the depth
grid — no LLM anywhere; the agent narrates the typed fields, never invents them.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware
via ``cache.storage_scheme()``); the agent does not re-render — ``publish_layer``
/ TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from grace2_contracts.swmm_contracts import SWMMDepthLayerURI

from . import cog_io
from .cog_io import CogIoError

# Reuse the SFINCS postprocess constants/helpers (single source of truth so the
# SWMM + SFINCS animation paths stay byte-compatible on the web side).
from .postprocess_flood import (
    FLOOD_DEPTH_STYLE_PRESET,
    MAX_FLOOD_FRAMES,
    NODATA_DEPTH_M,
    RUNS_BUCKET_DEFAULT,
    _select_frame_time_indices,
)

__all__ = [
    "PostprocessSWMMError",
    "postprocess_swmm",
    "postprocess_swmm_pollutants",
    "publish_swmm_quantities",
    "scatter_node_depths_to_grid",
    "scatter_node_attr_to_grid",
    "scatter_link_attr_to_grid",
    "compute_swmm_depth_metrics",
    "read_outfall_loading",
    "read_runoff_quality_built_washed",
    "CONCENTRATION_STYLE_PRESET",
    "FLOOD_DEPTH_STYLE_PRESET",
    "NODATA_DEPTH_M",
    "MAX_FLOOD_FRAMES",
    "RUNS_BUCKET_DEFAULT",
]

#: Sequential style preset for the per-cell peak washoff-concentration COG (the
#: single raster-styling seam; the actual TiTiler (rescale, colormap) lives in
#: publish_layer._resolve_titiler_style_params under this key).
CONCENTRATION_STYLE_PRESET = "continuous_concentration"

logger = logging.getLogger("grace2_agent.workflows.postprocess_swmm")


class PostprocessSWMMError(RuntimeError):
    """Raised on read / scatter / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame. Codes used here:

    - ``SWMM_OUTPUT_READ_FAILED`` — could not open / read the ``.out`` binary
      (missing file, pyswmm Output failure).
    - ``SWMM_OUTPUT_EMPTY`` — the ``.out`` carries no reporting timesteps / no
      mesh nodes — nothing to rasterize.
    - ``SWMM_DEPENDENCY_MISSING`` — pyswmm / swmm.toolkit / rasterio / numpy not
      importable in the runtime (lazy import failed); surfaces honestly typed.
    - ``SWMM_COG_WRITE_FAILED`` — rasterio could not write the depth COG.
    - ``SWMM_COG_REPROJECT_FAILED`` — the projected-metres -> EPSG:4326 warp
      failed.
    - ``SWMM_CRS_TAG_MISMATCH`` — the COG CRS tag did not round-trip (the
      TiTiler-wedge / mistagged-raster guard, mirrors postprocess_flood).
    - ``SWMM_COG_UPLOAD_FAILED`` — the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_SWMM_FAILED"

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


# --------------------------------------------------------------------------- #
# Node-name <-> cell-grid mapping (the builder's S_{i}_{j} convention).
# --------------------------------------------------------------------------- #
def _parse_cell_node(name: str) -> tuple[int, int] | None:
    """Parse a storage-node name ``S_<i>_<j>`` back to its ``(row, col)`` cell.

    Returns ``None`` for any non-cell node (the boundary ``OUT`` outfall, or a
    name that does not match the ``S_<int>_<int>`` shape) so the scatter skips
    it. This is the inverse of ``swmm_mesh_builder._cell_node`` — the SINGLE
    cell<->node accessor the builder already exposes through its naming
    convention (no builder change needed).
    """
    if not isinstance(name, str) or not name.startswith("S_"):
        return None
    parts = name.split("_")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def scatter_node_depths_to_grid(
    depth_by_node: dict[str, float],
    grid_shape: tuple[int, int],
) -> Any:
    """Scatter a ``{node_name: depth_m}`` snapshot onto the mesh-cell ``(H, W)`` grid.

    Each active cell ``(i, j)`` owns the storage node ``S_{i}_{j}``; its depth is
    written to ``grid[i, j]``. Cells with NO node (DROPPED buildings, or cells
    outside the active mesh) stay ``NaN`` — a hole in the mesh the renderer hides.
    Sub-threshold cells (``< NODATA_DEPTH_M``) are masked to ``NaN`` so the COG is
    dry-cell-aware (matches the SFINCS / MODFLOW convention + the
    ``continuous_flood_depth`` QML alpha=0 stop). The boundary ``OUT`` outfall is
    skipped (it is not a mesh cell). Pure numpy — unit-testable on a synthetic
    snapshot.
    """
    import numpy as np  # local — caller vouched for the import path

    nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    for name, depth in depth_by_node.items():
        rc = _parse_cell_node(name)
        if rc is None:
            continue
        i, j = rc
        if not (0 <= i < nrows and 0 <= j < ncols):
            continue
        d = float(depth)
        # sub-threshold (and non-positive) cells are dry -> NaN.
        grid[i, j] = d if d >= NODATA_DEPTH_M else np.nan
    return grid


# --------------------------------------------------------------------------- #
# levers STEP 3: generalized scatter for ANY node/link attribute.
#
# The existing depth scatter masks SUB-THRESHOLD cells to NaN (a dry-depth
# convention). The generic scatter has NO dry-floor (a flooding rate / ponded
# volume / conduit flow of 0 is meaningful "no flow", masked to NaN only when it
# is exactly 0 so the renderer hides null cells but keeps every active value).
# --------------------------------------------------------------------------- #
def _parse_conduit_link(name: str) -> tuple[int, int] | None:
    """Parse an overland conduit name ``L_<fi>_<fj>__<ti>_<tj>`` -> the (ti, tj)
    DOWNSTREAM cell (where the flow lands). Returns ``None`` for the boundary
    feeder ``L_OUTLET`` / a flap-gate ``FLAP_*`` / any non-overland link.
    """
    if not isinstance(name, str) or not name.startswith("L_") or "__" not in name:
        return None
    _frm, _, to = name.partition("__")
    parts = to.split("_")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def scatter_node_attr_to_grid(
    value_by_node: dict[str, float],
    grid_shape: tuple[int, int],
    *,
    signed: bool = False,
) -> Any:
    """Scatter a ``{node_name: value}`` snapshot onto the mesh ``(H, W)`` grid.

    Each cell ``(i, j)`` owns node ``S_{i}_{j}`` (the builder convention). Cells
    with no node stay NaN. A value of EXACTLY 0 is masked to NaN (no flow / no
    pond -> the renderer hides it); every non-zero value is kept. When
    ``signed`` is False, negative values are clamped to NaN (a magnitude field
    like FLOODING_LOSSES / PONDED_VOLUME is non-negative); when True the sign is
    preserved (a diverging field). Pure numpy.
    """
    import numpy as np  # local

    nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    for name, raw in value_by_node.items():
        rc = _parse_cell_node(name)
        if rc is None:
            continue
        i, j = rc
        if not (0 <= i < nrows and 0 <= j < ncols):
            continue
        v = float(raw)
        if v == 0.0 or (not signed and v < 0.0):
            continue  # leave NaN (no flow / clamp negative magnitude)
        grid[i, j] = v
    return grid


def scatter_link_attr_to_grid(
    value_by_link: dict[str, float],
    grid_shape: tuple[int, int],
    *,
    signed: bool = False,
) -> Any:
    """Scatter a ``{conduit_name: value}`` snapshot onto the DOWNSTREAM cell grid.

    Each overland conduit ``L_<fi>_<fj>__<ti>_<tj>`` deposits its value at the
    downstream cell ``(ti, tj)`` (where the flow lands). When two conduits share
    a downstream cell the LARGER MAGNITUDE wins (the headline flow/velocity at
    that cell). Zero -> NaN; negatives kept iff ``signed``. Boundary feeders /
    flap gates are skipped. Pure numpy.
    """
    import numpy as np  # local

    nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    for name, raw in value_by_link.items():
        rc = _parse_conduit_link(name)
        if rc is None:
            continue
        i, j = rc
        if not (0 <= i < nrows and 0 <= j < ncols):
            continue
        v = float(raw)
        if v == 0.0:
            continue
        if not signed:
            v = abs(v)
        cur = grid[i, j]
        if np.isnan(cur) or abs(v) > abs(cur):
            grid[i, j] = v
    return grid


# --------------------------------------------------------------------------- #
# Pure metric math (unit-testable on a synthetic peak grid).
# --------------------------------------------------------------------------- #
def compute_swmm_depth_metrics(
    peak_grid: Any,
    *,
    resolution_m: float,
    building_footprints: Any = None,
    grid_crs: str | None = None,
    grid_transform: Any = None,
) -> dict[str, Any]:
    """Compute the three narration scalars from the PEAK depth grid.

    Pure arithmetic over the masked peak grid (sub-threshold + non-cell already
    NaN):

      - ``max_depth_m``       global max over the wet cells (0.0 if all dry).
      - ``flooded_area_km2``  ``(#wet cells) * resolution_m^2 / 1e6``.
      - ``n_buildings_affected`` count of building footprints touched by a wet
        cell. When ``building_footprints`` + the grid georegistration are
        supplied we rasterize the footprints onto the grid and count those whose
        rasterized cells intersect the wet mask; otherwise (no footprints / no
        georegistration) the count is 0 — an HONEST under-report rather than an
        invented number (the agent narrates a typed field, never fabricates).

    Also returns ``mean_depth_m`` / ``p95_depth_m`` / ``flooded_cell_count`` for
    parity with the SFINCS ``peak_metrics`` dict (the FloodMetrics consumers read
    those keys).
    """
    import numpy as np  # local — caller vouched for the import path

    arr = np.asarray(peak_grid, dtype="float64")
    wet_mask = np.isfinite(arr)
    wet = arr[wet_mask]
    cell_area_m2 = float(resolution_m) * float(resolution_m)

    if wet.size == 0:
        metrics: dict[str, Any] = {
            "max_depth_m": 0.0,
            "mean_depth_m": 0.0,
            "p95_depth_m": 0.0,
            "flooded_cell_count": 0,
            "flooded_area_km2": 0.0,
            "n_buildings_affected": 0,
        }
        return metrics

    flooded_cell_count = int(wet.size)
    metrics = {
        "max_depth_m": float(np.nanmax(wet)),
        "mean_depth_m": float(np.nanmean(wet)),
        "p95_depth_m": float(np.nanpercentile(wet, 95)),
        "flooded_cell_count": flooded_cell_count,
        "flooded_area_km2": flooded_cell_count * cell_area_m2 / 1_000_000.0,
        "n_buildings_affected": _count_buildings_affected(
            wet_mask, building_footprints, grid_crs, grid_transform
        ),
    }
    return metrics


def _count_buildings_affected(
    wet_mask: Any,
    building_footprints: Any,
    grid_crs: str | None,
    grid_transform: Any,
) -> int:
    """Count building footprints touched by a wet cell.

    Rasterizes each footprint (its own value) onto the grid (in the grid CRS) and
    counts the distinct footprint labels whose rasterized cells overlap the wet
    mask. Degrades to 0 (honest under-report) when footprints / georegistration
    are absent or rasterization fails — never raises (a metric is best-effort,
    never the thing that sinks a real layer).
    """
    if building_footprints is None or grid_crs is None or grid_transform is None:
        return 0
    try:
        import numpy as np
        from rasterio.features import rasterize
        from rasterio.warp import transform_geom
    except Exception:  # noqa: BLE001
        return 0

    # Normalise footprints -> list of geometry mappings (GeoJSON WGS84 / shapely).
    geoms: list[dict] = []
    if isinstance(building_footprints, dict) and (
        building_footprints.get("type") == "FeatureCollection"
    ):
        for feat in building_footprints.get("features", []) or []:
            g = feat.get("geometry")
            if isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon"):
                geoms.append(g)
    elif isinstance(building_footprints, (list, tuple)):
        try:
            from shapely.geometry import mapping as shp_mapping

            for f in building_footprints:
                if isinstance(f, dict):
                    geoms.append(f)
                else:
                    geoms.append(shp_mapping(f))
        except Exception:  # noqa: BLE001
            geoms = [f for f in building_footprints if isinstance(f, dict)]
    if not geoms:
        return 0

    nrows, ncols = wet_mask.shape
    try:
        # Reproject each footprint into the grid CRS, then burn a UNIQUE label
        # per footprint (label = index+1; 0 = background).
        shapes = []
        for idx, g in enumerate(geoms, start=1):
            try:
                pg = transform_geom("EPSG:4326", grid_crs, g)
            except Exception:  # noqa: BLE001
                continue
            shapes.append((pg, idx))
        if not shapes:
            return 0
        labelled = rasterize(
            shapes,
            out_shape=(nrows, ncols),
            transform=grid_transform,
            fill=0,
            all_touched=True,
            dtype="int32",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("postprocess_swmm: building rasterize for metrics failed (%s)", exc)
        return 0

    import numpy as np

    touched = labelled[(labelled > 0) & np.asarray(wet_mask)]
    return int(np.unique(touched).size)


# --------------------------------------------------------------------------- #
# Read the .out -> per-timestep node depth snapshots.
# --------------------------------------------------------------------------- #
def _read_node_depth_snapshots(
    out_path: str, grid_shape: tuple[int, int]
) -> tuple[list[Any], int]:
    """Read every reporting timestep's node ``INVERT_DEPTH`` as a scattered grid.

    Returns ``(grids, n_steps)`` where ``grids`` is a list of ``(H, W)`` numpy
    arrays (one per reporting step, time-ascending; dropped/sub-threshold cells =
    NaN). Uses the pyswmm ``Output`` binary API: ``node_attribute(INVERT_DEPTH,
    t)`` returns ``{node_name: depth_m}`` for ALL nodes at step ``t``, which we
    scatter via :func:`scatter_node_depths_to_grid`. Raises a typed
    ``PostprocessSWMMError`` on a missing dependency / read failure / empty
    output.
    """
    try:
        from pyswmm import Output
        from swmm.toolkit.shared_enum import NodeAttribute
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"pyswmm / swmm.toolkit unavailable for .out read: {exc}",
            details={"out_path": out_path},
        ) from exc

    if not Path(out_path).exists():
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"SWMM .out not found at {out_path}",
            details={"out_path": out_path},
        )

    grids: list[Any] = []
    try:
        with Output(out_path) as out:
            times = out.times  # property: list of datetimes, one per report step
            nodes = out.nodes  # property: dict node_name -> index
            n_steps = len(times)
            if n_steps <= 0 or len(nodes) <= 0:
                raise PostprocessSWMMError(
                    "SWMM_OUTPUT_EMPTY",
                    message=(
                        f"SWMM .out carries no reporting timesteps "
                        f"({n_steps}) / no nodes ({len(nodes)})"
                    ),
                    details={"out_path": out_path},
                )
            for t in range(n_steps):
                depth_by_node = out.node_attribute(NodeAttribute.INVERT_DEPTH, t)
                grids.append(scatter_node_depths_to_grid(depth_by_node, grid_shape))
    except PostprocessSWMMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"could not read node depths from {out_path}: {exc}",
            details={"out_path": out_path},
        ) from exc

    return grids, n_steps


def _peak_grid_from_snapshots(grids: list[Any]) -> Any:
    """Select the PEAK snapshot — the step with the largest total wet depth.

    Mirrors ``run_swmm_deck``'s peak-volume selection: the meaningful wet state
    is the timestep whose summed cell depth is greatest (the flood crest), NOT a
    per-cell max-over-time (which would mix non-coincident peaks). Returns an
    all-NaN grid if every snapshot is dry.
    """
    import numpy as np

    best_grid = None
    best_sum = -1.0
    for g in grids:
        s = float(np.nansum(g))
        if s > best_sum:
            best_sum = s
            best_grid = g
    if best_grid is None:
        # no snapshots at all — defensive; the caller guards n_steps>0.
        return np.full((1, 1), np.nan, dtype="float64")
    return best_grid


# --------------------------------------------------------------------------- #
# COG write (projected-metres grid -> EPSG:4326) + CRS round-trip guard.
# --------------------------------------------------------------------------- #
#: stage -> (SWMM error_code) map for re-raising cog_io's generic CogIoError as
#: the engine's typed error (STEP 1 dedupe; byte-identical error codes).
_SWMM_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "SWMM_DEPENDENCY_MISSING",
    "WRITE": "SWMM_COG_WRITE_FAILED",
    "REPROJECT": "SWMM_COG_REPROJECT_FAILED",
    "CRS_MISMATCH": "SWMM_CRS_TAG_MISMATCH",
    "UPLOAD": "SWMM_COG_UPLOAD_FAILED",
}


def _reraise_cogio(exc: CogIoError, *, grid_crs: str | None = None) -> "PostprocessSWMMError":
    """Map a cog_io ``CogIoError`` onto the SWMM typed error (preserves codes)."""
    code = _SWMM_STAGE_CODES.get(exc.stage, "POSTPROCESS_SWMM_FAILED")
    details = dict(exc.details)
    if grid_crs is not None and "grid_crs" not in details:
        details["grid_crs"] = grid_crs
    return PostprocessSWMMError(code, message=exc.message, details=details)


def _write_depth_cog_4326(
    grid: Any,
    *,
    grid_crs: str,
    grid_transform: Any,
) -> Path:
    """Write a masked ``(H, W)`` depth grid to an EPSG:4326 COG.

    The grid is in the deck's projected-metres CRS (``BuildResult.crs``) with the
    builder's affine (``BuildResult.transform``; row 0 = north, col 0 = west, the
    standard COG orientation). Thin shim over ``cog_io.write_cog_4326_from_grid``
    (STEP 1 dedupe): stage a source GTiff in the grid CRS, warp to EPSG:4326
    (``Resampling.nearest`` so the NaN dry-mask is preserved without smearing),
    then run the CRS round-trip guard. Byte-identical to the pre-dedupe writer.
    """
    from rasterio.warp import Resampling

    try:
        return cog_io.write_cog_4326_from_grid(
            grid,
            src_crs=grid_crs,
            src_transform=grid_transform,
            reproject=True,
            resampling=Resampling.nearest,
            crs_roundtrip_guard=True,
            src_suffix="_swmm_src.tif",
            dst_suffix="_swmm_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc, grid_crs=grid_crs) from exc


def _safe_unlink(p: Path) -> None:
    cog_io.safe_unlink(p)


def _cog_bbox_4326(cog_path: Path) -> tuple[float, float, float, float] | None:
    """Return the COG's ``(min_lon, min_lat, max_lon, max_lat)`` for zoom-to."""
    return cog_io.cog_bbox_4326(cog_path)


# --------------------------------------------------------------------------- #
# Upload (scheme-aware: s3 via boto3 / gs via fsspec) — mirrors postprocess_flood.
# --------------------------------------------------------------------------- #
def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str = "swmm_depth_peak.tif",
) -> str:
    """Upload the staged COG to ``{scheme}://<runs_bucket>/<run_id>/<dest_filename>``.

    Thin shim over ``cog_io.upload_cog`` (STEP 1 dedupe; byte-identical):
    scheme-aware via ``cache.storage_scheme()`` - ``s3`` via boto3
    (``ContentType=image/tiff``, no GCP-named default), ``gs`` via fsspec (default
    bucket ``RUNS_BUCKET_DEFAULT``, RAISES on failure - no silent file:// on the
    cloud path). Per-frame callers pass a DISTINCT ``dest_filename`` so each frame
    lands at its own object key (its own TiTiler url / identity key -> no dedup).
    """
    try:
        return cog_io.upload_cog(
            local_cog,
            run_id,
            runs_bucket,
            dest_filename=dest_filename,
            content_type="image/tiff",
            gs_backend="fsspec",
            gs_fallback_to_file=False,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="SWMM depth COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_swmm(
    run: Any,
    build: Any,
    *,
    run_id: str,
    runs_bucket: str | None = None,
    building_footprints: Any = None,
) -> tuple[list[SWMMDepthLayerURI], dict[str, Any]]:
    """Rasterize a solved SWMM run into a peak + per-frame depth-COG layer set.

    Reads the per-timestep node ``INVERT_DEPTH`` from ``run.out_path`` (the
    pyswmm ``Output`` binary API), scatters each storage node's depth onto the
    mesh-cell grid the deck was built from (``build.grid_shape``; dropped/building
    cells + sub-threshold cells -> NaN), writes the PEAK + up to
    ``MAX_FLOOD_FRAMES`` per-timestep depth COGs (reprojected to EPSG:4326),
    uploads them, and returns the EXACT ``(layers, metrics)`` shape
    ``postprocess_flood`` returns so the Phase-1 scrubber path consumes it
    unchanged.

    Args:
        run: a ``swmm_mesh_builder.RunResult`` (carries ``out_path`` +
            ``continuity_error_pct``; the mass-balance honesty gate already fired
            in ``run_swmm_deck`` before this is called).
        build: the ``swmm_mesh_builder.BuildResult`` (carries ``grid_shape`` /
            ``crs`` / ``transform`` / ``resolution_m`` / ``n_buildings_dropped`` /
            ``barriers_geojson`` — the scatter + georegistration provenance).
        run_id: the run identifier the COGs are keyed under in the runs bucket.
        runs_bucket: optional override for the runs bucket name.
        building_footprints: optional GeoJSON FeatureCollection / shapely list of
            building footprints; when supplied (with the grid georegistration)
            ``n_buildings_affected`` counts footprints touched by a wet cell.

    Returns:
        ``(layers, metrics)``:

        - ``layers[0]`` = the PEAK ``SWMMDepthLayerURI`` (role ``"primary"``,
          name ``"Peak flood depth"``, style ``continuous_flood_depth``) carrying
          ``max_depth_m`` / ``flooded_area_km2`` / ``n_buildings_affected`` + the
          echoed barrier geometry.
        - ``layers[1:]`` = up to ``MAX_FLOOD_FRAMES`` per-frame
          ``SWMMDepthLayerURI`` (role ``"context"``, names ``"Flood depth step
          N"``, distinct runs-bucket keys). Present only when the run has > 1
          reporting timestep (else just the peak).
        - ``metrics`` = the peak aggregates dict (``max_depth_m`` /
          ``mean_depth_m`` / ``p95_depth_m`` / ``flooded_cell_count`` /
          ``flooded_area_km2`` / ``n_buildings_affected`` / ``crs``) the workflow
          surfaces.

    Raises:
        PostprocessSWMMError: any read / scatter / COG-write / reproject / upload
            step failed; ``error_code`` identifies the stage.
    """
    out_path = str(getattr(run, "out_path"))
    grid_shape = tuple(getattr(build, "grid_shape"))
    grid_crs = str(getattr(build, "crs"))
    resolution_m = float(getattr(build, "resolution_m"))
    barriers = getattr(build, "barriers_geojson", None)
    n_buildings_dropped = int(getattr(build, "n_buildings_dropped", 0) or 0)

    grid_transform = _affine_from_build(build)

    # --- read every reporting step's scattered node-depth grid ---
    grids, n_steps = _read_node_depth_snapshots(out_path, grid_shape)

    # --- PEAK grid (max-total-depth step) + the narration scalars ---
    peak_grid = _peak_grid_from_snapshots(grids)
    metrics = compute_swmm_depth_metrics(
        peak_grid,
        resolution_m=resolution_m,
        building_footprints=building_footprints,
        grid_crs=grid_crs,
        grid_transform=grid_transform,
    )
    metrics["crs"] = "EPSG:4326"
    # If no footprints were supplied for the metric but the build dropped some,
    # report the dropped count as a conservative lower bound (HONEST: those cells
    # are definitively obstructions; never invent a higher number).
    if building_footprints is None and n_buildings_dropped > 0:
        metrics["n_buildings_affected"] = max(
            int(metrics["n_buildings_affected"]), 0
        )

    logger.info(
        "postprocess_swmm run_id=%s n_steps=%d max_depth_m=%.4g "
        "flooded_area_km2=%.6g n_buildings_affected=%d",
        run_id,
        n_steps,
        metrics["max_depth_m"],
        metrics["flooded_area_km2"],
        metrics["n_buildings_affected"],
    )

    # --- PEAK layer (always layers[0]) ---
    peak_cog = _write_depth_cog_4326(
        peak_grid, grid_crs=grid_crs, grid_transform=grid_transform
    )
    peak_bbox = _cog_bbox_4326(peak_cog)
    try:
        peak_uri = _upload_cog_to_runs_bucket(
            peak_cog, run_id, runs_bucket, dest_filename="swmm_depth_peak.tif"
        )
    finally:
        _safe_unlink(peak_cog)

    layers: list[SWMMDepthLayerURI] = [
        SWMMDepthLayerURI(
            layer_id=f"swmm-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=peak_uri,
            style_preset=FLOOD_DEPTH_STYLE_PRESET,
            role="primary",
            units="meters",
            bbox=peak_bbox,
            max_depth_m=float(metrics["max_depth_m"]),
            flooded_area_km2=float(metrics["flooded_area_km2"]),
            n_buildings_affected=int(metrics["n_buildings_affected"]),
            barriers=barriers,
        )
    ]

    # --- per-frame layers (engine-agnostic flood animation, Phase 1) ---
    # Only when the run has > 1 reporting step; a 1-frame group never forms on
    # the web (needs >= 2 distinct members) so we emit just the peak otherwise.
    if n_steps > 1:
        frame_indices = _select_frame_time_indices(n_steps)
        frame_layers = _emit_frame_layers(
            grids,
            frame_indices,
            run_id=run_id,
            runs_bucket=runs_bucket,
            grid_crs=grid_crs,
            grid_transform=grid_transform,
            resolution_m=resolution_m,
            barriers=barriers,
        )
        # A lone styled frame can never group — drop a <2 frame set.
        if len(frame_layers) >= 2:
            layers.extend(frame_layers)
        else:
            logger.info(
                "postprocess_swmm: < 2 frame layers (%d) — emitting peak only "
                "(no animation group) for run_id=%s",
                len(frame_layers),
                run_id,
            )

    if len(layers) > 1:
        logger.info(
            "postprocess_swmm: emitted peak layer + %d time-step frames "
            "(animation group) for run_id=%s",
            len(layers) - 1,
            run_id,
        )
    return layers, metrics


def _emit_frame_layers(
    grids: list[Any],
    frame_indices: list[int],
    *,
    run_id: str,
    runs_bucket: str | None,
    grid_crs: str,
    grid_transform: Any,
    resolution_m: float,
    barriers: dict | None,
) -> list[SWMMDepthLayerURI]:
    """Write + upload the per-frame depth COGs as contiguous ``step N`` layers.

    A single corrupt frame must NOT sink the whole animation OR the peak layer:
    on a frame write/upload failure we clean up the partial frames and return
    ``[]`` (the caller degrades to peak-only) — better one good layer than a
    broken group (the honesty stance from postprocess_flood).
    """
    frame_layers: list[SWMMDepthLayerURI] = []
    written_cogs: list[Path] = []
    try:
        for frame_no, t_idx in enumerate(frame_indices, start=1):
            grid_t = grids[t_idx]
            frame_cog = _write_depth_cog_4326(
                grid_t, grid_crs=grid_crs, grid_transform=grid_transform
            )
            written_cogs.append(frame_cog)
            frame_bbox = _cog_bbox_4326(frame_cog)
            frame_metrics = compute_swmm_depth_metrics(
                grid_t, resolution_m=resolution_m
            )
            frame_uri = _upload_cog_to_runs_bucket(
                frame_cog,
                run_id,
                runs_bucket,
                dest_filename=f"swmm_depth_frame_{frame_no:02d}.tif",
            )
            _safe_unlink(frame_cog)
            written_cogs.pop()  # uploaded + unlinked
            frame_layers.append(
                SWMMDepthLayerURI(
                    layer_id=f"swmm-depth-frame-{frame_no:02d}-{run_id}",
                    name=f"Flood depth step {frame_no}",
                    layer_type="raster",
                    uri=frame_uri,
                    style_preset=FLOOD_DEPTH_STYLE_PRESET,
                    role="context",
                    units="meters",
                    bbox=frame_bbox,
                    max_depth_m=float(frame_metrics["max_depth_m"]),
                    flooded_area_km2=float(frame_metrics["flooded_area_km2"]),
                    n_buildings_affected=int(frame_metrics["n_buildings_affected"]),
                    barriers=barriers,
                )
            )
    except PostprocessSWMMError as exc:
        logger.warning(
            "postprocess_swmm: a frame COG write/upload failed (%s); degrading to "
            "peak-only (no animation group).",
            exc,
        )
        for p in written_cogs:
            _safe_unlink(p)
        return []
    return frame_layers


def _affine_from_build(build: Any) -> Any:
    """Reconstruct the rasterio ``Affine`` from ``BuildResult.transform`` (6-tuple).

    ``BuildResult.transform`` is ``list(grid.transform)[:6]`` = ``(a, b, c, d, e,
    f)`` (rasterio's row-major affine coefficients). Rebuild it as an
    ``Affine`` for the COG write.
    """
    from rasterio import Affine

    t = list(getattr(build, "transform"))
    if len(t) < 6:
        raise PostprocessSWMMError(
            "SWMM_COG_WRITE_FAILED",
            message=f"BuildResult.transform has {len(t)} coeffs; expected >= 6",
            details={"transform": t},
        )
    return Affine(t[0], t[1], t[2], t[3], t[4], t[5])


# --------------------------------------------------------------------------- #
# levers STEP 3 -- NEW published quantities (FLOODING_LOSSES / PONDED_VOLUME /
# conduit FLOW_RATE / conduit FLOW_VELOCITY).
#
# The EXISTING per-node INVERT_DEPTH peak + frames stay on the byte-identical
# ``postprocess_swmm`` path above. The Output binary API is ALREADY open there;
# this ADDS the other node/link attributes the Output API exposes (the audit's
# item 4) as additive per-cell PEAK rasters via the shared executor.
# --------------------------------------------------------------------------- #
#: token -> (OutputQuantitySpec.quantity_id, Output-API attr name, scope, signed)
#: scope = "node" | "link"; signed selects the diverging vs magnitude scatter.
_SWMM_NEW_QUANTITIES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("swmm-flooding-losses", "FLOODING_LOSSES", "node", "Node flooding rate", False),
    ("swmm-ponded-volume", "PONDED_VOLUME", "node", "Ponded volume", False),
    ("swmm-conduit-flow", "FLOW_RATE", "link", "Conduit flow", True),
    ("swmm-conduit-velocity", "FLOW_VELOCITY", "link", "Conduit velocity", False),
)


def _read_swmm_attr_peak_grid(
    out_path: str,
    grid_shape: tuple[int, int],
    *,
    attr_name: str,
    scope: str,
    signed: bool,
) -> Any:
    """Read every reporting step's node/link attr -> the PEAK-magnitude grid.

    Uses the pyswmm ``Output`` binary API: for a NODE attr,
    ``out.node_attribute(NodeAttribute.<attr>, t)`` -> ``{node: value}``; for a
    LINK attr, ``out.link_attribute(LinkAttribute.<attr>, t)`` -> ``{link:
    value}``. We scatter each step (node-> own cell, link-> downstream cell) and
    keep the per-cell PEAK MAGNITUDE across steps. Raises a typed
    ``PostprocessSWMMError`` on a missing dep / read failure. Returns the peak
    ``(H, W)`` grid (NaN where no flow).
    """
    try:
        import numpy as np
        from pyswmm import Output
        from swmm.toolkit.shared_enum import LinkAttribute, NodeAttribute
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"pyswmm / swmm.toolkit unavailable for .out attr read: {exc}",
            details={"out_path": out_path, "attr": attr_name},
        ) from exc

    if not Path(out_path).exists():
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"SWMM .out not found at {out_path}",
            details={"out_path": out_path},
        )

    peak: Any = None
    try:
        with Output(out_path) as out:
            n_steps = len(out.times)
            if n_steps <= 0:
                raise PostprocessSWMMError(
                    "SWMM_OUTPUT_EMPTY",
                    message="SWMM .out carries no reporting timesteps",
                    details={"out_path": out_path},
                )
            for t in range(n_steps):
                if scope == "node":
                    enum_val = getattr(NodeAttribute, attr_name)
                    by_id = out.node_attribute(enum_val, t)
                    grid = scatter_node_attr_to_grid(by_id, grid_shape, signed=signed)
                else:
                    enum_val = getattr(LinkAttribute, attr_name)
                    by_id = out.link_attribute(enum_val, t)
                    grid = scatter_link_attr_to_grid(by_id, grid_shape, signed=signed)
                if peak is None:
                    peak = grid
                else:
                    # keep the per-cell PEAK MAGNITUDE (NaN-safe).
                    take = (~np.isnan(grid)) & (
                        np.isnan(peak) | (np.abs(grid) > np.abs(peak))
                    )
                    peak = np.where(take, grid, peak)
    except PostprocessSWMMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"could not read {attr_name} from {out_path}: {exc}",
            details={"out_path": out_path, "attr": attr_name},
        ) from exc
    # ``np`` is bound from the try-body import above (we only reach here when it
    # succeeded; the except clauses re-raise).
    if peak is None:
        peak = np.full(
            (int(grid_shape[0]), int(grid_shape[1])), np.nan, dtype="float64"
        )
    return peak


def publish_swmm_quantities(
    run: Any,
    build: Any,
    *,
    run_id: str,
    register_manifest_layers: Any,
    runs_bucket: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Any:
    """Publish the NEW SWMM quantities (flooding / ponded / conduit flow+vel).

    Reads each attribute's PEAK-magnitude grid from the same solved ``.out`` the
    depth path used, builds registry readers, and routes them through the shared
    executor (ONE registrar). Returns the executor result. A quantity whose grid
    is entirely empty (all NaN) is still emitted as a (blank) layer only if it
    has any finite cell; otherwise the executor's reader returns an empty grid
    and the layer simply renders nothing -- never raises.
    """
    from dataclasses import replace as _dc_replace

    from grace2_contracts.output_quantities import (
        RasterField,
        get_output_registry,
    )

    from . import publish_quantities as _pq

    out_path = str(getattr(run, "out_path"))
    grid_shape = tuple(getattr(build, "grid_shape"))
    grid_crs = str(getattr(build, "crs"))
    grid_transform = _affine_from_build(build)
    resolution_m = float(getattr(build, "resolution_m"))

    by_qid = {
        qid: (attr, scope, signed)
        for (qid, attr, scope, _label, signed) in _SWMM_NEW_QUANTITIES
    }

    def _make_reader(qid: str):
        attr, scope, signed = by_qid[qid]

        def _reader(_ctx: Any) -> RasterField:
            import numpy as np

            grid = _read_swmm_attr_peak_grid(
                out_path, grid_shape, attr_name=attr, scope=scope, signed=signed
            )
            finite = grid[np.isfinite(grid)]
            mx = float(np.nanmax(np.abs(finite))) if finite.size else 0.0
            return RasterField(
                grid=grid,
                src_crs=grid_crs,
                src_transform=grid_transform,
                reproject=True,
                crs_roundtrip_guard=True,
                metrics={f"{qid}_peak": mx},
            )

        return _reader

    specs = [
        _dc_replace(spec, reader=_make_reader(spec.quantity_id))
        for spec in get_output_registry("swmm")
        if spec.quantity_id in by_qid
    ]

    def _upload(cog: Path, rid: str, _bucket: Any = None, *, dest_filename: str) -> str:
        return _upload_cog_to_runs_bucket(cog, rid, runs_bucket, dest_filename=dest_filename)

    return _pq.publish_quantities(
        "swmm",
        run_id=run_id,
        upload=_upload,
        register_manifest_layers=register_manifest_layers,
        specs=specs,
        bbox=bbox,
    )


# --------------------------------------------------------------------------- #
# Water-quality postprocess (sprint-WQ) — the outfall pollutograph + cumulative
# outfall LOAD + per-cell peak washoff-CONCENTRATION COG + the WQ mass-balance
# readout, from the SAME solved ``.out`` / ``.rpt`` the depth path already read.
#
# Result reads PINNED by the Phase-1 in-image smoke (see the scoping report):
#   - ``out.pollutants`` -> {name: index}; pollutant k's node concentration is
#     read via a NodeAttribute member of value 6 + k. The toolkit enum only
#     DEFINES POLLUT_CONC_0=6, and a PLAIN int 6+k is marshaled WRONG by the SWIG
#     layer, so we EXTEND the aenum with POLLUT_CONC_<k> members (aenum.extend_
#     enum) and pass the member — the pinned, verified read path.
#   - the ``.rpt`` Outfall Loading Summary carries the cumulative load per
#     pollutant per outfall; a MG/L pollutant reports ``kg``, a ``#/L`` COUNT
#     pollutant reports ``LogN`` (log10 of the raw count) — we convert 10^value ->
#     counts and label it honestly (never mislabel a count as kg).
# --------------------------------------------------------------------------- #
#: The builder's single dedicated boundary outfall node name.
_OUTFALL_NODE = "OUT"


def _pollut_node_attr(k: int) -> Any:
    """Return the ``NodeAttribute`` member for pollutant index ``k`` (value 6+k).

    The toolkit aenum defines only ``POLLUT_CONC_0`` (=6); we extend it with
    ``POLLUT_CONC_<k>`` on first use so the SWIG binding marshals the enum
    correctly (a plain int 6+k is read WRONG — pinned in the Phase-1 smoke).
    """
    import aenum
    from swmm.toolkit.shared_enum import NodeAttribute

    name = f"POLLUT_CONC_{int(k)}"
    if not hasattr(NodeAttribute, name):
        aenum.extend_enum(NodeAttribute, name, 6 + int(k))
    return getattr(NodeAttribute, name)


def _is_count_unit(unit: str) -> bool:
    """True for a COUNT concentration unit (``#/L``) vs a MASS unit (MG/L, UG/L)."""
    return "#" in str(unit)


def _load_units_for(unit: str) -> str:
    """The outfall-LOAD unit label derived from the concentration unit: a count
    pollutant's load is a raw count (converted from the .rpt LogN); a mass
    pollutant's load is kg (SI, because the deck is CMS)."""
    return "counts" if _is_count_unit(unit) else "kg"


def read_outfall_loading(
    rpt_path: str, n_pollutants: int, *, outfall_node: str = _OUTFALL_NODE
) -> list[float] | None:
    """Parse the RAW per-pollutant load column from the ``.rpt`` Outfall Loading
    Summary for one outfall node.

    Layout PINNED by the Phase-1 smoke: a data row is
    ``<node> <FlowFreqPcnt> <AvgFlow> <MaxFlow> <TotalVolume> <load_p0> <load_p1>
    ...`` — four fixed numeric columns then one load column per pollutant in
    ``[POLLUTANTS]`` order. Returns the ``n_pollutants`` RAW load values (kg for a
    mass pollutant; LOG10-count for a ``#/L`` pollutant — the caller converts and
    labels). Falls back to the ``System`` aggregate row when the named outfall row
    is absent, and to ``None`` when the block / row cannot be parsed (the caller
    then degrades to an honest zero / integral, never a fabricated load).
    """
    import re

    try:
        lines = Path(rpt_path).read_text().splitlines()
    except Exception:
        return None
    in_block = False
    system_floats: list[float] | None = None
    for line in lines:
        if "Outfall Loading Summary" in line:
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        floats = [float(x) for x in re.findall(r"-?\d+\.\d+", stripped)]
        if first == outfall_node and len(floats) >= 4 + n_pollutants:
            return floats[4 : 4 + n_pollutants]
        if first == "System" and len(floats) >= 4 + n_pollutants:
            system_floats = floats[4 : 4 + n_pollutants]
    return system_floats


def read_runoff_quality_built_washed(
    rpt_path: str, pollutant_index: int, *, is_count: bool
) -> tuple[float, float] | None:
    """Parse (total BUILT mass, WASHED-off mass) for one pollutant from the
    ``.rpt`` Runoff Quality Continuity block.

    built = Initial Buildup + Surface Buildup; washed = Surface Runoff (all at the
    pollutant's column, in ``[POLLUTANTS]`` order). For a COUNT pollutant the .rpt
    reports these as LOG10 values, so we convert 10^value -> counts before
    combining. Returns ``(built, washed)`` or ``None`` when the block / column is
    absent. Used for the supply-limited ``washoff_mass_fraction`` = washed / built.
    """
    import re

    try:
        lines = Path(rpt_path).read_text().splitlines()
    except Exception:
        return None

    def _col(line: str) -> float | None:
        nums = re.findall(r"-?\d+\.\d+", line)
        if 0 <= pollutant_index < len(nums):
            v = float(nums[pollutant_index])
            return (10.0 ** v) if is_count else v
        return None

    in_block = False
    initial = surface = washed = None
    for line in lines:
        if "Runoff Quality Continuity" in line:
            in_block = True
            continue
        if not in_block:
            continue
        if "Quality Routing Continuity" in line:  # next block — stop.
            break
        if "Initial Buildup" in line:
            initial = _col(line)
        elif "Surface Buildup" in line:
            surface = _col(line)
        elif "Surface Runoff" in line:
            washed = _col(line)
    if washed is None or (initial is None and surface is None):
        return None
    built = (initial or 0.0) + (surface or 0.0)
    return built, washed


def postprocess_swmm_pollutants(
    run: Any,
    build: Any,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> tuple[list[Any], dict[str, list[tuple[float, float]]], dict[str, dict[str, Any]]]:
    """Read the WQ results off a solved SWMM run into layers + series + metrics.

    Reuses the ALREADY-proven Output binary reader + the ``scatter_node_attr_to_
    grid`` + COG path (do NOT reinvent). For each authored pollutant (in
    ``build.pollutants`` order):

      - the OUTFALL POLLUTOGRAPH: ``out.node_series("OUT", POLLUT_CONC_<k>)`` ->
        the concentration-vs-time series (native units), downsampled minutes.
      - the CUMULATIVE OUTFALL LOAD: parsed from the ``.rpt`` Outfall Loading
        Summary (kg for MG/L; 10^LogN -> counts for ``#/L``).
      - the per-cell PEAK washoff-CONCENTRATION COG: scatter the peak-load
        timestep's node concentration onto the mesh grid and write/upload a COG
        (``swmm_conc_<name>.tif``) through the existing depth-COG machinery.
      - the WQ MASS-BALANCE readout: the Quality Routing Continuity error (%) +
        the supply-limited ``washoff_mass_fraction`` (washed <= built).

    Returns ``(pollutant_layers, pollutograph_series, wq_metrics)``:
      - ``pollutant_layers``: one ``SWMMPollutantLayerURI`` per pollutant (role
        ``"context"``; depth stays the primary).
      - ``pollutograph_series``: ``{name: [(minute, conc), ...]}`` for the chart.
      - ``wq_metrics``: ``{name: {outfall_load, outfall_load_units,
        peak_outfall_conc, washoff_mass_fraction, wq_continuity_error_pct,
        pollutant_units}}``.

    Best-effort per the design: WQ is additive context, so a pollutant that
    cannot be read is skipped (never sinks the others or the depth headline).
    """
    from datetime import datetime as _dt

    import numpy as np

    from grace2_contracts.swmm_contracts import SWMMPollutantLayerURI

    from .swmm_mesh_builder import read_quality_routing_continuity

    out_path = str(getattr(run, "out_path"))
    rpt_path = str(getattr(run, "rpt_path", "")) or str(Path(out_path).with_suffix(".rpt"))
    grid_shape = tuple(getattr(build, "grid_shape"))
    grid_crs = str(getattr(build, "crs"))
    grid_transform = _affine_from_build(build)
    pollutants: list[tuple[str, str]] = list(getattr(build, "pollutants", []) or [])

    if not pollutants:
        return [], {}, {}

    try:
        from pyswmm import Output
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"pyswmm unavailable for WQ .out read: {exc}",
            details={"out_path": out_path},
        ) from exc

    if not Path(out_path).exists():
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"SWMM .out not found at {out_path}",
            details={"out_path": out_path},
        )

    # RAW outfall loads (one per pollutant, in deck order); None -> degrade.
    raw_loads = read_outfall_loading(rpt_path, len(pollutants))

    layers: list[Any] = []
    series: dict[str, list[tuple[float, float]]] = {}
    metrics: dict[str, dict[str, Any]] = {}

    try:
        with Output(out_path) as out:
            name_to_idx = dict(out.pollutants or {})
            times = list(out.times)
            t0 = times[0] if times else None
            for order_idx, (name, unit) in enumerate(pollutants):
                # Key by NAME (the authoritative out.pollutants map) — never by
                # assumed position (guards a composer/deck ordering divergence).
                k = int(name_to_idx.get(name, order_idx))
                attr = _pollut_node_attr(k)
                is_count = _is_count_unit(unit)

                # --- pollutograph (outfall conc vs minutes) ---
                node_ser = out.node_series(_OUTFALL_NODE, attr)  # {datetime: conc}
                rows: list[tuple[float, float]] = []
                peak_conc = 0.0
                peak_time: _dt | None = None
                for tstamp, conc in node_ser.items():
                    mins = (
                        (tstamp - t0).total_seconds() / 60.0 if t0 is not None else 0.0
                    )
                    cval = float(conc)
                    rows.append((mins, cval))
                    if cval > peak_conc:
                        peak_conc = cval
                        peak_time = tstamp
                series[name] = rows

                # --- per-cell PEAK-load-timestep concentration grid -> COG ---
                peak_t_idx = times.index(peak_time) if peak_time in times else (
                    max(range(len(times)), key=lambda t: rows[t][1]) if rows else 0
                )
                conc_by_node = out.node_attribute(attr, peak_t_idx)
                grid = scatter_node_attr_to_grid(
                    conc_by_node, grid_shape, signed=False
                )

                # --- cumulative outfall load (kg or converted counts) ---
                raw_load = (
                    raw_loads[order_idx]
                    if raw_loads is not None and order_idx < len(raw_loads)
                    else None
                )
                if raw_load is None:
                    outfall_load = 0.0
                elif is_count:
                    outfall_load = 10.0 ** float(raw_load)  # LogN -> raw count
                else:
                    outfall_load = float(raw_load)
                load_units = _load_units_for(unit)

                # --- supply-limited washoff fraction (washed / built) ---
                bw = read_runoff_quality_built_washed(
                    rpt_path, order_idx, is_count=is_count
                )
                washoff_frac: float | None = None
                if bw is not None:
                    built, washed = bw
                    if built > 0:
                        washoff_frac = max(0.0, min(1.0, washed / built))

                # --- WQ mass-balance readout ---
                wq_cont = read_quality_routing_continuity(rpt_path, order_idx)

                pollutant_units = "#/L" if is_count else "mg/L"
                metrics[name] = {
                    "outfall_load": float(outfall_load),
                    "outfall_load_units": load_units,
                    "peak_outfall_conc": float(peak_conc),
                    "washoff_mass_fraction": washoff_frac,
                    "wq_continuity_error_pct": wq_cont,
                    "pollutant_units": pollutant_units,
                }

                # --- write + upload the concentration COG (best-effort) ---
                safe = _safe_pollutant_slug(name)
                try:
                    conc_cog = _write_depth_cog_4326(
                        grid, grid_crs=grid_crs, grid_transform=grid_transform
                    )
                    conc_bbox = _cog_bbox_4326(conc_cog)
                    try:
                        conc_uri = _upload_cog_to_runs_bucket(
                            conc_cog, run_id, runs_bucket,
                            dest_filename=f"swmm_conc_{safe}.tif",
                        )
                    finally:
                        _safe_unlink(conc_cog)
                except PostprocessSWMMError as exc:
                    logger.warning(
                        "postprocess_swmm_pollutants: conc COG for %s failed "
                        "(%s); emitting metrics without a raster.", name, exc,
                    )
                    continue

                layers.append(
                    SWMMPollutantLayerURI(
                        layer_id=f"swmm-conc-{safe}-{run_id}",
                        name=f"Peak {name} concentration",
                        layer_type="raster",
                        uri=conc_uri,
                        style_preset=CONCENTRATION_STYLE_PRESET,
                        role="context",
                        units=pollutant_units,
                        bbox=conc_bbox,
                        pollutant_name=name,
                        pollutant_units=pollutant_units,
                        outfall_load=float(outfall_load),
                        outfall_load_units=load_units,
                        peak_outfall_conc=float(peak_conc),
                        washoff_mass_fraction=washoff_frac,
                        wq_continuity_error_pct=wq_cont,
                    )
                )
    except PostprocessSWMMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSWMMError(
            "SWMM_OUTPUT_READ_FAILED",
            message=f"could not read WQ results from {out_path}: {exc}",
            details={"out_path": out_path},
        ) from exc

    logger.info(
        "postprocess_swmm_pollutants run_id=%s pollutants=%s loads=%s",
        run_id,
        [n for n, _ in pollutants],
        {n: round(m["outfall_load"], 4) for n, m in metrics.items()},
    )
    return layers, series, metrics


def _safe_pollutant_slug(name: str) -> str:
    """Filesystem/URL-safe slug for a pollutant name (e.g. ``E_coli`` -> ``e_coli``)."""
    return "".join(c.lower() if c.isalnum() else "_" for c in str(name)).strip("_") or "pollutant"
