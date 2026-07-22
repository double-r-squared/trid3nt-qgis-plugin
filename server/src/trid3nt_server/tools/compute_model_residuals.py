"""``compute_model_residuals`` composer tool -- observed-vs-modeled residuals.

Samples a MODEL raster (a MODFLOW simulated-head COG, a plume-concentration
COG, or any single-band raster the uri registry resolves) at OBSERVATION
points and returns a residuals point vector layer (``observed - simulated``
per point) plus fit statistics -- the standard model-calibration diagnostic
("how far off is the model from what was actually measured").

Observations come from EITHER an existing vector layer (``observations_layer_uri``
-- e.g. the point layer ``fetch_usgs_groundwater_levels`` produces) OR, when
``observations_layer_uri`` is omitted and a ``bbox`` is given, this tool fetches
USGS groundwater readings itself by calling ``fetch_usgs_groundwater_levels``'s
SHARED CORE (``_fetch_usgs_groundwater_levels_bytes``), not the LLM-facing tool
wrapper -- so no extra round-trip / duplicate tool call is needed.

HONEST UNITS/SEMANTICS (the load-bearing design constraint): USGS groundwater
readings come in two families that are NOT interchangeable --

- DEPTH-TO-WATER (pcodes 72019 / 61055, ft below land surface or a measuring
  point) -- larger value = DEEPER water table (an INVERTED sign convention
  vs. an elevation head).
- ELEVATION-referenced water level (pcodes 72150 / 62610 / 62611, ft relative
  to NAVD88/NGVD29) -- directly analogous to a MODFLOW simulated HEAD
  (elevation), modulo matching vertical datum.

A MODFLOW head raster is an ELEVATION. Comparing it against DEPTH-TO-WATER
readings without converting first (elevation = land-surface elevation minus
depth-to-water) produces a meaningless number, not a real residual. This tool
NEVER silently mixes/ignores that: it detects the fetched reading family from
the source schema (``parameter_code`` on the USGS layer), filters mixed
fetches down to one consistent family when both appear, and always attaches
an explicit ``units_warning`` to the result -- the tool is still useful for
RELATIVE spatial-bias reading even when an absolute unit match cannot be
confirmed, and the warning says so.

Output: (a) a point ``LayerURI`` (FlatGeobuf, EPSG:4326) with per-point
``observed`` / ``simulated`` / ``residual`` properties, a diverging
(red-blue, centered on zero) continuous legend on ``residual``, named
``"Model residuals (<n> points)"``; (b) fit statistics carried directly on
the returned ``ModelResidualsLayerURI`` (mirrors the ``compute_flood_depth_
damage`` / ``compute_exposure_summary`` pattern -- a ``LayerURI`` subclass IS
the "result dict" the LLM reads from the function response, no separate
publish step required for a vector layer).

``cacheable=False`` (``live-no-cache``): a comparison composer over
live/caller-supplied inputs; the artifact goes to the runs bucket (or
``_output_dir`` for offline tests), mirroring ``compute_flood_depth_damage``.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from trid3nt_contracts.execution import LayerURI, LegendKey
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_model_residuals",
    "ModelResidualsLayerURI",
    "ResidualsError",
    "ResidualsInputError",
    "ResidualsNoObservationsError",
    "ResidualsAllNodataError",
    "ResidualsUpstreamError",
    "USGS_ELEVATION_PCODES",
    "USGS_DEPTH_PCODES",
    "OBSERVED_FIELD_CANDIDATES",
]

logger = logging.getLogger("trid3nt_server.tools.compute_model_residuals")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ResidualsError(RuntimeError):
    """Base class for compute_model_residuals failures."""

    error_code: str = "MODEL_RESIDUALS_ERROR"
    retryable: bool = True


class ResidualsInputError(ResidualsError):
    """Bad inputs -- unreadable raster, missing field, no selector given."""

    error_code = "MODEL_RESIDUALS_INPUT_INVALID"
    retryable = False


class ResidualsNoObservationsError(ResidualsError):
    """No observation points at all, or none inside the raster's footprint."""

    error_code = "MODEL_RESIDUALS_NO_OBSERVATIONS"
    retryable = False


