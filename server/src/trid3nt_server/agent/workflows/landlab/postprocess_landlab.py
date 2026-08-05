"""Landlab run-output postprocessing (NEW engine).

``postprocess_landlab(field_cog_path, *, run_id, analysis, result, ...) ->
(layers, metrics)`` takes the worker-produced field COG (the LandslideProbability
``probability_of_failure`` field, or the OverlandFlow peak ``surface_water__depth``
field — a single-band GeoTIFF in the grid's projected-metres CRS), reprojects it
to EPSG:4326 with the CRS round-trip guard (the TiTiler-wedge / mistagged-raster
guard, identical to ``postprocess_swmm._write_depth_cog_4326`` /
``postprocess_modflow._write_reprojected_cog``), uploads it to the runs bucket,
and emits a :class:`~trid3nt_contracts.landlab_contracts.LandlabSusceptibilityLayerURI`
carrying the typed narration scalars.

Reuse (do NOT reinvent): the COG reproject-to-4326 + CRS round-trip guard pattern
from ``postprocess_swmm`` (the MapLibre basemap is EPSG:4326/web-mercator, so the
metric-CRS worker field must be warped). The honesty floor (Invariant 1 /
FR-AS-7): the narration scalars are the worker's deterministically-computed
``result`` block (unstable-area fraction / min FoS / mean PoF) — no LLM anywhere;
the agent narrates the typed fields, never invents them. The scalars are
recomputed from the field as a fallback when the worker result block is absent
(e.g. an older completion schema), so a missing result never produces invented
numbers.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware
via ``cache.storage_scheme()``); the agent does not re-render — ``publish_layer``
/ TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabDemConditioningLayerURI,
    LandlabFlowAccumulationLayerURI,
    LandlabGreenAmptLayerURI,
    LandlabHacksLawLayerURI,
    LandlabHandLayerURI,
    LandlabLakeMappingLayerURI,
    LandlabOverlandTimeseriesLayerURI,
    LandlabStormEnsembleLayerURI,
    LandlabSusceptibilityLayerURI,
)

from trid3nt_server.agent.workflows.shared import cog_io
from trid3nt_server.agent.workflows.shared.cog_io import CogIoError

__all__ = [
    "PostprocessLandlabError",
    "postprocess_landlab",
    "postprocess_landlab_flow_accumulation",
    "postprocess_landlab_green_ampt",
    "postprocess_landlab_storm_ensemble",
    "postprocess_landlab_overland_timeseries",
    "postprocess_landlab_dem_conditioning",
    "postprocess_landlab_lake_mapping",
    "postprocess_landlab_hacks_law",
    "postprocess_landlab_hand",
    "build_routing_comparison_chart_spec",
    "build_infiltration_partition_chart_spec",
    "build_storm_ensemble_chart_spec",
    "build_overland_hydrograph_chart_spec",
    "build_hacks_law_chart_spec",
    "publish_landlab_quantities",
    "compute_landlab_metrics",
    "LANDSLIDE_STYLE_PRESET",
    "OVERLAND_STYLE_PRESET",
    "DRAINAGE_AREA_STYLE_PRESET",
    "INFILTRATION_STYLE_PRESET",
    "RUNOFF_STYLE_PRESET",
    "FILL_DEPTH_STYLE_PRESET",
    "LAKE_DEPTH_STYLE_PRESET",
    "HAND_STYLE_PRESET",
    "UNSTABLE_PROBABILITY_THRESHOLD",
    "SECONDARY_QUANTITY_BY_TOKEN",
]

logger = logging.getLogger("trid3nt_server.agent.workflows.landlab.postprocess_landlab")

#: levers STEP 3: map the worker secondary-field TOKEN (the key in the
#: completion ``result.secondary_field_files`` map + the
#: ``landlab_secondary_<token>.tif`` filename) onto its OUTPUT_QUANTITIES
#: ``quantity_id``. The agent publishes only the tokens present in this map.
SECONDARY_QUANTITY_BY_TOKEN: dict[str, str] = {
    "drainage_area": "landlab-drainage-area",
    "slope": "landlab-slope",
    "relative_wetness": "landlab-relative-wetness",
    "discharge": "landlab-discharge",
    "factor_of_safety": "landlab-factor-of-safety",
}

#: The TiTiler style preset key the orchestrator registers in
#: ``_TITILER_STYLE_REGISTRY`` (the shared-append snippet). Susceptibility =
#: probability of failure in [0, 1], rendered with a reversed red->green diverging
#: ramp (rdylgn_r) so HIGH susceptibility = RED, LOW = GREEN.
LANDSLIDE_STYLE_PRESET: str = "continuous_landslide_susceptibility"

#: The overland-flow chain reuses the existing flood-depth preset (a depth field,
#: same physical quantity as SFINCS/SWMM depth — additive reuse, no new preset).
OVERLAND_STYLE_PRESET: str = "continuous_flood_depth"

#: The flow-accumulation primary (drainage area, m^2) reuses the already-registered
#: project drainage-area preset (``continuous_drainage_area``, viridis). A dedicated
#: log-DOMAIN TiTiler expression (drainage area spans several orders of magnitude)
#: is a NAMED RESIDUAL -- the existing preset is the reused styling, not a new one.
DRAINAGE_AREA_STYLE_PRESET: str = "continuous_drainage_area"

#: The Green-Ampt infiltration-depth + runoff-depth rasters are both DEPTH fields
#: (m), so they reuse the existing flood-depth preset (``continuous_flood_depth``).
#: A dedicated infiltration (browns) / runoff ramp is a NAMED RESIDUAL -- the
#: existing depth preset is the reused styling, not a new one.
INFILTRATION_STYLE_PRESET: str = "continuous_flood_depth"
RUNOFF_STYLE_PRESET: str = "continuous_flood_depth"

#: The DEM fill-depth, lake-depth, and HAND rasters are all metric-DEPTH/elevation
#: fields (m), so they reuse the existing flood-depth preset. Dedicated fill /
#: lake-bathymetry / HAND ramps are NAMED RESIDUALS -- the existing depth preset
#: is the reused styling, not a new one.
FILL_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"
LAKE_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"
HAND_STYLE_PRESET: str = "continuous_flood_depth"

#: Mirror of the worker threshold for recomputing the unstable fraction when the
#: completion result block is absent (kept in sync with
#: ``services/workers/landlab/component_chain.UNSTABLE_PROBABILITY_THRESHOLD``).
UNSTABLE_PROBABILITY_THRESHOLD: float = 0.75

#: Wet-depth floor for the overland-flow unstable/wet fraction fallback (mirrors
#: the flood NODATA_DEPTH_M).
OVERLAND_WET_DEPTH_M: float = 0.05

#: Runs-bucket default (the gs:// fallback only; AWS uses TRID3NT_RUNS_BUCKET).
RUNS_BUCKET_DEFAULT: str = "trid3nt-runs"


class PostprocessLandlabError(RuntimeError):
    """Raised on read / reproject / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame. Codes used here:

    - ``LANDLAB_OUTPUT_READ_FAILED`` — the field COG is missing / unreadable.
    - ``LANDLAB_DEPENDENCY_MISSING`` — rasterio / numpy not importable.
    - ``LANDLAB_COG_REPROJECT_FAILED`` — the projected-metres -> 4326 warp failed.
    - ``LANDLAB_CRS_TAG_MISMATCH`` — the COG CRS tag did not round-trip (the
      TiTiler-wedge / mistagged-raster guard).
    - ``LANDLAB_COG_UPLOAD_FAILED`` — the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_LANDLAB_FAILED"

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
# Pure metric math (unit-testable on a synthetic field grid).
# --------------------------------------------------------------------------- #
def compute_landlab_metrics(field: Any, *, analysis: str) -> dict[str, Any]:
    """Compute the three narration scalars from the output field grid.

    Pure arithmetic over the masked field (NaN = inactive/no-data):

      - landslide chain: ``unstable_area_fraction`` = fraction of active cells
        with probability >= ``UNSTABLE_PROBABILITY_THRESHOLD``;
        ``mean_probability_of_failure`` = mean probability over active cells;
        ``min_factor_of_safety`` is NOT derivable from the probability field
        alone, so it is left at 0.0 here (the authoritative value comes from the
        worker's deterministic FoS field via the completion ``result`` block).
      - overland chain: ``unstable_area_fraction`` = wet-cell fraction
        (depth >= ``OVERLAND_WET_DEPTH_M``); ``min_factor_of_safety`` carries the
        PEAK depth (m); ``mean_probability_of_failure`` = 0.0.

    Used as the FALLBACK when the worker ``result`` block is absent (a missing
    result yields an HONEST recomputed value, never an invented number).
    """
    import numpy as np

    arr = np.asarray(field, dtype="float64")
    active = np.isfinite(arr)
    vals = arr[active]
    n_active = int(vals.size)

    if n_active == 0:
        return {
            "unstable_area_fraction": 0.0,
            "min_factor_of_safety": 0.0,
            "mean_probability_of_failure": 0.0,
            "active_cell_count": 0,
        }

    if analysis == "overland_flow":
        wet_frac = float(np.count_nonzero(vals >= OVERLAND_WET_DEPTH_M) / n_active)
        max_depth = float(np.max(vals))
        return {
            "unstable_area_fraction": wet_frac,
            "min_factor_of_safety": max_depth,  # peak depth (units disambiguate)
            "mean_probability_of_failure": 0.0,
            "active_cell_count": n_active,
        }

    # landslide_probability (default): the field IS probability of failure.
    unstable_frac = float(
        np.count_nonzero(vals >= UNSTABLE_PROBABILITY_THRESHOLD) / n_active
    )
    mean_pof = float(np.mean(vals))
    return {
        "unstable_area_fraction": unstable_frac,
        "min_factor_of_safety": 0.0,  # authoritative FoS comes from worker result
        "mean_probability_of_failure": mean_pof,
        "active_cell_count": n_active,
    }


