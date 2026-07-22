"""Landlab run-output postprocessing (sprint-17 — NEW engine).

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

from trid3nt_contracts.landlab_contracts import LandlabSusceptibilityLayerURI

from . import cog_io
from .cog_io import CogIoError

__all__ = [
    "PostprocessLandlabError",
    "postprocess_landlab",
    "publish_landlab_quantities",
    "compute_landlab_metrics",
    "LANDSLIDE_STYLE_PRESET",
    "OVERLAND_STYLE_PRESET",
    "UNSTABLE_PROBABILITY_THRESHOLD",
    "SECONDARY_QUANTITY_BY_TOKEN",
]

logger = logging.getLogger("trid3nt_server.workflows.postprocess_landlab")

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

    from . import publish_quantities as _pq

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