class ResidualsAllNodataError(ResidualsError):
    """Observation points exist in the footprint but every sample is nodata."""

    error_code = "MODEL_RESIDUALS_ALL_NODATA"
    retryable = False


class ResidualsUpstreamError(ResidualsError):
    """Input staging, the USGS fetch, or the artifact write failed."""

    error_code = "MODEL_RESIDUALS_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the fit-statistics side-channel.
# ---------------------------------------------------------------------------


class ModelResidualsLayerURI(LayerURI):
    """The residuals point ``LayerURI`` plus fit-statistics summary.

    Extra fields beyond ``LayerURI``:

    - ``n_points`` -- points used in the statistics (in-footprint, both
      observed and simulated finite).
    - ``mean_error`` / ``bias`` -- mean of ``observed - simulated`` (the same
      statistic under both names; positive = model reads LOW vs. observed on
      average, negative = model reads HIGH).
    - ``rmse`` / ``mae`` -- root-mean-square / mean-absolute residual.
    - ``min_residual`` / ``max_residual``.
    - ``units_warning`` -- ALWAYS populated; states what is known (or not
      known) about whether the observed and modeled values share units and a
      vertical reference.
    - ``interpretation`` -- one-line honest summary, e.g. "Model biased low
      by 0.42 ft on average (n=14)."
    - ``small_n_caveat`` -- True when ``n_points < 3`` (stats still computed
      and returned, but flagged as not statistically meaningful).
    - ``notes`` -- provenance + filtering/exclusion detail.
    """

    n_points: int = 0
    mean_error: float = 0.0
    bias: float = 0.0
    rmse: float = 0.0
    mae: float = 0.0
    min_residual: float = 0.0
    max_residual: float = 0.0
    units_warning: str = ""
    interpretation: str = ""
    small_n_caveat: bool = False
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS groundwater parameter codes referenced to a fixed ELEVATION datum
#: (NAVD88/NGVD29) -- directly analogous to a MODFLOW simulated head.
USGS_ELEVATION_PCODES: frozenset[str] = frozenset({"72150", "62610", "62611"})

#: USGS groundwater parameter codes reporting DEPTH-TO-WATER (ft below land
#: surface / a measuring point) -- an inverted, non-elevation convention.
USGS_DEPTH_PCODES: frozenset[str] = frozenset({"72019", "61055"})

#: Candidate observed-value field names tried, in priority order, when
#: ``observed_value_field`` is not supplied. ``water_level`` (the
#: ``fetch_usgs_groundwater_levels`` schema) is tried first since that is the
#: canonical observed-head source this tool pairs with.
OBSERVED_FIELD_CANDIDATES: tuple[str, ...] = (
    "water_level",
    "observed_value",
    "obs_value",
    "value",
    "head",
    "obs_head",
    "gw_elevation",
    "level",
    "elevation",
    "depth_to_water",
    "concentration",
    "measurement",
)

_M_TO_FT = 3.280839895

_STYLE_PRESET = "model_residuals"

_METADATA = AtomicToolMetadata(
    name="compute_model_residuals",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)

_GENERIC_UNITS_WARNING = (
    "The observations layer carries no known field-semantics metadata (e.g. "
    "a USGS parameter_code) this tool recognizes -- whether the observed "
    "field and the model raster share the same units and vertical reference "
    "was NOT verified. Confirm this manually before treating residuals as an "
    "absolute error; they remain useful as a RELATIVE spatial-bias signal "
    "either way."
)


# ---------------------------------------------------------------------------
# Staging helpers (mirror compute_flood_depth_damage / compute_sediment_yield).
# ---------------------------------------------------------------------------


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ResidualsUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise ResidualsInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise ResidualsInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