def _resolve_scalars(
    field: Any,
    *,
    analysis: str,
    result: dict[str, Any] | None,
) -> dict[str, float]:
    """Prefer the worker's deterministic ``result`` block; fall back to recompute.

    The worker computed the scalars with the FULL component output (incl. the
    deterministic FoS field the probability raster does not carry), so its
    ``result`` block is authoritative. When it is absent / incomplete we recompute
    from the field (honest under-report, never invented). Returns the three
    contract scalars clamped to their valid ranges.
    """
    recomputed = compute_landlab_metrics(field, analysis=analysis)

    def _pick(key: str) -> float:
        if isinstance(result, dict) and result.get(key) is not None:
            try:
                return float(result[key])
            except (TypeError, ValueError):
                pass
        return float(recomputed[key])

    unstable = max(0.0, min(1.0, _pick("unstable_area_fraction")))
    min_fos = max(0.0, _pick("min_factor_of_safety"))
    mean_pof = max(0.0, min(1.0, _pick("mean_probability_of_failure")))
    return {
        "unstable_area_fraction": unstable,
        "min_factor_of_safety": min_fos,
        "mean_probability_of_failure": mean_pof,
    }


# --------------------------------------------------------------------------- #
# COG reproject (projected-metres field -> EPSG:4326) + CRS round-trip guard.
# --------------------------------------------------------------------------- #
#: stage -> (Landlab error_code) map (STEP 1 dedupe; byte-identical codes).
_LANDLAB_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "LANDLAB_DEPENDENCY_MISSING",
    "READ": "LANDLAB_OUTPUT_READ_FAILED",
    "WRITE": "LANDLAB_COG_REPROJECT_FAILED",
    "REPROJECT": "LANDLAB_COG_REPROJECT_FAILED",
    "CRS_MISMATCH": "LANDLAB_CRS_TAG_MISMATCH",
    "UPLOAD": "LANDLAB_COG_UPLOAD_FAILED",
}


def _reraise_cogio(exc: CogIoError) -> "PostprocessLandlabError":
    """Map a cog_io ``CogIoError`` onto the Landlab typed error (preserves codes)."""
    code = _LANDLAB_STAGE_CODES.get(exc.stage, "POSTPROCESS_LANDLAB_FAILED")
    return PostprocessLandlabError(code, message=exc.message, details=dict(exc.details))