def _to_float_array(values: Any) -> np.ndarray:
    """Best-effort elementwise float coercion; unparsable entries -> NaN."""
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        try:
            fv = float(v)
            out[i] = fv if math.isfinite(fv) else np.nan
        except (TypeError, ValueError):
            out[i] = np.nan
    return out


# ---------------------------------------------------------------------------
# Observation loading.
# ---------------------------------------------------------------------------


def _load_observations_from_uri(observations_layer_uri: str, tmpdir: str) -> Any:
    """Load a caller-supplied observations vector layer as a GeoDataFrame."""
    import geopandas as gpd

    local = _stage_uri_local(observations_layer_uri, tmpdir, "observations")
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise ResidualsInputError(
            f"could not open observations_layer_uri {observations_layer_uri!r}: {exc}"
        ) from exc
    return gdf


def _fetch_observations_from_bbox(
    bbox: tuple[float, float, float, float], tmpdir: str, notes: list[str]
) -> Any:
    """Fetch USGS groundwater readings over ``bbox`` via the SHARED CORE.

    Calls ``fetch_usgs_groundwater_levels._fetch_usgs_groundwater_levels_bytes``
    directly -- the internal function, not the LLM-facing tool wrapper -- so
    this composer does not need a separate tool round-trip. Lazy import
    mirrors ``compute_flood_depth_damage``'s ``fetch_usace_nsi`` pattern.
    """
    import geopandas as gpd

    from .fetch_usgs_groundwater_levels import (
        GwInputError,
        GwNoWellsError,
        GwUpstreamError,
        _fetch_usgs_groundwater_levels_bytes,
        _round_bbox_to_6dp,
        _validate_bbox,
    )

    try:
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        rounded = _round_bbox_to_6dp(bbox_t)
        fgb_bytes, _extent = _fetch_usgs_groundwater_levels_bytes(
            state_fips=None,
            bbox=rounded,
            scope_label=f"bbox={rounded!r}",
        )
    except GwNoWellsError as exc:
        raise ResidualsNoObservationsError(
            f"no USGS groundwater observations available for bbox={bbox!r}: {exc}"
        ) from exc
    except GwInputError as exc:
        raise ResidualsInputError(str(exc)) from exc
    except GwUpstreamError as exc:
        raise ResidualsUpstreamError(str(exc)) from exc

    local = os.path.join(tmpdir, "usgs_groundwater_levels.fgb")
    with open(local, "wb") as f:
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise ResidualsUpstreamError(
            f"could not read the fetched USGS groundwater FlatGeobuf: {exc}"
        ) from exc
    notes.append(
        f"Observations: USGS groundwater monitoring wells via "
        f"fetch_usgs_groundwater_levels over bbox={tuple(round(v, 4) for v in bbox)}."
    )
    return gdf


# ---------------------------------------------------------------------------
# Observed-field resolution + honest units/semantics detection.
# ---------------------------------------------------------------------------


def _resolve_observed_field(gdf: Any, observed_value_field: str | None) -> str:
    """Pick the observed-value column: caller-supplied verbatim, else auto."""
    columns = [c for c in gdf.columns if c != "geometry"]
    if observed_value_field:
        if observed_value_field not in gdf.columns:
            raise ResidualsInputError(
                f"observed_value_field={observed_value_field!r} not found on "
                f"the observations layer; available columns: {sorted(columns)}"
            )
        return observed_value_field

    for cand in OBSERVED_FIELD_CANDIDATES:
        if cand in gdf.columns and np.isfinite(_to_float_array(gdf[cand])).any():
            return cand

    numeric_candidates = sorted(
        c for c in columns if np.isfinite(_to_float_array(gdf[c])).any()
    )
    raise ResidualsInputError(
        "could not auto-detect an observed-value field on the observations "
        f"layer (tried {list(OBSERVED_FIELD_CANDIDATES)}); pass "
        f"observed_value_field explicitly. Available columns: {sorted(columns)}"
        + (
            f"; numeric-looking candidates: {numeric_candidates}"
            if numeric_candidates
            else ""
        )
    )


def _apply_usgs_semantics(
    gdf: Any, field: str, notes: list[str]
) -> tuple[Any, str]:
    """Honest units/semantics handling for the USGS groundwater schema.

    No-op (returns ``gdf`` unchanged) unless ``field == "water_level"`` AND
    the layer carries a ``parameter_code`` column (the
    ``fetch_usgs_groundwater_levels`` schema) -- a generic caller-supplied
    field/layer always gets the generic disclaimer instead. When BOTH
    depth-to-water and elevation-referenced readings are present in the same
    fetch, filters down to the elevation-referenced subset (directly
    comparable to a head raster) and notes the drop -- never silently
    averages incompatible units together.

    Returns ``(gdf, units_warning)``; ``units_warning`` is NEVER empty.
    """
    if field != "water_level" or "parameter_code" not in gdf.columns:
        return gdf, _GENERIC_UNITS_WARNING

    codes = gdf["parameter_code"].astype(str)
    present = set(codes.unique())
    elev = present & USGS_ELEVATION_PCODES
    depth = present & USGS_DEPTH_PCODES

    datum = None
    if "vertical_datum" in gdf.columns:
        vals = [v for v in gdf["vertical_datum"].tolist() if v]
        datum = vals[0] if vals else None
    datum_label = datum or "the reported vertical datum"

    if elev and depth:
        n_before = len(gdf)
        gdf = gdf[codes.isin(elev)].copy()
        notes.append(
            f"Observations mixed DEPTH-TO-WATER (pcode(s) {sorted(depth)}) and "
            f"ELEVATION-referenced (pcode(s) {sorted(elev)}) readings in the "
            f"same fetch; kept only the {len(gdf)}/{n_before} elevation-"
            "referenced reading(s) for a consistent comparison against the "
            "model raster."
        )
        return gdf, (
            f"Observed values are groundwater-level ELEVATION referenced to "
            f"{datum_label} (depth-to-water readings from the same fetch were "
            "dropped -- see notes). Confirm the model raster shares the same "
            "vertical datum/CRS before treating residuals as absolute error."
        )

    if elev:
        return gdf, (
            f"Observed values are groundwater-level ELEVATION referenced to "
            f"{datum_label}. Confirm the model raster shares the same "
            "vertical datum/CRS before treating residuals as absolute error; "
            "if datums differ, residuals are only meaningful as RELATIVE "
            "spatial bias."
        )

    if depth:
        return gdf, (
            "Observed values are DEPTH-TO-WATER (ft below land surface or a "
            "measuring point) -- NOT a head elevation. If this model raster "
            "represents head ELEVATION (e.g. a simulated MODFLOW head), "
            "depth-to-water and elevation are NOT directly comparable without "
            "converting first (elevation = land-surface elevation minus "
            "depth-to-water). Residuals below are only meaningful as a "
            "RELATIVE spatial-bias signal, not an absolute-error comparison, "
            "unless you convert units first."
        )

    return gdf, _GENERIC_UNITS_WARNING


# ---------------------------------------------------------------------------
# Bilinear raster sampling.
# ---------------------------------------------------------------------------