def _reproject_field_cog_4326(src_cog: Path) -> tuple[Path, tuple[float, float, float, float] | None]:
    """Reproject a metric-CRS field COG to EPSG:4326 (the MapLibre basemap CRS).

    Thin shim over ``cog_io.reproject_cog_file_to_4326`` (STEP 1 dedupe): the
    SOURCE is the worker's on-disk field COG; warp to EPSG:4326
    (``Resampling.nearest`` preserves the NaN no-data without smearing) + run the
    CRS round-trip guard (which also supplies the zoom-to bbox). Byte-identical to
    the pre-dedupe reprojector. Returns ``(dst_cog_path, bbox_4326)``.
    """
    try:
        return cog_io.reproject_cog_file_to_4326(
            src_cog,
            crs_roundtrip_guard=True,
            dst_suffix="_landlab_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


def _safe_unlink(p: Path) -> None:
    cog_io.safe_unlink(p)


def _read_field_array(cog_path: Path) -> Any:
    """Read the field COG band 1 as a numpy array (NaN no-data preserved)."""
    try:
        import numpy as np
        import rasterio
    except Exception as exc:  # noqa: BLE001
        raise PostprocessLandlabError(
            "LANDLAB_DEPENDENCY_MISSING",
            message=f"rasterio/numpy unavailable for field read: {exc}",
        ) from exc
    if not cog_path.exists():
        raise PostprocessLandlabError(
            "LANDLAB_OUTPUT_READ_FAILED",
            message=f"Landlab field COG not found at {cog_path}",
            details={"cog_path": str(cog_path)},
        )
    with rasterio.open(cog_path) as ds:
        arr = ds.read(1).astype("float64")
        nodata = ds.nodata
    if nodata is not None and np.isfinite(nodata):
        arr = np.where(arr == nodata, np.nan, arr)
    return arr


# --------------------------------------------------------------------------- #
# Upload (scheme-aware: s3 via boto3 / gs via fsspec) — mirrors postprocess_swmm.
# --------------------------------------------------------------------------- #
def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str = "landlab_susceptibility.tif",
) -> str:
    """Upload the staged COG to ``{scheme}://<runs_bucket>/<run_id>/<dest_filename>``.

    Thin shim over ``cog_io.upload_cog`` (STEP 1 dedupe; byte-identical):
    scheme-aware via ``cache.storage_scheme()`` - ``s3`` via boto3
    (``ContentType=image/tiff``), ``gs`` via fsspec (default bucket
    ``RUNS_BUCKET_DEFAULT``, RAISES on failure).
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
            log_label="Landlab field COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_landlab(
    field_cog_path: str | Path,
    *,
    run_id: str,
    analysis: str = "landslide_probability",
    result: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LandlabSusceptibilityLayerURI], dict[str, Any]]:
    """Reproject a Landlab field COG to 4326 + emit a susceptibility layer.

    Reads the worker-produced field COG (probability of failure for the landslide
    chain; peak depth for the overland chain), reprojects it to EPSG:4326 (with
    the CRS round-trip guard), uploads it, and returns the ``(layers, metrics)``
    shape the composer consumes.

    Args:
        field_cog_path: the LOCAL on-disk path to the worker's field COG (the
            composer downloads it from the Batch output before calling this).
        run_id: the run identifier the output COG is keyed under.
        analysis: the component chain that produced the field ("landslide_
            probability" | "overland_flow") — selects the style preset + the
            metric interpretation.
        result: the worker's deterministic ``result`` block from completion.json
            (the authoritative narration scalars); recomputed from the field when
            absent.
        runs_bucket: optional override for the runs bucket name.

    Returns:
        ``(layers, metrics)``:
        - ``layers[0]`` = the susceptibility ``LandlabSusceptibilityLayerURI``
          (role ``"primary"``) carrying the three narration scalars; style preset
          is ``continuous_landslide_susceptibility`` (landslide) or
          ``continuous_flood_depth`` (overland).
        - ``metrics`` = the scalar dict + ``crs`` + ``analysis``.

    Raises:
        PostprocessLandlabError: any read / reproject / upload step failed;
            ``error_code`` identifies the stage.
    """
    src = Path(field_cog_path)

    field = _read_field_array(src)
    scalars = _resolve_scalars(field, analysis=analysis, result=result)

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_susceptibility.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    is_landslide = analysis != "overland_flow"
    style = LANDSLIDE_STYLE_PRESET if is_landslide else OVERLAND_STYLE_PRESET
    if is_landslide:
        name = "Landslide susceptibility"
        units = "probability"
    else:
        name = "Peak overland depth"
        units = "meters"

    layer = LandlabSusceptibilityLayerURI(
        layer_id=f"landlab-susceptibility-{run_id}",
        name=name,
        layer_type="raster",
        uri=uri,
        style_preset=style,
        role="primary",
        units=units,
        bbox=bbox,
        unstable_area_fraction=float(scalars["unstable_area_fraction"]),
        min_factor_of_safety=float(scalars["min_factor_of_safety"]),
        mean_probability_of_failure=float(scalars["mean_probability_of_failure"]),
    )

    metrics = {
        "analysis": analysis,
        "crs": "EPSG:4326",
        "unstable_area_fraction": float(scalars["unstable_area_fraction"]),
        "min_factor_of_safety": float(scalars["min_factor_of_safety"]),
        "mean_probability_of_failure": float(scalars["mean_probability_of_failure"]),
    }
    logger.info(
        "postprocess_landlab run_id=%s analysis=%s unstable_frac=%.4f "
        "min_fos=%.4f mean_pof=%.4f uri=%s",
        run_id,
        analysis,
        metrics["unstable_area_fraction"],
        metrics["min_factor_of_safety"],
        metrics["mean_probability_of_failure"],
        uri,
    )
    return [layer], metrics


# --------------------------------------------------------------------------- #
# levers STEP 3 -- NEW published quantities (drainage_area / slope /
# relative_wetness / discharge / factor_of_safety).
#
# The EXISTING susceptibility primary stays on the byte-identical
# ``postprocess_landlab`` path above. These ADDITIVE context layers come from
# the SECONDARY field COGs the worker now writes (each computed by the same
# component chain). The reader reads each secondary COG's band + CRS into a
# RasterField and routes it through the shared executor (publish_quantities).
# --------------------------------------------------------------------------- #
def _read_cog_grid_and_georef(cog_path: Path) -> tuple[Any, str, Any]:
    """Read a secondary COG's band 1 + CRS + transform (the reproject source).

    Returns ``(grid, src_crs, src_transform)`` so the executor warps the
    metric-CRS worker field to EPSG:4326. NaN no-data preserved.
    """
    try:
        import numpy as np
        import rasterio
    except Exception as exc:  # noqa: BLE001
        raise PostprocessLandlabError(
            "LANDLAB_DEPENDENCY_MISSING",
            message=f"rasterio/numpy unavailable for secondary field read: {exc}",
        ) from exc
    if not Path(cog_path).exists():
        raise PostprocessLandlabError(
            "LANDLAB_OUTPUT_READ_FAILED",
            message=f"Landlab secondary COG not found at {cog_path}",
            details={"cog_path": str(cog_path)},
        )
    with rasterio.open(cog_path) as ds:
        arr = ds.read(1).astype("float64")
        nodata = ds.nodata
        src_crs = str(ds.crs) if ds.crs is not None else "EPSG:4326"
        src_transform = ds.transform
    if nodata is not None and np.isfinite(nodata):
        arr = np.where(arr == nodata, np.nan, arr)
    return arr, src_crs, src_transform


def publish_landlab_quantities(
    secondary_cogs_by_token: dict[str, str | Path],
    *,
    run_id: str,
    register_manifest_layers: Any,
    runs_bucket: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Any:
    """Publish the NEW Landlab quantities from the worker secondary COGs.

    ``secondary_cogs_by_token`` maps a worker token (``"drainage_area"`` /
    ``"slope"`` / ``"relative_wetness"`` / ``"discharge"`` /
    ``"factor_of_safety"``) to the LOCAL path of that field's COG (the composer
    downloads them from the Batch output alongside the primary field). Builds
    registry readers + routes them through the shared executor (ONE registrar).

    Returns the executor result, or ``None`` when no secondary COGs were
    supplied (a chain may compute none).
    """
    from dataclasses import replace as _dc_replace

    from trid3nt_contracts.output_quantities import (
        RasterField,
        get_output_registry,
    )

    from trid3nt_server.agent.workflows.shared import publish_quantities as _pq

    if not secondary_cogs_by_token:
        return None

    # quantity_id -> token (invert SECONDARY_QUANTITY_BY_TOKEN for the specs).
    qid_to_token = {qid: tok for tok, qid in SECONDARY_QUANTITY_BY_TOKEN.items()}

    def _make_reader(cog_path: str | Path):
        grid, src_crs, src_transform = _read_cog_grid_and_georef(Path(cog_path))

        def _reader(_ctx: Any) -> RasterField:
            import numpy as np

            finite = grid[np.isfinite(grid)]
            mx = float(np.max(finite)) if finite.size else 0.0
            return RasterField(
                grid=grid,
                src_crs=src_crs,
                src_transform=src_transform,
                reproject=src_crs.upper() != "EPSG:4326",
                crs_roundtrip_guard=False,
                metrics={},
            )

        return _reader

    specs = []
    for spec in get_output_registry("landlab"):
        token = qid_to_token.get(spec.quantity_id)
        if token is None or token not in secondary_cogs_by_token:
            continue
        specs.append(
            _dc_replace(spec, reader=_make_reader(secondary_cogs_by_token[token]))
        )
    if not specs:
        return None

    def _upload(cog: Path, rid: str, _bucket: Any = None, *, dest_filename: str) -> str:
        return _upload_cog_to_runs_bucket(cog, rid, runs_bucket, dest_filename=dest_filename)

    return _pq.publish_quantities(
        "landlab",
        run_id=run_id,
        upload=_upload,
        register_manifest_layers=register_manifest_layers,
        specs=specs,
        bbox=bbox,
    )


# --------------------------------------------------------------------------- #
# flow_accumulation postprocess: drainage-area raster + channel-network vector
# + routing-comparison chart. Mirrors the canonical the_FlowAccumulator tutorial
# outputs (drainage-area map, extracted channel network, routing comparison).
# --------------------------------------------------------------------------- #
def _vectorize_channel_mask(
    channel_cog_path: Path,
) -> dict[str, Any] | None:
    """Vectorize a channel-network mask COG into a EPSG:4326 GeoJSON collection.

    Reads the boolean channel mask (1.0 = channel cell, NaN elsewhere), extracts
    the channel polygons with ``rasterio.features.shapes``, reprojects each to
    EPSG:4326, and returns a GeoJSON ``FeatureCollection`` dict. Returns ``None``
    when the mask has no channel cells (a flat / tiny AOI). The polygonized
    channel footprint IS the extracted channel network (a drainage-area-threshold
    network, same definition as the tutorial's channel extraction).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.features import shapes as _shapes
        from rasterio.warp import transform_geom as _transform_geom
    except Exception as exc:  # noqa: BLE001
        raise PostprocessLandlabError(
            "LANDLAB_DEPENDENCY_MISSING",
            message=f"rasterio/numpy unavailable for channel vectorization: {exc}",
        ) from exc
    if not Path(channel_cog_path).exists():
        return None
    with rasterio.open(channel_cog_path) as ds:
        arr = ds.read(1).astype("float64")
        nodata = ds.nodata
        src_crs = ds.crs
        transform = ds.transform
    if nodata is not None and np.isfinite(nodata):
        arr = np.where(arr == nodata, np.nan, arr)
    mask = np.isfinite(arr) & (arr > 0.0)
    if not mask.any():
        return None
    mask_u8 = mask.astype("uint8")
    features: list[dict[str, Any]] = []
    for geom, val in _shapes(mask_u8, mask=mask, transform=transform):
        if int(val) != 1:
            continue
        if src_crs is not None and str(src_crs).upper() != "EPSG:4326":
            geom = _transform_geom(src_crs, "EPSG:4326", geom, precision=6)
        features.append({"type": "Feature", "properties": {"channel": 1}, "geometry": geom})
    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def _upload_geojson_to_runs_bucket(
    geojson: dict[str, Any],
    run_id: str,
    runs_bucket: str | None,
    *,
    dest_filename: str,
) -> str:
    """Upload a GeoJSON FeatureCollection to the runs bucket; return its URI.

    Scheme-aware via ``cache.storage_scheme()`` (s3 via the solver boto3 client,
    gs/file via fsspec/local), mirroring the COG upload. The client renders a
    vector ``LayerURI`` inline from this GeoJSON URI (no TiTiler tiling)."""
    import json as _json

    from trid3nt_server.agent.tools.cache import storage_scheme

    body = _json.dumps(geojson).encode("utf-8")
    scheme = storage_scheme()
    bucket = runs_bucket or __import__("os").environ.get(
        "TRID3NT_RUNS_BUCKET"
    ) or RUNS_BUCKET_DEFAULT
    key = f"{run_id}/{dest_filename}"
    uri = f"{scheme}://{bucket}/{key}"
    try:
        if scheme == "s3":
            from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

            _get_s3_client().put_object(
                Bucket=bucket, Key=key, Body=body, ContentType="application/geo+json"
            )
        else:
            import fsspec  # type: ignore

            with fsspec.open(uri, "wb") as fh:
                fh.write(body)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessLandlabError(
            "LANDLAB_COG_UPLOAD_FAILED",
            message=f"failed to upload channel-network GeoJSON to {uri}: {exc}",
            details={"run_id": run_id, "uri": uri},
        ) from exc
    return uri


def build_routing_comparison_chart_spec(
    routing_comparison: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the Vega-Lite routing-comparison chart (D8 vs Dinf vs MFD).

    Grouped bars of the channelized-area fraction per routing director -- the
    the_FlowAccumulator tutorial's central comparison (how much the routing
    choice moves where concentrated flow ends up). Returns ``None`` when the
    comparison is empty. Pure (unit-testable on a synthetic comparison list)."""
    if not routing_comparison:
        return None
    values = [
        {
            "flow_director": str(row.get("flow_director", "?")),
            "channelized_area_fraction": float(
                row.get("channelized_area_fraction", 0.0)
            ),
            "max_drainage_area_km2": float(row.get("max_drainage_area_km2", 0.0)),
        }
        for row in routing_comparison
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "bar", "color": "#1f5fbf"},
        "encoding": {
            "x": {
                "field": "flow_director",
                "type": "nominal",
                "title": "flow-routing director",
                "sort": ["D8", "Dinf", "MFD"],
            },
            "y": {
                "field": "channelized_area_fraction",
                "type": "quantitative",
                "title": "channelized area fraction",
            },
            "tooltip": [
                {"field": "flow_director", "type": "nominal"},
                {"field": "channelized_area_fraction", "type": "quantitative", "format": ".3f"},
                {"field": "max_drainage_area_km2", "type": "quantitative", "format": ".3g"},
            ],
        },
    }


def postprocess_landlab_flow_accumulation(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    channel_cog_path: str | Path | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the drainage-area COG + emit the drainage-area layer + channel vector.

    Reads the worker's ``drainage_area`` field COG, reprojects to EPSG:4326
    (CRS round-trip guard), uploads it, and returns the primary drainage-area
    ``LandlabFlowAccumulationLayerURI`` plus (when a channel mask COG is supplied
    and non-empty) the extracted channel-network vector ``LayerURI``. The typed
    narration scalars come from the worker's authoritative
    ``result["flow_accumulation"]`` block (recomputed from the field only as a
    fallback).

    Returns ``(layers, metrics)`` where ``layers[0]`` is the drainage-area raster
    (role ``"primary"``), ``layers[1:]`` the channel-network vector if present,
    and ``metrics`` carries the scalars + the ``routing_comparison`` list (the
    composer turns it into the routing-comparison chart).
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    fa = (result or {}).get("flow_accumulation") if isinstance(result, dict) else None
    fa = fa if isinstance(fa, dict) else {}

    active = np.isfinite(field)
    da_active = field[active]
    if da_active.size:
        recomputed_max = float(np.max(da_active)) / 1e6
        recomputed_mean = float(np.mean(da_active)) / 1e6
    else:
        recomputed_max = recomputed_mean = 0.0

    def _pick(key: str, fallback: float) -> float:
        v = fa.get(key)
        try:
            return float(v) if v is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    max_da = max(0.0, _pick("max_drainage_area_km2", recomputed_max))
    mean_da = max(0.0, _pick("mean_drainage_area_km2", recomputed_mean))
    chan_frac = max(0.0, min(1.0, _pick("channelized_area_fraction", 0.0)))
    routing_comparison = fa.get("routing_comparison") or []

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_drainage_area.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabFlowAccumulationLayerURI(
        layer_id=f"landlab-drainage-area-{run_id}",
        name="Drainage area",
        layer_type="raster",
        uri=uri,
        style_preset=DRAINAGE_AREA_STYLE_PRESET,
        role="primary",
        units="m^2",
        bbox=bbox,
        max_drainage_area_km2=max_da,
        mean_drainage_area_km2=mean_da,
        channelized_area_fraction=chan_frac,
    )
    layers: list[LayerURI] = [primary]

    # --- Channel-network vector (drainage-area-threshold network) ---
    if channel_cog_path is not None:
        collection = _vectorize_channel_mask(Path(channel_cog_path))
        if collection is not None:
            geojson_uri = _upload_geojson_to_runs_bucket(
                collection,
                run_id,
                runs_bucket,
                dest_filename="landlab_channel_network.geojson",
            )
            layers.append(
                LayerURI(
                    layer_id=f"landlab-channel-network-{run_id}",
                    name="Channel network",
                    layer_type="vector",
                    uri=geojson_uri,
                    style_preset="mesh_grid",
                    role="context",
                    bbox=bbox,
                )
            )

    metrics = {
        "analysis": "flow_accumulation",
        "crs": "EPSG:4326",
        "max_drainage_area_km2": max_da,
        "mean_drainage_area_km2": mean_da,
        "channelized_area_fraction": chan_frac,
        "routing_comparison": routing_comparison,
    }
    logger.info(
        "postprocess_landlab_flow_accumulation run_id=%s max_da=%.4g km2 "
        "mean_da=%.4g km2 chan_frac=%.4f channel_vector=%s uri=%s",
        run_id,
        max_da,
        mean_da,
        chan_frac,
        len(layers) > 1,
        uri,
    )
    return layers, metrics


# --------------------------------------------------------------------------- #
# green_ampt_overland_flow postprocess: infiltration-depth raster + runoff-depth
# raster + the storm-partition chart. Mirrors the canonical
# infilt_green_ampt_with_overland_flow tutorial outputs (where the storm splits
# into infiltration vs runoff).
# --------------------------------------------------------------------------- #
def build_infiltration_partition_chart_spec(
    infiltrated_fraction: float,
    runoff_fraction: float,
) -> dict[str, Any] | None:
    """Build the Vega-Lite storm-partition chart (infiltration vs runoff).

    A two-bar split of the design storm into the infiltrated share vs the runoff
    (rainfall-excess) share -- the tutorial's central question (how much of this
    storm infiltrates vs runs off). Returns ``None`` when both shares are zero
    (an empty solve). Pure (unit-testable on scalars)."""
    inf = max(0.0, float(infiltrated_fraction))
    run = max(0.0, float(runoff_fraction))
    if inf <= 0.0 and run <= 0.0:
        return None
    values = [
        {"partition": "infiltration", "fraction": inf},
        {"partition": "runoff", "fraction": run},
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "bar"},
        "encoding": {
            "x": {
                "field": "partition",
                "type": "nominal",
                "title": "storm partition",
                "sort": ["infiltration", "runoff"],
            },
            "y": {
                "field": "fraction",
                "type": "quantitative",
                "title": "fraction of storm rainfall",
            },
            "color": {
                "field": "partition",
                "type": "nominal",
                "scale": {
                    "domain": ["infiltration", "runoff"],
                    "range": ["#8c6d3f", "#1f5fbf"],
                },
                "legend": None,
            },
            "tooltip": [
                {"field": "partition", "type": "nominal"},
                {"field": "fraction", "type": "quantitative", "format": ".3f"},
            ],
        },
    }


def postprocess_landlab_green_ampt(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    runoff_cog_path: str | Path | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the infiltration-depth COG + emit the infiltration + runoff layers.

    Reads the worker's ``soil_water_infiltration__depth`` field COG, reprojects
    to EPSG:4326 (CRS round-trip guard), uploads it, and returns the primary
    infiltration-depth ``LandlabGreenAmptLayerURI`` plus (when a runoff COG is
    supplied) the runoff-depth (rainfall-excess) context raster. The typed
    partition scalars come from the worker's authoritative ``result["green_ampt"]``
    block (recomputed from the field only as a fallback).

    Returns ``(layers, metrics)`` where ``layers[0]`` is the infiltration raster
    (role ``"primary"``), ``layers[1:]`` the runoff raster if present, and
    ``metrics`` carries the partition scalars (the composer turns them into the
    partition chart).
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    ga = (result or {}).get("green_ampt") if isinstance(result, dict) else None
    ga = ga if isinstance(ga, dict) else {}

    active = np.isfinite(field)
    infil_active = field[active]
    recomputed_mean_mm = (
        float(np.mean(infil_active)) * 1000.0 if infil_active.size else 0.0
    )

    def _pick(key: str, fallback: float) -> float:
        v = ga.get(key)
        try:
            return float(v) if v is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    total_rain_mm = max(0.0, _pick("total_rainfall_mm", 0.0))
    mean_infil_mm = max(0.0, _pick("mean_infiltration_mm", recomputed_mean_mm))
    mean_runoff_mm = max(0.0, _pick("mean_runoff_mm", 0.0))
    infil_frac = max(0.0, min(1.0, _pick("infiltrated_fraction", 0.0)))
    runoff_frac = max(0.0, min(1.0, _pick("runoff_fraction", 0.0)))

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_infiltration_depth.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabGreenAmptLayerURI(
        layer_id=f"landlab-infiltration-depth-{run_id}",
        name="Infiltration depth",
        layer_type="raster",
        uri=uri,
        style_preset=INFILTRATION_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=bbox,
        infiltrated_fraction=infil_frac,
        runoff_fraction=runoff_frac,
        mean_infiltration_mm=mean_infil_mm,
        mean_runoff_mm=mean_runoff_mm,
        total_rainfall_mm=total_rain_mm,
    )
    layers: list[LayerURI] = [primary]

    # --- Runoff-depth (rainfall-excess) context raster ---
    if runoff_cog_path is not None and Path(runoff_cog_path).exists():
        dst_runoff, _rb = _reproject_field_cog_4326(Path(runoff_cog_path))
        try:
            runoff_uri = _upload_cog_to_runs_bucket(
                dst_runoff, run_id, runs_bucket, dest_filename="landlab_runoff_depth.tif"
            )
        finally:
            _safe_unlink(dst_runoff)
        layers.append(
            LayerURI(
                layer_id=f"landlab-runoff-depth-{run_id}",
                name="Runoff depth (rainfall excess)",
                layer_type="raster",
                uri=runoff_uri,
                style_preset=RUNOFF_STYLE_PRESET,
                role="context",
                units="meters",
                bbox=bbox,
            )
        )

    metrics = {
        "analysis": "green_ampt_overland_flow",
        "crs": "EPSG:4326",
        "infiltrated_fraction": infil_frac,
        "runoff_fraction": runoff_frac,
        "mean_infiltration_mm": mean_infil_mm,
        "mean_runoff_mm": mean_runoff_mm,
        "total_rainfall_mm": total_rain_mm,
    }
    logger.info(
        "postprocess_landlab_green_ampt run_id=%s rain=%.1f mm infil_frac=%.3f "
        "runoff_frac=%.3f runoff_raster=%s uri=%s",
        run_id,
        total_rain_mm,
        infil_frac,
        runoff_frac,
        len(layers) > 1,
        uri,
    )
    return layers, metrics


# --------------------------------------------------------------------------- #
# Generic binary-mask vectorizer (lake extent, fitted basin footprint) + shared
# helpers for the added Landlab diagnostic templates.
# --------------------------------------------------------------------------- #
def _vectorize_mask_cog(
    mask_cog_path: Path, *, property_name: str
) -> dict[str, Any] | None:
    """Vectorize a boolean mask COG (1.0 = in, NaN elsewhere) to a 4326 GeoJSON.

    Reads the mask, extracts polygons with ``rasterio.features.shapes``,
    reprojects each to EPSG:4326, and returns a ``FeatureCollection`` (or ``None``
    when the mask is empty). Each feature carries ``{property_name: 1}``.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.features import shapes as _shapes
        from rasterio.warp import transform_geom as _transform_geom
    except Exception as exc:  # noqa: BLE001
        raise PostprocessLandlabError(
            "LANDLAB_DEPENDENCY_MISSING",
            message=f"rasterio/numpy unavailable for mask vectorization: {exc}",
        ) from exc
    if not Path(mask_cog_path).exists():
        return None
    with rasterio.open(mask_cog_path) as ds:
        arr = ds.read(1).astype("float64")
        nodata = ds.nodata
        src_crs = ds.crs
        transform = ds.transform
    if nodata is not None and np.isfinite(nodata):
        arr = np.where(arr == nodata, np.nan, arr)
    mask = np.isfinite(arr) & (arr > 0.0)
    if not mask.any():
        return None
    features: list[dict[str, Any]] = []
    for geom, val in _shapes(mask.astype("uint8"), mask=mask, transform=transform):
        if int(val) != 1:
            continue
        if src_crs is not None and str(src_crs).upper() != "EPSG:4326":
            geom = _transform_geom(src_crs, "EPSG:4326", geom, precision=6)
        features.append(
            {"type": "Feature", "properties": {property_name: 1}, "geometry": geom}
        )
    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def _pick_from_block(
    block: dict[str, Any] | None, key: str, fallback: float
) -> float:
    """Prefer ``block[key]`` (the worker's authoritative scalar); else fallback."""
    if isinstance(block, dict) and block.get(key) is not None:
        try:
            return float(block[key])
        except (TypeError, ValueError):
            pass
    return float(fallback)