def _bilinear_sample(
    band: np.ndarray, transform: Any, xs: np.ndarray, ys: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear-sample ``band`` (NaN-filled nodata) at world coords (xs, ys).

    Returns ``(in_bounds, samples)``:
    - ``in_bounds`` -- True where the point's world coordinate falls inside
      the raster's rectangular extent (footprint test, independent of
      nodata).
    - ``samples`` -- bilinear-interpolated value at each point; NaN where the
      point falls outside the extent OR the interpolation stencil touches a
      nodata / out-of-extent neighbor (points within ~half a pixel of the
      raster edge may fall in this bucket -- honest, not a bug).
    """
    from scipy.ndimage import map_coordinates

    inv = ~transform
    cols_corner, rows_corner = inv * (
        np.asarray(xs, dtype=np.float64),
        np.asarray(ys, dtype=np.float64),
    )
    height, width = band.shape
    in_bounds = (
        (cols_corner >= 0)
        & (cols_corner <= width)
        & (rows_corner >= 0)
        & (rows_corner <= height)
    )
    # map_coordinates indexes array elements (pixel CENTERS) at integer
    # coordinates; the affine inverse gives pixel-CORNER fractional coords,
    # so shift by half a pixel to align with the interpolation grid.
    coords = np.vstack([rows_corner - 0.5, cols_corner - 0.5])
    samples = map_coordinates(band, coords, order=1, mode="constant", cval=np.nan)
    return in_bounds, samples


# ---------------------------------------------------------------------------
# Output helpers.
# ---------------------------------------------------------------------------


def _write_output(payload: bytes, seed: str, output_dir: str | None) -> str:
    """Persist the FGB; return its URI (local for tests, runs bucket live)."""
    filename = f"model_residuals_{seed}.fgb"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from .solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"model-residuals-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise ResidualsUpstreamError(
            f"failed to upload the model-residuals FGB to the runs bucket: {exc}"
        ) from exc


def _build_legend(max_abs_residual: float, units: str | None) -> LegendKey:
    """Diverging (red-blue, centered on zero) continuous legend on residual."""
    span = max_abs_residual if max_abs_residual > 1e-9 else 1.0
    return LegendKey(
        kind="continuous",
        colormap="rdbu",
        vmin=-span,
        vmax=span,
        value_field="residual",
        units=units,
        label="Model residual (observed - simulated)",
    )


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: may fetch its own USGS groundwater observations (external
    # API) when observations_layer_uri is not passed -- the same
    # input-fetching-composer shape as compute_flood_depth_damage, so
    # open_world_hint=True is honest (listed in
    # test_tool_annotations._OPEN_WORLD_COMPUTE_EXCEPTIONS).
    open_world_hint=True,
)
def compute_model_residuals(
    model_layer_uri: str,
    observations_layer_uri: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    observed_value_field: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> ModelResidualsLayerURI:
    """Observed-vs-modeled residuals: sample a MODEL raster at OBSERVATION points.

    Use this to CALIBRATE / sanity-check a model result against real
    measurements -- e.g. a MODFLOW simulated-head COG vs. USGS groundwater
    monitoring wells, or a plume-concentration COG vs. any point layer of
    measured concentrations. Samples the raster (bilinear) at every
    observation point inside its footprint, computes
    ``residual = observed - simulated`` per point, and returns the residual
    points as a map layer plus fit statistics (mean error / RMSE / MAE /
    bias).

    **When to use:**
    - "How well does this MODFLOW head raster match the observed wells?"
      right after ``run_model_sustainable_yield_scenario`` /
      ``run_modflow_job`` / any MODFLOW composer produces a head COG.
    - Model calibration / validation questions in general: any single-band
      MODEL raster vs. any point layer of MEASURED values at the same kind
      of quantity.
    - "Is the model biased high or low, and where?" -- the per-point residual
      map shows spatial bias pattern; the summary stats give a headline
      number.

    **When NOT to use:**
    - Aggregating a raster within polygons (zones) -- use
      ``compute_zonal_statistics``.
    - Running the model itself -- this tool only COMPARES an already-produced
      model raster against observations; it never simulates anything.
    - Comparing two MODELED rasters against each other (no real
      observations) -- that is a model-to-model diff, not a residuals
      calibration check.

    **HONEST UNITS WARNING (read this):** the result ALWAYS carries a
    ``units_warning``. USGS groundwater readings split into DEPTH-TO-WATER
    (pcodes 72019/61055, ft below land surface -- NOT an elevation) and
    ELEVATION-referenced water level (pcodes 72150/62610/62611, ft relative
    to NAVD88/NGVD29 -- directly analogous to a MODFLOW head). A depth-to-
    water reading compared against a head-ELEVATION raster is NOT a valid
    residual without first converting (elevation = land-surface elevation
    minus depth-to-water); this tool detects which family the observations
    belong to (from the ``parameter_code`` USGS carries) and says so
    explicitly. When both families appear in one fetch, it filters to the
    elevation-referenced subset only and notes the drop. Even when semantics
    cannot be confirmed (a generic, non-USGS observations layer), the result
    is still useful for RELATIVE spatial-bias reading -- the warning says
    that too.

    **Parameters:**
    - ``model_layer_uri``: the MODEL raster to evaluate -- a layer handle
      from a prior tool result (preferred) or an ``s3://`` COG URI. Any
      single-band raster (simulated head, plume concentration, etc.).
    - ``observations_layer_uri``: OPTIONAL. An existing point vector layer
      handle/URI of real measurements (e.g. the layer
      ``fetch_usgs_groundwater_levels`` produces). When given, ``bbox`` is
      ignored.
    - ``bbox``: OPTIONAL ``(west, south, east, north)`` in EPSG:4326. Used
      ONLY when ``observations_layer_uri`` is omitted -- this tool then
      fetches USGS groundwater observations itself over ``bbox`` (via
      ``fetch_usgs_groundwater_levels``'s shared core; no separate tool call
      needed). Exactly one of ``observations_layer_uri`` / ``bbox`` must be
      given.
    - ``observed_value_field``: OPTIONAL. The observations-layer property
      holding the measured value. When omitted, auto-detected (tries
      ``water_level`` first -- the ``fetch_usgs_groundwater_levels`` schema
      -- then a short list of common field names). When given, used
      VERBATIM (no auto-detection or pcode filtering).

    **Returns:** ``ModelResidualsLayerURI`` -- a point vector ``LayerURI``
    (FlatGeobuf, EPSG:4326; per-feature ``observed`` / ``simulated`` /
    ``residual``, plus whatever properties the observations layer already
    carried) named ``"Model residuals (<n> points)"``, with a diverging
    red-blue continuous legend centered on zero (``value_field="residual"``),
    plus ``n_points`` / ``mean_error`` / ``bias`` / ``rmse`` / ``mae`` /
    ``min_residual`` / ``max_residual`` / ``units_warning`` (always
    populated) / ``interpretation`` (one-line honest summary) /
    ``small_n_caveat`` (True when ``n_points < 3`` -- stats are still
    returned, just flagged as not statistically meaningful) / ``notes``.

    **Errors (FR-AS-11):** ``ResidualsInputError`` (bad/unreadable inputs, no
    selector given, unresolvable ``observed_value_field``);
    ``ResidualsNoObservationsError`` (zero observation points loaded, or none
    fall inside the raster's footprint); ``ResidualsAllNodataError`` (points
    exist in the footprint but every sample lands on nodata);
    ``ResidualsUpstreamError`` (staging / USGS fetch / write failures).

    Cross-tool dependencies:
        Upstream (consumes):
        - ``run_model_sustainable_yield_scenario`` / ``run_modflow_job`` / any
          MODFLOW composer -- produces the simulated-head ``model_layer_uri``.
        - ``fetch_usgs_groundwater_levels`` -- produces the observed-wells
          ``observations_layer_uri`` (or is called internally via ``bbox``).
        Downstream (feeds):
        - Agent narration reads ``interpretation`` / ``units_warning`` for the
          headline calibration answer.
    """
    if not isinstance(model_layer_uri, str) or not model_layer_uri.strip():
        raise ResidualsInputError(
            f"model_layer_uri must be a non-empty URI string; got {model_layer_uri!r}"
        )
    has_layer = isinstance(observations_layer_uri, str) and observations_layer_uri.strip()
    has_bbox = bbox is not None
    if not has_layer and not has_bbox:
        raise ResidualsInputError(
            "compute_model_residuals requires either observations_layer_uri "
            "(an existing point layer) or bbox (to fetch USGS groundwater "
            "observations directly)."
        )

    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError as exc:
        raise ResidualsUpstreamError(f"rasterio unavailable: {exc}") from exc

    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="trid3nt_model_residuals_") as tmpdir:
        model_local = _stage_uri_local(model_layer_uri, tmpdir, "model")
        try:
            src = rasterio.open(model_local)
        except Exception as exc:  # noqa: BLE001
            raise ResidualsInputError(
                f"could not open model_layer_uri {model_layer_uri!r}: {exc}"
            ) from exc
        try:
            if src.crs is None:
                raise ResidualsInputError(
                    f"model raster {model_layer_uri!r} carries no CRS."
                )
            band = src.read(1).astype(np.float64)
            nodata = src.nodata
            if nodata is not None:
                if isinstance(nodata, float) and math.isnan(nodata):
                    pass  # already NaN-valued in the read array
                elif math.isfinite(float(nodata)):
                    band[band == float(nodata)] = np.nan
            transform = src.transform
            crs = src.crs
            raster_units = (
                src.tags().get("units") or (src.units[0] if src.units else None)
            )
            bbox_4326 = tuple(
                float(v) for v in transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            )
        finally:
            src.close()

        # ---- Load observations. --------------------------------------
        if has_layer:
            gdf = _load_observations_from_uri(observations_layer_uri, tmpdir)  # type: ignore[arg-type]
            notes.append(
                f"Observations from caller-supplied observations_layer_uri "
                f"({observations_layer_uri})."
            )
        else:
            gdf = _fetch_observations_from_bbox(bbox, tmpdir, notes)  # type: ignore[arg-type]

        if len(gdf) == 0:
            raise ResidualsNoObservationsError(
                "the observations source returned zero points -- nothing to compare."
            )
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
            notes.append("Observations carried no CRS; assumed EPSG:4326.")

        geom_types = set(gdf.geometry.geom_type.unique())
        if not geom_types.issubset({"Point"}):
            gdf = gdf.copy()
            gdf["geometry"] = gdf.geometry.centroid
            notes.append(
                f"Non-point observation geometries ({sorted(geom_types - {'Point'})}) "
                "reduced to centroids for raster sampling."
            )

        # ---- Resolve field + honest units/semantics. -------------------
        field = _resolve_observed_field(gdf, observed_value_field)
        if observed_value_field:
            # Verbatim override: skip auto pcode-based filtering, but still
            # attach the generic disclaimer (semantics unverified either way).
            units_warning = _GENERIC_UNITS_WARNING
        else:
            gdf, units_warning = _apply_usgs_semantics(gdf, field, notes)

        if len(gdf) == 0:
            raise ResidualsNoObservationsError(
                "zero observation points remained after filtering to a "
                "consistent reading family -- see notes."
            )

        # ---- Sample the raster (bilinear) at each point. ----------------
        pts = gdf.to_crs(crs)
        xs = np.array([geom.x for geom in pts.geometry], dtype=np.float64)
        ys = np.array([geom.y for geom in pts.geometry], dtype=np.float64)
        in_bounds, simulated = _bilinear_sample(band, transform, xs, ys)

        n_in_footprint = int(in_bounds.sum())
        if n_in_footprint == 0:
            raise ResidualsNoObservationsError(
                f"no observation points fall inside the model raster's "
                f"footprint {tuple(round(v, 4) for v in bbox_4326)} "
                f"({len(gdf)} observation point(s) loaded, none in footprint)."
            )

        observed = _to_float_array(gdf[field].tolist())
        valid = in_bounds & np.isfinite(simulated) & np.isfinite(observed)
        n_valid = int(valid.sum())

        if n_valid == 0:
            raise ResidualsAllNodataError(
                f"{n_in_footprint} observation point(s) fall inside the "
                "raster's footprint, but every sample landed on nodata (or "
                "the observed field was unparseable at every point) -- no "
                "usable residuals."
            )

        n_excluded = n_in_footprint - n_valid
        if n_excluded:
            notes.append(
                f"{n_excluded} in-footprint observation point(s) excluded: "
                "sampled raster nodata, an edge-adjacent unsamplable pixel, "
                f"or an unparseable {field!r} value."
            )
        n_outside = len(gdf) - n_in_footprint
        if n_outside:
            notes.append(
                f"{n_outside} observation point(s) fell outside the model "
                "raster's footprint and were dropped."
            )

        residual = observed[valid] - simulated[valid]
        n_points = n_valid
        small_n_caveat = n_points < 3
        if small_n_caveat:
            notes.append(
                f"Small sample (n={n_points} < 3): summary statistics are "
                "not statistically meaningful with this few points -- treat "
                "as anecdotal; per-point values are still returned."
            )

        mean_error = float(np.mean(residual))
        rmse = float(np.sqrt(np.mean(residual ** 2)))
        mae = float(np.mean(np.abs(residual)))
        min_residual = float(np.min(residual))
        max_residual = float(np.max(residual))

        units = None
        if "unit" in gdf.columns:
            unit_vals = [v for v in gdf.loc[valid, "unit"].tolist() if v]
            units = unit_vals[0] if unit_vals else None
        units = units or raster_units

        if abs(mean_error) < 1e-9:
            interpretation = f"Model shows no net bias on average (n={n_points})."
        else:
            direction = "low" if mean_error > 0 else "high"
            interpretation = (
                f"Model biased {direction} by {abs(mean_error):.4g}"
                f"{(' ' + units) if units else ''} on average (n={n_points})."
            )
        if small_n_caveat:
            interpretation += " CAVEAT: n<3 -- not statistically meaningful."

        # ---- Output vector (EPSG:4326 points). ---------------------------
        out = gdf.loc[valid].copy()
        out["observed"] = observed[valid]
        out["simulated"] = simulated[valid]
        out["residual"] = residual
        if out.crs is None or out.crs.to_epsg() != 4326:
            out = out.to_crs(4326)

        fgb_path = os.path.join(tmpdir, "model_residuals.fgb")
        try:
            out.to_file(fgb_path, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise ResidualsUpstreamError(
                f"model-residuals FlatGeobuf write failed: {exc}"
            ) from exc
        with open(fgb_path, "rb") as f:
            payload = f.read()

    seed = uuid.uuid4().hex[:8]
    uri = _write_output(payload, seed, _output_dir)
    max_abs_residual = max(abs(min_residual), abs(max_residual))

    logger.info(
        "compute_model_residuals: model=%s -> n_points=%d mean_error=%.4g "
        "rmse=%.4g mae=%.4g",
        model_layer_uri,
        n_points,
        mean_error,
        rmse,
        mae,
    )
    return ModelResidualsLayerURI(
        layer_id=f"model-residuals-{seed}",
        name=f"Model residuals ({n_points} points)",
        layer_type="vector",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units=units,
        bbox=tuple(round(float(v), 6) for v in bbox_4326),
        legend=_build_legend(max_abs_residual, units),
        n_points=n_points,
        mean_error=round(mean_error, 6),
        bias=round(mean_error, 6),
        rmse=round(rmse, 6),
        mae=round(mae, 6),
        min_residual=round(min_residual, 6),
        max_residual=round(max_residual, 6),
        units_warning=units_warning,
        interpretation=interpretation,
        small_n_caveat=small_n_caveat,
        notes=notes,
    )