# --------------------------------------------------------------------------- #
# landslide_storm_ensemble postprocess + susceptibility-vs-recharge chart.
# --------------------------------------------------------------------------- #
def build_storm_ensemble_chart_spec(
    recharge_scenarios: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the susceptibility-vs-recharge sensitivity chart (Vega-Lite).

    A line+point of the unstable-area fraction against the swept recharge
    scenarios -- how landslide susceptibility grows with rainfall/recharge
    variability. Returns ``None`` when the ensemble is empty. Pure."""
    if not recharge_scenarios:
        return None
    values = [
        {
            "recharge_mm_day": float(r.get("recharge_mm_day", 0.0)),
            "unstable_area_fraction": float(r.get("unstable_area_fraction", 0.0)),
            "mean_probability_of_failure": float(
                r.get("mean_probability_of_failure", 0.0)
            ),
        }
        for r in recharge_scenarios
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#b5442f"},
        "encoding": {
            "x": {
                "field": "recharge_mm_day",
                "type": "quantitative",
                "title": "triggering recharge (mm/day)",
            },
            "y": {
                "field": "unstable_area_fraction",
                "type": "quantitative",
                "title": "unstable area fraction",
            },
            "tooltip": [
                {"field": "recharge_mm_day", "type": "quantitative", "format": ".1f"},
                {
                    "field": "unstable_area_fraction",
                    "type": "quantitative",
                    "format": ".4f",
                },
                {
                    "field": "mean_probability_of_failure",
                    "type": "quantitative",
                    "format": ".4f",
                },
            ],
        },
    }


def postprocess_landlab_storm_ensemble(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the ensemble-mean probability COG + emit the storm-ensemble layer.

    Returns ``(layers, metrics)`` where ``layers[0]`` is the ensemble-mean
    probability ``LandlabStormEnsembleLayerURI`` (role ``"primary"``) and
    ``metrics`` carries the recharge-scenario table (the composer turns it into
    the susceptibility-vs-recharge chart) + the typed scalars.
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    block = (result or {}).get("landslide_storm_ensemble") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    active = np.isfinite(field)
    va = field[active]
    recomputed_unstable = (
        float(np.count_nonzero(va >= UNSTABLE_PROBABILITY_THRESHOLD) / va.size)
        if va.size
        else 0.0
    )
    recomputed_mean = float(np.mean(va)) if va.size else 0.0

    unstable = max(0.0, min(1.0, _pick_from_block(block, "unstable_area_fraction", recomputed_unstable)))
    mean_pof = max(0.0, min(1.0, _pick_from_block(block, "mean_probability_of_failure", recomputed_mean)))
    min_rech = max(0.0, _pick_from_block(block, "min_recharge_mm_day", 0.0))
    max_rech = max(0.0, _pick_from_block(block, "max_recharge_mm_day", 0.0))
    n_scen = int(_pick_from_block(block, "n_recharge_scenarios", 0.0))
    slope = _pick_from_block(block, "sensitivity_slope", 0.0)
    scenarios = block.get("recharge_scenarios") or []

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_storm_ensemble.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabStormEnsembleLayerURI(
        layer_id=f"landlab-storm-ensemble-{run_id}",
        name="Ensemble-mean landslide susceptibility",
        layer_type="raster",
        uri=uri,
        style_preset=LANDSLIDE_STYLE_PRESET,
        role="primary",
        units="probability",
        bbox=bbox,
        unstable_area_fraction=unstable,
        mean_probability_of_failure=mean_pof,
        min_recharge_mm_day=min_rech,
        max_recharge_mm_day=max_rech,
        n_recharge_scenarios=max(n_scen, 1),
        sensitivity_slope=slope,
    )
    metrics = {
        "analysis": "landslide_storm_ensemble",
        "crs": "EPSG:4326",
        "unstable_area_fraction": unstable,
        "mean_probability_of_failure": mean_pof,
        "min_recharge_mm_day": min_rech,
        "max_recharge_mm_day": max_rech,
        "sensitivity_slope": slope,
        "recharge_scenarios": scenarios,
    }
    logger.info(
        "postprocess_landlab_storm_ensemble run_id=%s n_scenarios=%d unstable=%.4f "
        "recharge=[%.1f,%.1f] slope=%.5f uri=%s",
        run_id, n_scen, unstable, min_rech, max_rech, slope, uri,
    )
    return [primary], metrics


# --------------------------------------------------------------------------- #
# overland_flow_timeseries postprocess: peak-depth primary + per-frame animation
# layers + the max-depth-cell hydrograph chart.
# --------------------------------------------------------------------------- #
def build_overland_hydrograph_chart_spec(
    series: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the depth-vs-time hydrograph chart at the max-depth cell (Vega-Lite).

    A line of surface-water depth against elapsed seconds at the cell that reached
    the peak depth. Returns ``None`` when the series is empty. Pure."""
    if not series or len(series) < 2:
        return None
    values = [
        {
            "time_s": float(p.get("time_s", 0.0)),
            "depth_m": float(p.get("depth_m", 0.0)),
        }
        for p in series
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "time_s", "type": "quantitative", "title": "time (s)"},
            "y": {
                "field": "depth_m",
                "type": "quantitative",
                "title": "surface-water depth (m)",
            },
            "tooltip": [
                {"field": "time_s", "type": "quantitative", "format": ".0f"},
                {"field": "depth_m", "type": "quantitative", "format": ".4f"},
            ],
        },
    }


def postprocess_landlab_overland_timeseries(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    frame_cogs_by_token: dict[str, str] | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the peak-depth COG + emit the peak layer + per-frame animation layers.

    ``frame_cogs_by_token`` maps ``depth_step_NN`` -> local COG path (the composer
    downloads them alongside the peak field). Each frame is reprojected to 4326,
    uploaded, and emitted as a time-stepped animation LayerURI carrying the web
    scrubber ``step N`` naming. Returns ``(layers, metrics)`` where ``layers[0]``
    is the peak-depth primary and ``layers[1:]`` the ordered frames.
    """
    import numpy as np

    from trid3nt_server.agent.workflows.shared.frames import (
        frame_dest_filename,
        frame_layer_id,
        frame_name,
        peak_layer_id,
        peak_layer_name,
    )

    src = Path(field_cog_path)
    field = _read_field_array(src)
    block = (result or {}).get("overland_flow_timeseries") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    active = np.isfinite(field)
    va = field[active]
    recomputed_wet = (
        float(np.count_nonzero(va >= OVERLAND_WET_DEPTH_M) / va.size) if va.size else 0.0
    )
    recomputed_max = float(np.max(va)) if va.size else 0.0

    wet_frac = max(0.0, min(1.0, _pick_from_block(block, "wet_area_fraction", recomputed_wet)))
    max_depth = max(0.0, _pick_from_block(block, "max_depth_m", recomputed_max))
    time_to_peak = max(0.0, _pick_from_block(block, "time_to_peak_s", 0.0))
    series = block.get("max_cell_series") or []

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        peak_uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_overland_peak.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    stem = "landlab-overland-depth"
    quantity_label = "Overland depth"

    # --- Per-frame animation layers (ordered by the depth_step token index) ---
    frame_layers: list[LayerURI] = []
    tokens = sorted((frame_cogs_by_token or {}).keys())
    frame_no = 0
    for tok in tokens:
        local = (frame_cogs_by_token or {}).get(tok)
        if not local or not Path(local).exists():
            continue
        frame_no += 1
        try:
            dst_frame, _fb = _reproject_field_cog_4326(Path(local))
        except PostprocessLandlabError as exc:
            logger.warning("overland frame %s reproject failed: %s", tok, exc)
            continue
        try:
            frame_uri = _upload_cog_to_runs_bucket(
                dst_frame,
                run_id,
                runs_bucket,
                dest_filename=frame_dest_filename(stem.replace("-", "_"), frame_no),
            )
        finally:
            _safe_unlink(dst_frame)
        frame_layers.append(
            LayerURI(
                layer_id=frame_layer_id(stem, frame_no, run_id),
                name=frame_name(frame_no, quantity_label),
                layer_type="raster",
                uri=frame_uri,
                style_preset=OVERLAND_STYLE_PRESET,
                role="context",
                units="meters",
                bbox=bbox,
            )
        )

    primary = LandlabOverlandTimeseriesLayerURI(
        layer_id=peak_layer_id(stem, run_id),
        name=peak_layer_name(quantity_label),
        layer_type="raster",
        uri=peak_uri,
        style_preset=OVERLAND_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=bbox,
        wet_area_fraction=wet_frac,
        max_depth_m=max_depth,
        n_frames=len(frame_layers),
        time_to_peak_s=time_to_peak,
    )
    # A single frame can never form a web scrubber group; drop a lone frame.
    if len(frame_layers) < 2:
        frame_layers = []
        primary = primary.model_copy(update={"n_frames": 0})

    layers: list[LayerURI] = [primary, *frame_layers]
    metrics = {
        "analysis": "overland_flow_timeseries",
        "crs": "EPSG:4326",
        "wet_area_fraction": wet_frac,
        "max_depth_m": max_depth,
        "time_to_peak_s": time_to_peak,
        "n_frames": len(frame_layers),
        "max_cell_series": series,
    }
    logger.info(
        "postprocess_landlab_overland_timeseries run_id=%s max_depth=%.4f m "
        "frames=%d uri=%s",
        run_id, max_depth, len(frame_layers), peak_uri,
    )
    return layers, metrics


# --------------------------------------------------------------------------- #
# dem_pit_fill postprocess: fill-depth conditioning raster.
# --------------------------------------------------------------------------- #
def postprocess_landlab_dem_conditioning(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the fill-depth COG + emit the DEM-conditioning layer.

    Returns ``(layers, metrics)`` where ``layers[0]`` is the fill-depth
    ``LandlabDemConditioningLayerURI`` (role ``"primary"``).
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    block = (result or {}).get("dem_pit_fill") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    active = np.isfinite(field)
    va = field[active]
    recomputed_max = float(np.max(va)) if va.size else 0.0
    recomputed_filled = (
        float(np.count_nonzero(va >= 1e-3) / va.size) if va.size else 0.0
    )

    max_fill = max(0.0, _pick_from_block(block, "max_fill_depth_m", recomputed_max))
    filled_frac = max(0.0, min(1.0, _pick_from_block(block, "filled_area_fraction", recomputed_filled)))
    n_dep = int(_pick_from_block(block, "n_depressions", 0.0))

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_fill_depth.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabDemConditioningLayerURI(
        layer_id=f"landlab-fill-depth-{run_id}",
        name="DEM fill depth",
        layer_type="raster",
        uri=uri,
        style_preset=FILL_DEPTH_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=bbox,
        max_fill_depth_m=max_fill,
        filled_area_fraction=filled_frac,
        n_depressions=max(n_dep, 0),
    )
    metrics = {
        "analysis": "dem_pit_fill",
        "crs": "EPSG:4326",
        "max_fill_depth_m": max_fill,
        "filled_area_fraction": filled_frac,
        "n_depressions": n_dep,
    }
    logger.info(
        "postprocess_landlab_dem_conditioning run_id=%s max_fill=%.3f m "
        "filled_frac=%.4f n_depressions=%d uri=%s",
        run_id, max_fill, filled_frac, n_dep, uri,
    )
    return [primary], metrics


# --------------------------------------------------------------------------- #
# lake_mapping postprocess: lake-depth raster + lake-extent vector.
# --------------------------------------------------------------------------- #
def postprocess_landlab_lake_mapping(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    extent_cog_path: str | Path | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the lake-depth COG + emit the lake-depth layer + lake-extent vector.

    Returns ``(layers, metrics)`` where ``layers[0]`` is the lake-depth
    ``LandlabLakeMappingLayerURI`` (role ``"primary"``) and ``layers[1:]`` the
    lake-extent vector when present.
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    block = (result or {}).get("lake_mapping") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    fin = field[np.isfinite(field)]
    recomputed_max = float(np.max(fin)) if fin.size else 0.0

    n_lakes = int(_pick_from_block(block, "n_lakes", 0.0))
    total_area = max(0.0, _pick_from_block(block, "total_lake_area_km2", 0.0))
    total_vol = max(0.0, _pick_from_block(block, "total_lake_volume_m3", 0.0))
    max_depth = max(0.0, _pick_from_block(block, "max_lake_depth_m", recomputed_max))

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_lake_depth.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabLakeMappingLayerURI(
        layer_id=f"landlab-lake-depth-{run_id}",
        name="Lake depth",
        layer_type="raster",
        uri=uri,
        style_preset=LAKE_DEPTH_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=bbox,
        n_lakes=max(n_lakes, 0),
        total_lake_area_km2=total_area,
        total_lake_volume_m3=total_vol,
        max_lake_depth_m=max_depth,
    )
    layers: list[LayerURI] = [primary]

    if extent_cog_path is not None:
        collection = _vectorize_mask_cog(Path(extent_cog_path), property_name="lake")
        if collection is not None:
            geojson_uri = _upload_geojson_to_runs_bucket(
                collection, run_id, runs_bucket, dest_filename="landlab_lake_extent.geojson"
            )
            layers.append(
                LayerURI(
                    layer_id=f"landlab-lake-extent-{run_id}",
                    name="Lake extent",
                    layer_type="vector",
                    uri=geojson_uri,
                    style_preset="mesh_grid",
                    role="context",
                    bbox=bbox,
                )
            )

    metrics = {
        "analysis": "lake_mapping",
        "crs": "EPSG:4326",
        "n_lakes": n_lakes,
        "total_lake_area_km2": total_area,
        "total_lake_volume_m3": total_vol,
        "max_lake_depth_m": max_depth,
    }
    logger.info(
        "postprocess_landlab_lake_mapping run_id=%s n_lakes=%d area=%.4g km2 "
        "max_depth=%.3f m extent_vector=%s uri=%s",
        run_id, n_lakes, total_area, max_depth, len(layers) > 1, uri,
    )
    return layers, metrics


# --------------------------------------------------------------------------- #
# hacks_law postprocess: drainage-area backdrop + basin vector + log-log chart.
# --------------------------------------------------------------------------- #
def build_hacks_law_chart_spec(
    scatter: list[dict[str, Any]],
    *,
    exponent: float,
    coefficient: float,
) -> dict[str, Any] | None:
    """Build the Hack's-law log-log scatter chart (Vega-Lite).

    Channel-length vs drainage-area points on log-log axes plus the fitted
    ``L = C * A**h`` line, so the fitted exponent is visible against the classic
    ~0.5-0.6. Returns ``None`` when the scatter is empty. Pure."""
    if not scatter or len(scatter) < 3:
        return None
    pts = [
        {
            "area_m2": float(p.get("area_m2", 0.0)),
            "length_m": float(p.get("length_m", 0.0)),
        }
        for p in scatter
        if float(p.get("area_m2", 0.0)) > 0.0 and float(p.get("length_m", 0.0)) > 0.0
    ]
    if len(pts) < 3:
        return None
    areas = [p["area_m2"] for p in pts]
    a_min, a_max = min(areas), max(areas)
    c = max(float(coefficient), 1e-12)
    h = float(exponent)
    fit = [
        {"area_m2": a_min, "length_m": c * (a_min ** h)},
        {"area_m2": a_max, "length_m": c * (a_max ** h)},
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "layer": [
            {
                "data": {"values": pts},
                "mark": {"type": "point", "filled": True, "color": "#1f5fbf", "opacity": 0.5},
                "encoding": {
                    "x": {
                        "field": "area_m2",
                        "type": "quantitative",
                        "scale": {"type": "log"},
                        "title": "drainage area A (m^2)",
                    },
                    "y": {
                        "field": "length_m",
                        "type": "quantitative",
                        "scale": {"type": "log"},
                        "title": "flow-path length L (m)",
                    },
                },
            },
            {
                "data": {"values": fit},
                "mark": {"type": "line", "color": "#b5442f"},
                "encoding": {
                    "x": {"field": "area_m2", "type": "quantitative", "scale": {"type": "log"}},
                    "y": {"field": "length_m", "type": "quantitative", "scale": {"type": "log"}},
                },
            },
        ],
    }


def postprocess_landlab_hacks_law(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    basin_cog_path: str | Path | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the drainage-area COG + emit the Hack's-law diagnostic layers.

    Returns ``(layers, metrics)`` where ``layers[0]`` is the drainage-area
    ``LandlabHacksLawLayerURI`` (role ``"primary"``), ``layers[1:]`` the fitted
    basin vector when present, and ``metrics`` carries the scatter + exponent (the
    composer turns them into the log-log chart).
    """
    src = Path(field_cog_path)
    _field = _read_field_array(src)
    block = (result or {}).get("hacks_law") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    exponent = max(0.0, _pick_from_block(block, "hack_exponent", 0.0))
    coefficient = max(0.0, _pick_from_block(block, "hack_coefficient", 0.0))
    largest_area = max(0.0, _pick_from_block(block, "largest_basin_area_km2", 0.0))
    n_basins = int(_pick_from_block(block, "n_basins", 0.0))
    scatter = block.get("scatter") or []

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_hacks_drainage_area.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabHacksLawLayerURI(
        layer_id=f"landlab-hacks-law-{run_id}",
        name="Drainage area (Hack diagnostic)",
        layer_type="raster",
        uri=uri,
        style_preset=DRAINAGE_AREA_STYLE_PRESET,
        role="primary",
        units="m^2",
        bbox=bbox,
        hack_exponent=exponent,
        hack_coefficient=coefficient,
        largest_basin_area_km2=largest_area,
        n_basins=max(n_basins, 0),
    )
    layers: list[LayerURI] = [primary]

    if basin_cog_path is not None:
        collection = _vectorize_mask_cog(Path(basin_cog_path), property_name="basin")
        if collection is not None:
            geojson_uri = _upload_geojson_to_runs_bucket(
                collection, run_id, runs_bucket, dest_filename="landlab_hacks_basin.geojson"
            )
            layers.append(
                LayerURI(
                    layer_id=f"landlab-hacks-basin-{run_id}",
                    name="Largest fitted basin",
                    layer_type="vector",
                    uri=geojson_uri,
                    style_preset="mesh_grid",
                    role="context",
                    bbox=bbox,
                )
            )

    metrics = {
        "analysis": "hacks_law",
        "crs": "EPSG:4326",
        "hack_exponent": exponent,
        "hack_coefficient": coefficient,
        "largest_basin_area_km2": largest_area,
        "n_basins": n_basins,
        "scatter": scatter,
    }
    logger.info(
        "postprocess_landlab_hacks_law run_id=%s exponent=%.4f n_basins=%d "
        "basin_vector=%s uri=%s",
        run_id, exponent, n_basins, len(layers) > 1, uri,
    )
    return layers, metrics


# --------------------------------------------------------------------------- #
# hand postprocess: HAND raster + channel-network vector.
# --------------------------------------------------------------------------- #
def postprocess_landlab_hand(
    field_cog_path: str | Path,
    *,
    run_id: str,
    result: dict[str, Any] | None = None,
    channel_cog_path: str | Path | None = None,
    runs_bucket: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Reproject the HAND COG + emit the HAND layer + channel-network vector.

    Returns ``(layers, metrics)`` where ``layers[0]`` is the HAND
    ``LandlabHandLayerURI`` (role ``"primary"``) and ``layers[1:]`` the channel
    network vector when present.
    """
    import numpy as np

    src = Path(field_cog_path)
    field = _read_field_array(src)
    block = (result or {}).get("hand") if isinstance(result, dict) else None
    block = block if isinstance(block, dict) else {}

    active = np.isfinite(field)
    va = field[active]
    recomputed_mean = float(np.mean(va)) if va.size else 0.0
    recomputed_max = float(np.max(va)) if va.size else 0.0

    mean_h = max(0.0, _pick_from_block(block, "mean_hand_m", recomputed_mean))
    max_h = max(0.0, _pick_from_block(block, "max_hand_m", recomputed_max))
    chan_frac = max(0.0, min(1.0, _pick_from_block(block, "channel_area_fraction", 0.0)))
    lowland_frac = max(0.0, min(1.0, _pick_from_block(block, "lowland_area_fraction", 0.0)))

    dst_cog, bbox = _reproject_field_cog_4326(src)
    try:
        uri = _upload_cog_to_runs_bucket(
            dst_cog, run_id, runs_bucket, dest_filename="landlab_hand.tif"
        )
    finally:
        _safe_unlink(dst_cog)

    primary = LandlabHandLayerURI(
        layer_id=f"landlab-hand-{run_id}",
        name="Height above nearest drainage",
        layer_type="raster",
        uri=uri,
        style_preset=HAND_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=bbox,
        mean_hand_m=mean_h,
        max_hand_m=max_h,
        channel_area_fraction=chan_frac,
        lowland_area_fraction=lowland_frac,
    )
    layers: list[LayerURI] = [primary]

    if channel_cog_path is not None:
        collection = _vectorize_mask_cog(Path(channel_cog_path), property_name="channel")
        if collection is not None:
            geojson_uri = _upload_geojson_to_runs_bucket(
                collection, run_id, runs_bucket, dest_filename="landlab_hand_channel.geojson"
            )
            layers.append(
                LayerURI(
                    layer_id=f"landlab-hand-channel-{run_id}",
                    name="Channel network",
                    layer_type="vector",
                    uri=geojson_uri,
                    style_preset="mesh_grid",
                    role="context",
                    bbox=bbox,
                )
            )

    metrics = {
        "analysis": "hand",
        "crs": "EPSG:4326",
        "mean_hand_m": mean_h,
        "max_hand_m": max_h,
        "channel_area_fraction": chan_frac,
        "lowland_area_fraction": lowland_frac,
    }
    logger.info(
        "postprocess_landlab_hand run_id=%s mean_hand=%.3f m max_hand=%.3f m "
        "channel_frac=%.4f channel_vector=%s uri=%s",
        run_id, mean_h, max_h, chan_frac, len(layers) > 1, uri,
    )
    return layers, metrics
