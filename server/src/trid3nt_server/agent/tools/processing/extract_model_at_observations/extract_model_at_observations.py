"""``extract_model_at_observations`` -- the model-vs-observation pairing primitive.

Samples a MODEL result at OBSERVATION locations/times and writes the ALIGNED
paired table the skill-metric tools (``compute_skill_metrics`` /
``compute_flood_extent_skill``) consume. This is the crucial alignment step:
it reconciles space (exact cell, else nearest wet cell within a tolerance),
time (static max vs surveyed peak, or nearest sample within a tolerance),
VERTICAL DATUM (a NAVD88 vs NGVD29 mismatch it cannot reconcile is a typed
error, never a guess), and physical QUANTITY (a model flood-DEPTH raster vs an
observed water-surface ELEVATION -- e.g. a USGS STN HWM ``elev_ft`` -- is
converted WSE<->depth via a ground-elevation DEM, never paired silently; with
no ground source it is a typed error). It ALWAYS lists every dropped
observation with a per-item reason rather than silently discarding it.

TWO input modes, auto-detected from the model handle:

- STATIC RASTER model (a max-flood-depth / max-WSE / head COG) paired against
  OBSERVATION POINTS (surveyed high-water marks, peak stages): one paired
  sample per point, ``temporal="none_static"`` (model max vs surveyed peak).
- TIME-SERIES model (a point vector layer carrying an inline ``time_series_csv``
  per station, the ``fetch_usgs_nwis_gauges`` / ``fetch_noaa_coops_tides``
  shape) paired against an OBSERVATION time-series layer of the same shape:
  N paired samples per matched station, temporally aligned (exact, else
  nearest model sample within ``time_tolerance_s``).

Output: a FlatGeobuf point layer (EPSG:4326) with columns ``obs_id`` /
``observed`` / ``simulated`` / ``time`` (+ passthrough observation
properties) -- the ``compute_model_residuals`` output shape MINUS the
``residual`` column, on purpose, so the paired table is interoperable. The
returned ``PairedObsLayerURI`` carries the pairing summary: ``n_paired`` /
``n_dropped`` / ``dropped[]`` (per-item reasons) / ``alignment{}`` (spatial /
temporal / datum / crs) / ``units_warning`` (ALWAYS populated) / ``notes``.

``cacheable=False`` (``live-no-cache``): a comparison composer over
caller-supplied handles; the artifact goes to the runs bucket (or
``_output_dir`` for offline tests), mirroring ``compute_model_residuals``.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool

__all__ = [
    "extract_model_at_observations",
    "PairedObsLayerURI",
    "PairingError",
    "PairingInputError",
    "PairingNoPairsError",
    "PairingDatumMismatchError",
    "PairingQuantityMismatchError",
    "PairingUpstreamError",
    "OBSERVED_FIELD_CANDIDATES",
    "OBS_ID_FIELD_CANDIDATES",
    "TIME_FIELD_CANDIDATES",
    "DATUM_FIELD_CANDIDATES",
    "DROP_REASONS",
]

logger = logging.getLogger(
    "trid3nt_server.agent.tools.processing.extract_model_at_observations.extract_model_at_observations"
)


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class PairingError(RuntimeError):
    """Base class for extract_model_at_observations failures."""

    error_code: str = "PAIRED_OBS_ERROR"
    retryable: bool = True


class PairingInputError(PairingError):
    """Bad inputs -- unreadable model/observations, no observed field, empty."""

    error_code = "PAIRED_OBS_INPUT_INVALID"
    retryable = False


class PairingNoPairsError(PairingError):
    """Zero paired samples survived (all points dropped) -- honest, not empty."""

    error_code = "PAIRED_OBS_NO_PAIRS"
    retryable = False


class PairingDatumMismatchError(PairingError):
    """Observation and model vertical datums differ and cannot be reconciled.

    NAVD88 vs NGVD29 (or any declared mismatch) with NO ``datum_shift_m`` is a
    typed error, NEVER a silent guess -- an unreconciled vertical shift makes
    every "residual" meaningless. Supply ``datum_shift_m`` to reconcile.
    """

    error_code = "PAIRED_OBS_DATUM_MISMATCH"
    retryable = False


class PairingQuantityMismatchError(PairingError):
    """Model and observation measure DIFFERENT physical quantities and no

    ground-elevation source is available to reconcile them. A model
    flood-DEPTH raster (height above ground) vs an observed WATER-SURFACE
    ELEVATION (height above a vertical datum) are NOT directly comparable --
    pairing them produces a residual dominated by ground elevation (the
    ~15 m Harvey false-error). Converting WSE<->depth requires sampling a
    ground-elevation DEM; when none is supplied (``ground_elevation_uri``) and
    none can be fetched, this typed error is raised NAMING both quantities,
    NEVER a silent apples-vs-oranges pairing.
    """

    error_code = "PAIRED_OBS_QUANTITY_MISMATCH"
    retryable = False


class PairingUpstreamError(PairingError):
    """Input staging or the paired-table write failed (retryable)."""

    error_code = "PAIRED_OBS_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the pairing side-channel.
# ---------------------------------------------------------------------------


class PairedObsLayerURI(LayerURI):
    """The paired-table point ``LayerURI`` plus the alignment summary.

    Extra fields beyond ``LayerURI`` (all match build-contract section 3.3):

    - ``paired_table_uri`` -- the handle lane B consumes (== ``uri``).
    - ``n_paired`` / ``n_dropped`` -- kept vs dropped observation counts.
    - ``dropped`` -- list of ``{obs_id, reason}``; reason in ``DROP_REASONS``.
    - ``alignment`` -- ``{spatial, temporal, datum, crs}`` provenance, plus
      ``model_quantity`` / ``observed_quantity`` and (when a WSE<->depth
      conversion ran) ``quantity_conversion`` + ``ground_source``.
    - ``columns`` -- the output feature columns.
    - ``units_warning`` -- ALWAYS populated (datum/units/quantity honesty).
    - ``flags`` -- non-fatal per-point honesty flags kept in the pairs (e.g.
      ``negative_depth`` where the sampled ground sat above the observed water
      surface); ``[]`` when none. Flagged points are KEPT, never clamped.
    - ``notes`` -- provenance + per-step detail.
    """

    paired_table_uri: str = ""
    n_paired: int = 0
    n_dropped: int = 0
    dropped: list[dict[str, Any]] = []
    alignment: dict[str, Any] = {}
    columns: list[str] = []
    units_warning: str = ""
    flags: list[dict[str, Any]] = []
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Per-item drop reasons (build-contract section 3.3).
DROP_REASONS: tuple[str, ...] = (
    "outside_footprint",
    "nodata_sample",
    "unparseable_value",
    "no_time_match",
    "crs_reproject_failed",
)

#: Observed-value field auto-detection order (HWM ``elev_ft`` first -- the
#: dominant surveyed-peak source this primitive pairs).
OBSERVED_FIELD_CANDIDATES: tuple[str, ...] = (
    "observed",
    "elev_ft",
    "elev_m",
    "elev",
    "water_level",
    "peak_stage",
    "stage_ft",
    "gage_height_ft",
    "head",
    "observed_value",
    "obs_value",
    "value",
    "elevation",
    "discharge_cfs",
)

#: Exact foot per metre (used to convert a feet-unit observed field into the
#: model's metre reference at ingestion -- the feet-vs-metres pairing bug).
_FT_TO_M = 0.3048

#: Observed fields whose NAME declares FEET (converted ft->m x0.3048).
_FEET_OBS_FIELDS: frozenset[str] = frozenset(
    {"elev_ft", "stage_ft", "gage_height_ft"}
)
#: Observed fields whose NAME declares / implies METRES (no conversion). The
#: generic already-aligned names (``observed``/``value``/...) are treated as the
#: model's metre reference -- they carry no unit tell and are the interop
#: columns a caller pre-aligns.
_METER_OBS_FIELDS: frozenset[str] = frozenset(
    {"elev_m", "elev", "elevation", "head", "observed", "observed_value",
     "obs_value", "value"}
)

#: Observation-id field auto-detection order.
OBS_ID_FIELD_CANDIDATES: tuple[str, ...] = (
    "obs_id",
    "hwm_id",
    "site_no",
    "station_id",
    "gauge_id",
    "id",
)

#: Per-feature timestamp field auto-detection order.
TIME_FIELD_CANDIDATES: tuple[str, ...] = (
    "time",
    "datetime",
    "date_time",
    "timestamp",
    "survey_date",
    "peak_date",
    "reading_dt",
)

#: Vertical-datum field auto-detection order (USGS + STN schemas).
DATUM_FIELD_CANDIDATES: tuple[str, ...] = (
    "vertical_datum",
    "verticalDatumName",
    "vdatum",
    "datum",
)

_STYLE_PRESET = "model_obs_pairs"

_METADATA = AtomicToolMetadata(
    name="extract_model_at_observations",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Staging + coercion helpers.
# ---------------------------------------------------------------------------


def _stage_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local path for ``uri`` (s3:// download or a local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.agent.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise PairingUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise PairingInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise PairingInputError(f"{label} uri points at a missing file: {uri!r}")
    return uri


def _to_float(v: Any) -> float:
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _to_float_array(values: Any) -> np.ndarray:
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        out[i] = _to_float(v)
    return out


# ---------------------------------------------------------------------------
# Field resolution.
# ---------------------------------------------------------------------------


def _resolve_field(
    gdf: Any, override: str | None, candidates: tuple[str, ...], *, numeric: bool
) -> str | None:
    """Resolve a column: caller override (verbatim) else the first candidate.

    ``numeric=True`` also requires the candidate to carry at least one finite
    value. Returns ``None`` when nothing matches (the caller decides if that
    is fatal). An override naming a missing column is a hard input error.
    """
    if override:
        if override not in gdf.columns:
            cols = sorted(c for c in gdf.columns if c != "geometry")
            raise PairingInputError(
                f"field {override!r} not found on the observations layer; "
                f"available columns: {cols}"
            )
        return override
    for cand in candidates:
        if cand in gdf.columns:
            if not numeric or np.isfinite(_to_float_array(gdf[cand])).any():
                return cand
    return None


def _detect_datum(gdf: Any) -> str | None:
    """Return the first non-empty vertical-datum label on the layer, or None."""
    for cand in DATUM_FIELD_CANDIDATES:
        if cand in gdf.columns:
            for v in gdf[cand].tolist():
                if v is not None and str(v).strip():
                    return str(v).strip()
    return None


def _resolve_observed_units(
    field: str, override: str | None
) -> tuple[str, float, str]:
    """Resolve the observed field's unit -> ``(unit_label, factor_to_m, note)``.

    ``factor_to_m`` multiplies observed values into METRES (the model raster's
    unit). Resolution order: caller ``observed_units`` override, else infer
    from the field NAME (``elev_ft``/``*_ft`` -> feet; ``elev_m``/``elev``/
    ``head``/generic aligned names -> metres). A field whose unit CANNOT be
    determined is a typed ``PairingInputError`` -- never a silent guess, since
    pairing a feet-unit observation against a metre-unit model is the
    number-one silent unit-mismatch bug.
    """
    if override:
        u = override.strip().lower()
        if u in ("ft", "feet", "foot"):
            return "feet", _FT_TO_M, f"obs {field} declared feet, converted ft->m x{_FT_TO_M:g}"
        if u in ("m", "meter", "meters", "metre", "metres"):
            return "meters", 1.0, f"obs {field} declared meters (no conversion)"
        raise PairingInputError(
            f"observed_units={override!r} is not a recognized length unit; "
            "use 'feet'/'ft' or 'meters'/'m'."
        )
    name = field.strip().lower()
    if name in _FEET_OBS_FIELDS or name.endswith(("_ft", "_feet")):
        return "feet", _FT_TO_M, f"obs {field} converted ft->m x{_FT_TO_M:g}"
    if name in _METER_OBS_FIELDS or name.endswith(("_m", "_meter", "_metre", "_meters", "_metres")):
        return "meters", 1.0, f"obs {field} already in meters (no conversion)"
    raise PairingInputError(
        f"cannot determine the unit of the observed field {field!r}; the model "
        "raster is in metres, so pairing an observed value of unknown unit "
        "would risk a silent feet-vs-metres mismatch. Pass observed_units="
        "'feet' or observed_units='meters' explicitly."
    )


# ---------------------------------------------------------------------------
# Quantity semantics (the WSE-vs-depth honesty core -- same class as ft->m).
# ---------------------------------------------------------------------------
#
# The number-two silent killer after datum/units: pairing a model flood-DEPTH
# raster (height above GROUND) against an observed WATER-SURFACE ELEVATION
# (height above a vertical DATUM, e.g. a USGS STN HWM elev_ft). Their
# difference is dominated by ground elevation (~15 m in the Harvey L2 run), so
# a "residual" is meaningless. We classify each side into a physical FAMILY
# and, on an elevation-vs-depth cross, CONVERT via a ground-elevation DEM
# rather than pair silently.

#: A raster-tag / band-tag key a producer MAY stamp to declare the quantity
#: explicitly (forward-compatible; the flood workflow does not stamp it today,
#: so the filename heuristic below is the live path).
_QUANTITY_TAG_KEYS: tuple[str, ...] = ("quantity", "QUANTITY", "measured_quantity")

#: Observation-layer column carrying a stamped quantity (fetch_high_water_marks
#: writes ``quantity="water_surface_elevation"`` per feature).
_OBS_QUANTITY_COLUMN = "quantity"


def _classify_quantity(name: str | None) -> tuple[str | None, str | None]:
    """Classify a quantity NAME -> ``(family, label)``.

    ``family`` is ``"depth"`` (height above ground) or ``"elevation"`` (height
    above a vertical datum -- WSE / head / bare elevation), or ``None`` when the
    name carries no quantity tell (never guessed). ``label`` is a canonical
    display string. The depth keywords are tested FIRST so ``flood_depth_peak``
    classifies as depth even though it also names water.
    """
    n = (name or "").strip().lower()
    if not n:
        return None, None
    if any(k in n for k in ("depth", "height_above_gnd", "water_depth", "hmax",
                            "depth_above_ground")):
        return "depth", "depth_above_ground"
    if any(k in n for k in ("water_surface_elevation", "wse", "water_surface",
                            "elev", "head")):
        return "elevation", ("head" if "head" in n else "water_surface_elevation")
    return None, None


def _resolve_model_quantity(
    tags: dict[str, Any], model_uri: str, override: str | None
) -> tuple[str | None, str | None, str]:
    """Resolve the MODEL raster quantity -> ``(family, label, note)``.

    Order: caller ``model_quantity`` override -> a stamped raster tag (forward
    compat) -> the raster filename heuristic (``flood_depth_peak`` -> depth).
    Unknown (``None`` family) is honest -- the caller may pass ``model_quantity``.
    """
    if override:
        fam, label = _classify_quantity(override)
        if fam is None:
            raise PairingInputError(
                f"model_quantity={override!r} is not a recognized quantity; use a "
                "depth-like ('depth'/'flood_depth'/'depth_above_ground') or "
                "elevation-like ('wse'/'water_surface_elevation'/'head') name."
            )
        return fam, label, f"model quantity = {label} (caller model_quantity={override!r})"
    for key in _QUANTITY_TAG_KEYS:
        v = tags.get(key) if tags else None
        if v:
            fam, label = _classify_quantity(str(v))
            if fam is not None:
                return fam, label, f"model quantity = {label} (raster tag {key}={v!r})"
    base = model_uri.rstrip("/").rsplit("/", 1)[-1]
    fam, label = _classify_quantity(base)
    if fam is not None:
        return fam, label, f"model quantity = {label} (inferred from name {base!r})"
    return None, None, (
        f"model quantity could not be determined from raster tags or the name "
        f"{base!r}; pass model_quantity if it must be reconciled against the "
        "observation quantity."
    )


def _resolve_observed_quantity(
    gdf: Any, observed_col: str, override: str | None
) -> tuple[str | None, str | None, str]:
    """Resolve the OBSERVED quantity -> ``(family, label, note)``.

    Order: caller ``observed_quantity`` override -> the CHOSEN observed field's
    name heuristic (specific) -> the layer-level ``quantity`` stamp column
    (``fetch_high_water_marks`` writes ``water_surface_elevation``). Unknown is
    honest -- never guessed.
    """
    if override:
        fam, label = _classify_quantity(override)
        if fam is None:
            raise PairingInputError(
                f"observed_quantity={override!r} is not a recognized quantity; use "
                "a depth-like or elevation-like (WSE/head) name."
            )
        return fam, label, f"observed quantity = {label} (caller observed_quantity={override!r})"
    fam, label = _classify_quantity(observed_col)
    if fam is not None:
        return fam, label, f"observed quantity = {label} (from field name {observed_col!r})"
    if _OBS_QUANTITY_COLUMN in getattr(gdf, "columns", []):
        for v in gdf[_OBS_QUANTITY_COLUMN].tolist():
            if v is not None and str(v).strip():
                fam2, label2 = _classify_quantity(str(v))
                if fam2 is not None:
                    return fam2, label2, (
                        f"observed quantity = {label2} (layer quantity stamp {str(v)!r})"
                    )
                break
    return None, None, (
        f"observed quantity could not be determined from the field name "
        f"{observed_col!r} or a layer quantity stamp; pass observed_quantity if "
        "it must be reconciled against the model quantity."
    )


def _fetch_ground_dem(
    bbox_4326: tuple[float, float, float, float], tmpdir: str
) -> tuple[str, str]:
    """Fetch a ground-elevation DEM for the AOI via ``fetch_dem`` (module seam).

    Returns ``(local_path, source_label)``. Raises on any failure; the caller
    turns a failure into a typed ``PairingQuantityMismatchError`` (never a
    silent mismatched pairing). Patched in offline tests -- the committed suite
    never calls the live 3DEP/GLO-30 fetch.
    """
    # ADR 0097: fetch_dem is spec-driven -- resolve the promoted closure (keyword-only).
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    fetch_dem = TOOL_REGISTRY["fetch_dem"].fn
    layer = fetch_dem(bbox=tuple(bbox_4326))
    local = _stage_local(layer.uri, tmpdir, "ground_dem")
    label = f"3DEP/GLO-30 ground DEM via fetch_dem ({layer.uri}; meters above NAVD88)"
    fb = getattr(layer, "fallback_note", None)
    if fb:
        label += f" [{fb}]"
    return local, label


def _sample_ground_elev(dem_local: str, gdf: Any) -> np.ndarray:
    """Bilinear-sample the ground DEM at each observation point (NaN off-extent).

    Returns a float array aligned with ``gdf`` row order; NaN where the point is
    outside the DEM or lands on nodata (that point cannot be depth-converted and
    is dropped ``nodata_sample`` by the caller).
    """
    import rasterio

    with rasterio.open(dem_local) as src:
        band = src.read(1).astype(np.float64)
        nodata = src.nodata
        if nodata is not None:
            if isinstance(nodata, float) and math.isnan(nodata):
                pass
            elif math.isfinite(float(nodata)):
                band[band == float(nodata)] = np.nan
        transform = src.transform
        crs = src.crs
        pts = gdf.to_crs(crs) if crs is not None else gdf
        xs = np.array([g.x for g in pts.geometry], dtype=np.float64)
        ys = np.array([g.y for g in pts.geometry], dtype=np.float64)
        in_bounds, samples = _bilinear_sample(band, transform, xs, ys)
    samples = np.asarray(samples, dtype=np.float64)
    samples[~np.asarray(in_bounds, dtype=bool)] = np.nan
    return samples


# ---------------------------------------------------------------------------
# Vertical-datum reconciliation (the honesty core).
# ---------------------------------------------------------------------------


def _reconcile_datum(
    obs_datum: str | None,
    model_datum: str | None,
    datum_shift_m: float | None,
    notes: list[str],
) -> tuple[float, str, str]:
    """Reconcile obs vs model vertical datum.

    Returns ``(shift_m, alignment_datum, units_warning)``; ``units_warning`` is
    NEVER empty. Raises ``PairingDatumMismatchError`` when the two datums are
    declared, differ, and no ``datum_shift_m`` was supplied.

    ``shift_m`` is ADDED to every observed value to bring it into the model's
    vertical reference.
    """
    obs_u = obs_datum.upper() if obs_datum else None
    mod_u = model_datum.upper() if model_datum else None

    if datum_shift_m is not None:
        notes.append(
            f"Applied caller-supplied datum_shift_m={datum_shift_m:+.4f} m to "
            f"observed values ({obs_datum or 'obs'} -> {model_datum or 'model'})."
        )
        return (
            float(datum_shift_m),
            f"{obs_datum or 'obs'} -> {model_datum or 'model'} "
            f"(shift {datum_shift_m:+.4f} m applied)",
            f"Observed values were shifted {datum_shift_m:+.4f} m to reconcile "
            f"{obs_datum or 'the observation datum'} onto "
            f"{model_datum or 'the model datum'} (caller-supplied "
            "datum_shift_m). Verify the shift value is correct for this area.",
        )

    if obs_u and mod_u:
        if obs_u == mod_u:
            return (
                0.0,
                obs_datum,  # type: ignore[return-value]
                f"Observations and model share vertical datum {obs_datum}; no "
                "shift applied.",
            )
        raise PairingDatumMismatchError(
            f"observation vertical datum {obs_datum!r} does not match the model "
            f"vertical datum {model_datum!r}, and no datum_shift_m was supplied. "
            "An unreconciled vertical-datum mismatch (e.g. NAVD88 vs NGVD29) "
            "makes every paired residual meaningless. Supply datum_shift_m "
            "(model_datum minus obs_datum, in metres) to reconcile them."
        )

    if obs_u and not mod_u:
        return (
            0.0,
            "assumed_match",
            f"Observations are referenced to {obs_datum}; the model vertical "
            "datum was NOT provided (pass model_datum). Assuming the datums "
            "match; if they differ, pass datum_shift_m. Pairs remain valid for "
            "RELATIVE spatial bias regardless.",
        )
    if mod_u and not obs_u:
        return (
            0.0,
            "assumed_match",
            f"Model datum is {model_datum}; the observations carry no "
            "vertical-datum metadata. Assuming a matching datum; confirm before "
            "treating pairs as absolute error.",
        )
    return (
        0.0,
        "assumed_match",
        "Neither observations nor model declared a vertical datum; assuming a "
        "match. Confirm the datums align before treating pairs as absolute "
        "error (they remain valid for RELATIVE spatial bias).",
    )


# ---------------------------------------------------------------------------
# Raster sampling (mode A): bilinear + nearest-wet-cell fallback.
# ---------------------------------------------------------------------------


def _meters_per_unit(crs: Any, lat_deg: float) -> float:
    """Approximate metres per CRS linear unit for a nearest-cell tolerance.

    Projected metre CRS -> 1.0; geographic degrees -> ~111320 m/deg (latitude
    scaling on the lon axis is folded into the pixel search window, so this is
    a deliberately conservative single scalar). Used ONLY to size the
    nearest-wet-cell search radius; the tolerance is stated in the alignment
    block so the approximation is transparent.
    """
    try:
        if crs is not None and crs.is_geographic:
            return 111320.0
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _bilinear_sample(
    band: np.ndarray, transform: Any, xs: np.ndarray, ys: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear-sample ``band`` (NaN nodata) at world coords; NaN off-extent."""
    from scipy.ndimage import map_coordinates

    inv = ~transform
    cols, rows = inv * (np.asarray(xs, np.float64), np.asarray(ys, np.float64))
    h, w = band.shape
    in_bounds = (cols >= 0) & (cols <= w) & (rows >= 0) & (rows <= h)
    coords = np.vstack([rows - 0.5, cols - 0.5])
    samples = map_coordinates(band, coords, order=1, mode="constant", cval=np.nan)
    return in_bounds, samples


def _nearest_wet_sample(
    band: np.ndarray, transform: Any, x: float, y: float, radius_px: int
) -> tuple[float, float]:
    """Nearest finite (wet) cell value within ``radius_px`` pixels of (x, y).

    Returns ``(value, pixel_distance)``; ``(nan, inf)`` if none is found in the
    window. Used when the exact-cell bilinear sample lands on nodata (a dry
    cell at the observation location -- common at a shoreline HWM).
    """
    inv = ~transform
    col, row = inv * (x, y)
    ci, ri = int(math.floor(col)), int(math.floor(row))
    h, w = band.shape
    best_val = float("nan")
    best_d = float("inf")
    for dr in range(-radius_px, radius_px + 1):
        rr = ri + dr
        if rr < 0 or rr >= h:
            continue
        for dc in range(-radius_px, radius_px + 1):
            cc = ci + dc
            if cc < 0 or cc >= w:
                continue
            v = band[rr, cc]
            if not math.isfinite(v):
                continue
            d = math.hypot((cc + 0.5) - col, (rr + 0.5) - row)
            if d < best_d:
                best_d = d
                best_val = float(v)
    return best_val, best_d


# ---------------------------------------------------------------------------
# Output write (runs bucket, or _output_dir for offline tests).
# ---------------------------------------------------------------------------


def _write_paired_fgb(gdf: Any, seed: str, output_dir: str | None) -> str:
    """Persist the paired FlatGeobuf; return its URI (local test / runs live)."""
    filename = "paired.fgb"
    tmp = tempfile.mkdtemp(prefix="trid3nt_paired_")
    fgb_path = os.path.join(tmp, filename)
    try:
        gdf.to_file(fgb_path, driver="FlatGeobuf", engine="pyogrio")
        with open(fgb_path, "rb") as f:
            payload = f.read()
    except Exception as exc:  # noqa: BLE001
        raise PairingUpstreamError(
            f"paired-table FlatGeobuf write failed: {exc}"
        ) from exc

    if output_dir is not None:
        path = os.path.join(output_dir, f"model-obs-pairs-{seed}.fgb")
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )

        bucket = _get_runs_bucket()
        key = f"model-obs-pairs-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        return f"s3://{bucket}/{key}"
    except PairingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PairingUpstreamError(
            f"failed to upload the paired-table FGB to the runs bucket: {exc}"
        ) from exc


def _records_bbox(
    lons: list[float], lats: list[float]
) -> tuple[float, float, float, float]:
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.02
        east += 0.02
    if south == north:
        south -= 0.02
        north += 0.02
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# Observation loading (shared).
# ---------------------------------------------------------------------------


def _load_points(uri: str, tmpdir: str, label: str) -> Any:
    import geopandas as gpd

    local = _stage_local(uri, tmpdir, label)
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise PairingInputError(
            f"could not open {label} layer {uri!r}: {exc}"
        ) from exc
    if len(gdf) == 0:
        raise PairingInputError(f"{label} layer {uri!r} carries zero features.")
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    geom_types = set(gdf.geometry.geom_type.unique())
    if not geom_types.issubset({"Point"}):
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.centroid
    return gdf


# ---------------------------------------------------------------------------
# Mode A -- static raster model vs observation points.
# ---------------------------------------------------------------------------


def _pair_raster_static(
    model_local: str,
    model_uri: str,
    obs_uri: str,
    observed_value_field: str | None,
    obs_id_field: str | None,
    time_field: str | None,
    model_datum: str | None,
    datum_shift_m: float | None,
    observed_units: str | None,
    model_quantity: str | None,
    observed_quantity: str | None,
    ground_elevation_uri: str | None,
    nearest_wet_tolerance_m: float,
    tmpdir: str,
    notes: list[str],
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], str, list[dict[str, Any]]]:
    """Pair a single-band raster (model max/head) against observation points.

    Returns ``(out_gdf, dropped, alignment, units_warning, flags)``.
    """
    import rasterio
    from rasterio.warp import transform_bounds  # noqa: F401

    try:
        src = rasterio.open(model_local)
    except Exception as exc:  # noqa: BLE001
        raise PairingInputError(f"could not open model raster: {exc}") from exc
    try:
        if src.crs is None:
            raise PairingInputError("model raster carries no CRS.")
        band = src.read(1).astype(np.float64)
        nodata = src.nodata
        if nodata is not None:
            if isinstance(nodata, float) and math.isnan(nodata):
                pass
            elif math.isfinite(float(nodata)):
                band[band == float(nodata)] = np.nan
        transform = src.transform
        crs = src.crs
        model_crs_str = str(crs.to_string()) if crs else "unknown"
        px = abs(transform.a)
        py = abs(transform.e)
        pixel_size_units = max(px, py) or 1.0
        model_tags: dict[str, Any] = dict(src.tags() or {})
        try:
            model_tags.update(src.tags(1) or {})
        except Exception:  # noqa: BLE001
            pass
    finally:
        src.close()

    gdf = _load_points(obs_uri, tmpdir, "observations")
    notes.append(f"Observations: {len(gdf)} point(s) from {obs_uri}.")

    observed_col = _resolve_field(
        gdf, observed_value_field, OBSERVED_FIELD_CANDIDATES, numeric=True
    )
    if observed_col is None:
        cols = sorted(c for c in gdf.columns if c != "geometry")
        raise PairingInputError(
            "could not auto-detect an observed-value field (tried "
            f"{list(OBSERVED_FIELD_CANDIDATES)}); pass observed_value_field. "
            f"Available columns: {cols}"
        )
    obs_id_col = _resolve_field(gdf, obs_id_field, OBS_ID_FIELD_CANDIDATES, numeric=False)
    time_col = _resolve_field(gdf, time_field, TIME_FIELD_CANDIDATES, numeric=False)

    # Unit reconciliation: the model raster is in metres, so a feet-unit
    # observed field is converted ft->m x0.3048 at INGESTION (before any datum
    # shift, which is itself in metres). A field of undeterminable unit is a
    # typed error, never a silent feet-vs-metres guess.
    obs_unit_label, obs_factor, units_note = _resolve_observed_units(
        observed_col, observed_units
    )
    notes.append(units_note + ".")

    obs_datum = _detect_datum(gdf)
    shift_m, alignment_datum, units_warning = _reconcile_datum(
        obs_datum, model_datum, datum_shift_m, notes
    )

    # --- Quantity compatibility (WSE-vs-depth honesty) ------------------
    # Determine each side's physical FAMILY (depth above ground vs elevation
    # above a datum). On an elevation-vs-depth cross, convert the observation to
    # the model's family via a ground-elevation DEM; when neither side's family
    # is known we proceed but WARN (cannot assert a mismatch we cannot see);
    # when they cross and NO ground source is available we raise a typed
    # QuantityMismatchError -- never a silent apples-vs-oranges pairing.
    model_fam, model_qlabel, model_qnote = _resolve_model_quantity(
        model_tags, model_uri, model_quantity
    )
    obs_fam, obs_qlabel, obs_qnote = _resolve_observed_quantity(
        gdf, observed_col, observed_quantity
    )
    notes.append(model_qnote)
    notes.append(obs_qnote)

    if model_fam is not None and obs_fam is not None:
        compat = "match" if model_fam == obs_fam else "convert"
    else:
        compat = "unverified"

    ground: np.ndarray | None = None
    ground_source: str | None = None
    convert_to_depth = False
    if compat == "convert":
        # elevation-vs-depth -> reconcile via a ground-elevation DEM.
        convert_to_depth = model_fam == "depth"
        g4326 = gdf.to_crs(4326)
        gb = g4326.total_bounds  # (minx, miny, maxx, maxy)
        obs_bbox_4326 = (float(gb[0]), float(gb[1]), float(gb[2]), float(gb[3]))
        dem_local: str | None = None
        if ground_elevation_uri:
            dem_local = _stage_local(ground_elevation_uri, tmpdir, "ground_dem")
            ground_source = f"caller-supplied ground DEM ({ground_elevation_uri})"
        else:
            try:
                dem_local, ground_source = _fetch_ground_dem(obs_bbox_4326, tmpdir)
            except Exception as exc:  # noqa: BLE001
                logger.info("ground DEM auto-fetch failed for quantity conversion: %s", exc)
                dem_local = None
        if dem_local is None:
            raise PairingQuantityMismatchError(
                f"model quantity is {model_qlabel!r} (a {model_fam}) but the "
                f"observed quantity is {obs_qlabel!r} (an {obs_fam}); a "
                "water-surface ELEVATION and a DEPTH above ground are NOT "
                "directly comparable (their difference is dominated by ground "
                "elevation). Reconciling them requires sampling a ground-"
                "elevation DEM, but none was supplied (ground_elevation_uri) and "
                "none could be fetched for the AOI. Pass ground_elevation_uri "
                "(a DEM COG covering the observations) to convert WSE<->depth."
            )
        ground = _sample_ground_elev(dem_local, gdf)

    # Reproject to the model CRS for sampling (record any per-point failure).
    try:
        pts = gdf.to_crs(crs)
    except Exception as exc:  # noqa: BLE001
        raise PairingUpstreamError(
            f"could not reproject observations to the model CRS {model_crs_str}: {exc}"
        ) from exc

    xs = np.array([g.x for g in pts.geometry], dtype=np.float64)
    ys = np.array([g.y for g in pts.geometry], dtype=np.float64)
    in_bounds, simulated = _bilinear_sample(band, transform, xs, ys)
    observed = _to_float_array(gdf[observed_col].tolist())

    m_per_unit = _meters_per_unit(crs, float(np.nanmean(ys)) if len(ys) else 0.0)
    pixel_size_m = pixel_size_units * m_per_unit
    radius_px = int(math.ceil(nearest_wet_tolerance_m / pixel_size_m)) if pixel_size_m > 0 else 0
    radius_px = max(0, min(radius_px, 12))  # bound the search cost

    kept_rows: list[int] = []
    kept_sim: list[float] = []
    dropped: list[dict[str, Any]] = []
    n_snapped = 0
    for i in range(len(gdf)):
        oid = _obs_id_for(gdf, obs_id_col, i)
        if not bool(in_bounds[i]):
            dropped.append({"obs_id": oid, "reason": "outside_footprint"})
            continue
        if not math.isfinite(observed[i]):
            dropped.append({"obs_id": oid, "reason": "unparseable_value"})
            continue
        sim = float(simulated[i])
        if not math.isfinite(sim):
            # Exact cell is nodata (dry) -- try the nearest wet cell.
            if radius_px > 0:
                v, d_px = _nearest_wet_sample(band, transform, xs[i], ys[i], radius_px)
                if math.isfinite(v) and d_px * pixel_size_m <= nearest_wet_tolerance_m:
                    sim = v
                    n_snapped += 1
                else:
                    dropped.append({"obs_id": oid, "reason": "nodata_sample"})
                    continue
            else:
                dropped.append({"obs_id": oid, "reason": "nodata_sample"})
                continue
        if ground is not None and not math.isfinite(float(ground[i])):
            # A WSE<->depth conversion is required but the ground DEM has no
            # value at this point -- it cannot be converted, so drop it honestly
            # rather than pair a raw elevation against a depth.
            dropped.append({"obs_id": oid, "reason": "nodata_sample"})
            continue
        kept_rows.append(i)
        kept_sim.append(sim)

    if not kept_rows:
        raise PairingNoPairsError(
            f"no observation point produced a paired sample: {len(dropped)} "
            f"dropped ({_reason_summary(dropped)}). None fell on a wet model "
            "cell within the footprint + tolerance."
        )

    out = gdf.iloc[kept_rows].copy()
    if out.crs is None or out.crs.to_epsg() != 4326:
        out = out.to_crs(4326)
    out["obs_id"] = [_obs_id_for(gdf, obs_id_col, i) for i in kept_rows]

    # Observed into metres (obs_factor) THEN the metre datum shift -> the value
    # in its OWN native quantity (elevation or depth), in the model's datum.
    observed_native = [float(observed[i]) * obs_factor + shift_m for i in kept_rows]

    # Quantity conversion: reconcile observed onto the MODEL's family via the
    # per-point ground elevation. Flag (never clamp) a negative depth (ground
    # sampled ABOVE the observed water surface).
    flags: list[dict[str, Any]] = []
    if ground is not None:
        kept_ground = [float(ground[i]) for i in kept_rows]
        out["ground_elev_m"] = kept_ground
        if convert_to_depth:
            # model=depth, obs=elevation: depth = WSE - ground_elev.
            observed_final = [o - g for o, g in zip(observed_native, kept_ground)]
            conv_desc = (
                f"obs {obs_qlabel} -> depth: depth = observed_WSE - ground_elev "
                "(per-point DEM sample)"
            )
            for oid_v, val in zip(out["obs_id"].tolist(), observed_final):
                if val < 0.0:
                    flags.append({
                        "obs_id": oid_v,
                        "flag": "negative_depth",
                        "depth_m": round(float(val), 4),
                        "detail": "sampled ground elevation sits above the "
                                  "observed water surface; kept, not clamped",
                    })
        else:
            # model=elevation, obs=depth: elevation = depth + ground_elev.
            observed_final = [o + g for o, g in zip(observed_native, kept_ground)]
            conv_desc = (
                f"obs {obs_qlabel} -> elevation: elevation = observed_depth + "
                "ground_elev (per-point DEM sample)"
            )
        out["flag"] = [
            next((f["flag"] for f in flags if f["obs_id"] == oid_v), "")
            for oid_v in out["obs_id"].tolist()
        ]
        out["observed"] = observed_final
        notes.append(
            f"QUANTITY CONVERSION: {conv_desc}; ground source: {ground_source}. "
            f"Assumes observations and the DEM share a vertical datum (fetch_dem "
            f"DEM is NAVD88); pass datum_shift_m if the observations are on a "
            f"different datum. {len(flags)} point(s) flagged negative_depth."
        )
    else:
        observed_final = observed_native
        conv_desc = None
        out["observed"] = observed_final

    out["simulated"] = kept_sim
    out["time"] = [_time_for(gdf, time_col, i) for i in kept_rows]

    spatial = "bilinear_sample_at_point"
    if n_snapped:
        spatial = (
            f"bilinear_sample_at_point (nearest wet cell within "
            f"{nearest_wet_tolerance_m:g} m for {n_snapped} dry-cell point(s))"
        )
        notes.append(
            f"{n_snapped} point(s) snapped to the nearest wet model cell within "
            f"{nearest_wet_tolerance_m:g} m (exact cell was dry/nodata)."
        )
    temporal = "none_static"
    if time_col is not None:
        notes.append(
            f"Model is a STATIC field (max/peak raster); observation timestamps "
            f"from {time_col!r} are carried in the 'time' column but the model "
            "value is the static max (temporal='none_static')."
        )
    alignment = {
        "spatial": spatial,
        "temporal": temporal,
        "datum": alignment_datum,
        "crs": f"{model_crs_str} -> EPSG:4326",
        "units": units_note,
        "model_quantity": model_qlabel or "unknown",
        "observed_quantity": obs_qlabel or "unknown",
    }
    if compat == "convert":
        alignment["quantity_conversion"] = conv_desc
        alignment["ground_source"] = ground_source
        units_warning += (
            f" QUANTITY: model is a {model_fam} ({model_qlabel}) and observations "
            f"are an {obs_fam} ({obs_qlabel}); observations were converted to the "
            f"model quantity via a ground-elevation DEM ({ground_source})."
        )
        if flags:
            units_warning += (
                f" {len(flags)} point(s) yielded a negative depth (ground above "
                "the observed water surface) -- kept and flagged, not clamped."
            )
    elif compat == "unverified":
        alignment["quantity_conversion"] = "none (quantity compatibility unverified)"
        units_warning += (
            f" QUANTITY WARNING: could not confirm the model quantity "
            f"({model_qlabel or 'unknown'}) and observed quantity "
            f"({obs_qlabel or 'unknown'}) are the same physical quantity. If one "
            "is a water-surface ELEVATION and the other a DEPTH above ground, the "
            "pairs are meaningless -- pass model_quantity / observed_quantity (and "
            "ground_elevation_uri) to reconcile them."
        )
    else:  # match
        alignment["quantity_conversion"] = f"none (both {model_fam})"
        units_warning += (
            f" QUANTITY: model and observations are both {model_fam} "
            f"({model_qlabel} vs {obs_qlabel}); no conversion needed."
        )
    return out, dropped, alignment, units_warning, flags


# ---------------------------------------------------------------------------
# Mode B -- time-series model vs time-series observations.
# ---------------------------------------------------------------------------


def _parse_time_series_csv(raw: Any) -> list[tuple[str, float]]:
    """Parse an inline ``"iso,value"`` time_series_csv cell -> [(iso, value)]."""
    out: list[tuple[str, float]] = []
    if not raw:
        return out
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        iso = parts[0].strip()
        v = _to_float(parts[1])
        if iso and math.isfinite(v):
            out.append((iso, v))
    return out


def _pair_timeseries(
    model_local: str,
    obs_uri: str,
    observed_value_field: str | None,
    obs_id_field: str | None,
    time_field: str | None,
    model_datum: str | None,
    datum_shift_m: float | None,
    station_tolerance_m: float,
    time_tolerance_s: float,
    tmpdir: str,
    notes: list[str],
) -> tuple[Any, list[dict[str, Any]], dict[str, Any], str, list[dict[str, Any]]]:
    """Pair a model time-series vector layer against an observation time-series.

    Both layers are point vectors carrying an inline ``time_series_csv``
    (``fetch_usgs_nwis_gauges`` / ``fetch_noaa_coops_tides`` shape). Stations
    are matched by nearest coordinate; each observed timestamp is aligned to
    the exact model sample, else the nearest model sample within
    ``time_tolerance_s``.
    """
    import geopandas as gpd  # noqa: F401
    import datetime as _dt

    model_gdf = _load_points(model_local, tmpdir, "model")
    obs_gdf = _load_points(obs_uri, tmpdir, "observations")
    if "time_series_csv" not in model_gdf.columns:
        raise PairingInputError(
            "time-series model layer carries no 'time_series_csv' column."
        )
    if "time_series_csv" not in obs_gdf.columns:
        raise PairingInputError(
            "time-series observation layer carries no 'time_series_csv' column."
        )

    obs_datum = _detect_datum(obs_gdf)
    shift_m, alignment_datum, units_warning = _reconcile_datum(
        obs_datum, model_datum, datum_shift_m, notes
    )
    obs_id_col = _resolve_field(obs_gdf, obs_id_field, OBS_ID_FIELD_CANDIDATES, numeric=False)

    model_pts = model_gdf.to_crs(4326)
    obs_pts = obs_gdf.to_crs(4326)
    m_lons = np.array([g.x for g in model_pts.geometry], dtype=np.float64)
    m_lats = np.array([g.y for g in model_pts.geometry], dtype=np.float64)
    model_series = [
        _parse_time_series_csv(model_gdf["time_series_csv"].iloc[j])
        for j in range(len(model_gdf))
    ]

    def _parse_iso(s: str) -> float | None:
        try:
            t = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            return t.timestamp()
        except ValueError:
            return None

    tol_deg = station_tolerance_m / 111320.0
    rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    any_nearest = False
    for i in range(len(obs_gdf)):
        oid = _obs_id_for(obs_gdf, obs_id_col, i)
        olon = float(obs_pts.geometry.iloc[i].x)
        olat = float(obs_pts.geometry.iloc[i].y)
        if len(m_lons) == 0:
            dropped.append({"obs_id": oid, "reason": "outside_footprint"})
            continue
        d = np.hypot(m_lons - olon, m_lats - olat)
        j = int(np.argmin(d))
        if float(d[j]) > tol_deg:
            dropped.append({"obs_id": oid, "reason": "outside_footprint"})
            continue
        msamples = model_series[j]
        if not msamples:
            dropped.append({"obs_id": oid, "reason": "nodata_sample"})
            continue
        m_epochs = [(_parse_iso(t), v, t) for t, v in msamples]
        m_epochs = [(e, v, t) for (e, v, t) in m_epochs if e is not None]
        osamples = _parse_time_series_csv(obs_gdf["time_series_csv"].iloc[i])
        if not osamples:
            dropped.append({"obs_id": oid, "reason": "unparseable_value"})
            continue
        matched_here = 0
        for otime, oval in osamples:
            oe = _parse_iso(otime)
            if oe is None:
                dropped.append({"obs_id": f"{oid}@{otime}", "reason": "no_time_match"})
                continue
            best = min(m_epochs, key=lambda t: abs(t[0] - oe))
            dt_s = abs(best[0] - oe)
            if dt_s > time_tolerance_s:
                dropped.append({"obs_id": f"{oid}@{otime}", "reason": "no_time_match"})
                continue
            if dt_s > 0:
                any_nearest = True
            rows.append(
                {
                    "obs_id": oid,
                    "observed": float(oval) + shift_m,
                    "simulated": float(best[1]),
                    "time": otime,
                    "lon": olon,
                    "lat": olat,
                }
            )
            matched_here += 1

    if not rows:
        raise PairingNoPairsError(
            f"no observation sample aligned to a model sample: {len(dropped)} "
            f"dropped ({_reason_summary(dropped)})."
        )

    from shapely.geometry import Point

    out = gpd.GeoDataFrame(
        {
            "obs_id": [r["obs_id"] for r in rows],
            "observed": [r["observed"] for r in rows],
            "simulated": [r["simulated"] for r in rows],
            "time": [r["time"] for r in rows],
        },
        geometry=[Point(r["lon"], r["lat"]) for r in rows],
        crs="EPSG:4326",
    )
    temporal = f"nearest_within_tolerance:{int(time_tolerance_s)}" if any_nearest else "exact"
    alignment = {
        "spatial": f"nearest_station_coordinate (within {station_tolerance_m:g} m)",
        "temporal": temporal,
        "datum": alignment_datum,
        "crs": "EPSG:4326 -> EPSG:4326",
        "station_tolerance_m": station_tolerance_m,
    }
    notes.append(
        f"Time-series pairing: {len(rows)} sample(s) across matched stations "
        f"(temporal={temporal})."
    )
    # Mode B (time-series) pairs like-for-like series; the WSE-vs-depth quantity
    # conversion is a mode-A (static raster) concern, so no flags here.
    return out, dropped, alignment, units_warning, []


# ---------------------------------------------------------------------------
# Small shared helpers.
# ---------------------------------------------------------------------------


def _obs_id_for(gdf: Any, col: str | None, i: int) -> str:
    if col is not None:
        v = gdf[col].iloc[i]
        if v is not None and str(v).strip():
            return str(v)
    return f"OBS-{i}"


def _time_for(gdf: Any, col: str | None, i: int) -> str | None:
    if col is None:
        return None
    v = gdf[col].iloc[i]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    # A date/datetime column (pyogrio auto-parses ISO strings) -> ISO8601.
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:  # noqa: BLE001
            pass
    s = str(v).strip()
    if not s:
        return None
    # Normalize "YYYY-MM-DD HH:MM:SS" (str-cast Timestamp) to ISO8601 with a T.
    if len(s) >= 19 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    return s


def _reason_summary(dropped: list[dict[str, Any]]) -> str:
    from collections import Counter

    c = Counter(d["reason"] for d in dropped)
    return ", ".join(f"{k}={v}" for k, v in sorted(c.items()))


def _looks_like_raster(path: str) -> bool:
    """True if ``path`` opens as a raster (mode A) rather than a vector."""
    import rasterio

    try:
        with rasterio.open(path) as src:
            return src.count >= 1 and src.width > 0 and src.height > 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(_METADATA)
def extract_model_at_observations(
    model_layer_uri: str,
    observations_layer_uri: str,
    observed_value_field: str | None = None,
    obs_id_field: str | None = None,
    time_field: str | None = None,
    model_datum: str | None = None,
    datum_shift_m: float | None = None,
    observed_units: str | None = None,
    nearest_wet_tolerance_m: float = 250.0,
    station_tolerance_m: float = 500.0,
    time_tolerance_s: float = 3600.0,
    model_quantity: str | None = None,
    observed_quantity: str | None = None,
    ground_elevation_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    **_extra_ignored: Any,
) -> PairedObsLayerURI:
    """Pair a MODEL result with OBSERVATIONS -> the aligned paired table for skill metrics.

    Samples a model result at observation locations/times and writes the
    paired table ``compute_skill_metrics`` / ``compute_flood_extent_skill``
    consume. Handles the three alignment axes honestly: space (exact cell,
    else nearest wet cell within a tolerance), time (static max vs surveyed
    peak, or nearest sample within a tolerance), and VERTICAL DATUM (a
    declared NAVD88 vs NGVD29 mismatch with no shift is a typed error, never a
    guess). Every dropped observation is listed with a per-item reason.

    **When to use:**
    - Right BEFORE ``compute_skill_metrics`` / ``compute_flood_extent_skill``:
      you have a model result handle and an observation layer and need the
      aligned observed/simulated pairs.
    - Validate a modeled max-flood-depth / max-WSE raster against surveyed
      high-water marks (``fetch_high_water_marks``) or peak stages.
    - Align a modeled gauge/tide time-series against an observed hydrograph.

    **When NOT to use:**
    - You already have observed + simulated arrays -> call
      ``compute_skill_metrics`` directly (it also accepts arrays).
    - You want residual VALUES + a diverging bias map ->
      ``compute_model_residuals`` (this tool omits the residual on purpose so
      the paired table stays a neutral input).
    - Running the model itself -> the engine ``run_*`` tools.

    **Two modes (auto-detected from the model handle):**
    - STATIC RASTER model (max-depth / WSE / head COG) vs observation POINTS
      -> one pair per point, ``temporal="none_static"``.
    - TIME-SERIES model (point layer with an inline ``time_series_csv``) vs an
      observation time-series layer -> N pairs per matched station, temporally
      aligned (exact, else nearest model sample within ``time_tolerance_s``).

    **Parameters:**
    - ``model_layer_uri``: model result handle -- a raster COG or a
      time-series point layer (s3:// or a prior tool's layer handle).
    - ``observations_layer_uri``: point observation layer handle.
    - ``observed_value_field``: observed column (auto-detected; ``elev_ft`` /
      ``elev_m`` / ``water_level`` tried early).
    - ``obs_id_field`` / ``time_field``: id / timestamp columns (auto-detected).
    - ``model_datum``: the model's vertical datum (e.g. ``"NAVD88"``). Needed
      to reconcile against the observation datum; a declared mismatch with no
      ``datum_shift_m`` raises ``PairingDatumMismatchError``.
    - ``datum_shift_m``: metres ADDED to observed to bring it into the model
      datum (recorded in the alignment block).
    - ``observed_units``: ``"feet"`` / ``"meters"`` (mode A). The model raster
      is metres, so a feet-unit observed field (``elev_ft`` and friends) is
      converted ft->m x0.3048 at ingestion; this overrides the name-based
      inference. A field whose unit cannot be inferred AND has no override is a
      typed ``PairingInputError`` (never a silent feet-vs-metres guess). The
      conversion applied is recorded in ``alignment["units"]``.
    - ``nearest_wet_tolerance_m`` (default 250): dry-cell snap radius (mode A).
    - ``station_tolerance_m`` (default 500): station-match radius (mode B) --
      the max distance a model station may sit from an observation station;
      recorded in the alignment block.
    - ``time_tolerance_s`` (default 3600): temporal match window (mode B).
    - ``model_quantity`` / ``observed_quantity`` (mode A): OPTIONAL explicit
      quantity for each side -- a depth-like (``"depth"`` / ``"flood_depth"`` /
      ``"depth_above_ground"``) or elevation-like (``"wse"`` /
      ``"water_surface_elevation"`` / ``"head"``) label. Overrides the automatic
      resolution (raster tag / filename for the model; the observed field name
      or a layer ``quantity`` stamp for the observations). When BOTH resolve and
      one is a DEPTH while the other is an ELEVATION, the tool converts the
      observations to the model's quantity (see ``ground_elevation_uri``); a
      cross with NO ground source raises ``PairingQuantityMismatchError``.
    - ``ground_elevation_uri`` (mode A): OPTIONAL ground-elevation DEM COG used
      to convert a water-surface ELEVATION observation to a DEPTH (or back):
      ``depth = observed_WSE - ground_elev`` sampled per point. When omitted and
      a conversion is required, the tool auto-fetches a DEM for the AOI
      (``fetch_dem``); if that also fails it raises
      ``PairingQuantityMismatchError`` naming both quantities. The DEM's vertical
      datum (3DEP: NAVD88) is assumed to match the observations' datum -- pass
      ``datum_shift_m`` if not.

    **Returns:** ``PairedObsLayerURI`` -- a FlatGeobuf point layer (EPSG:4326,
    columns ``obs_id`` / ``observed`` / ``simulated`` / ``time`` + passthrough;
    plus ``ground_elev_m`` / ``flag`` when a quantity conversion ran) named
    ``"Model-obs pairs (<n> points)"``, plus ``paired_table_uri`` (the handle
    lane B reads), ``n_paired`` / ``n_dropped`` / ``dropped[]`` (per-item
    reasons), ``alignment`` (``spatial`` / ``temporal`` / ``datum`` / ``crs`` /
    ``model_quantity`` / ``observed_quantity`` and, on a conversion,
    ``quantity_conversion`` + ``ground_source``), ``columns``, ``units_warning``
    (always populated), ``flags`` (e.g. ``negative_depth`` points -- kept, never
    clamped), ``notes``.

    **Errors (FR-AS-11):** ``PairingInputError`` (bad/unreadable inputs, no
    observed field, unrecognized quantity); ``PairingDatumMismatchError``
    (unreconciled vertical datums); ``PairingQuantityMismatchError`` (model
    quantity vs observed quantity cross elevation-vs-depth with no ground
    source); ``PairingNoPairsError`` (every observation dropped);
    ``PairingUpstreamError`` (staging / write failure).

    Cross-tool dependencies:
        Upstream: engine ``run_*`` (model raster / time-series);
        ``fetch_high_water_marks`` / ``fetch_usgs_nwis_gauges`` /
        ``fetch_noaa_coops_tides`` (observations).
        Downstream: ``compute_skill_metrics`` / ``compute_flood_extent_skill``
        read ``paired_table_uri``.
    """
    if not isinstance(model_layer_uri, str) or not model_layer_uri.strip():
        raise PairingInputError(
            f"model_layer_uri must be a non-empty URI string; got {model_layer_uri!r}"
        )
    if not isinstance(observations_layer_uri, str) or not observations_layer_uri.strip():
        raise PairingInputError(
            "observations_layer_uri must be a non-empty URI string; got "
            f"{observations_layer_uri!r}"
        )

    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="trid3nt_extract_obs_") as tmpdir:
        model_local = _stage_local(model_layer_uri, tmpdir, "model")
        if _looks_like_raster(model_local):
            out, dropped, alignment, units_warning, flags = _pair_raster_static(
                model_local,
                model_layer_uri,
                observations_layer_uri,
                observed_value_field,
                obs_id_field,
                time_field,
                model_datum,
                datum_shift_m,
                observed_units,
                model_quantity,
                observed_quantity,
                ground_elevation_uri,
                float(nearest_wet_tolerance_m),
                tmpdir,
                notes,
            )
        else:
            out, dropped, alignment, units_warning, flags = _pair_timeseries(
                model_local,
                observations_layer_uri,
                observed_value_field,
                obs_id_field,
                time_field,
                model_datum,
                datum_shift_m,
                float(station_tolerance_m),
                float(time_tolerance_s),
                tmpdir,
                notes,
            )

        lons = [float(g.x) for g in out.geometry]
        lats = [float(g.y) for g in out.geometry]
        bbox_4326 = _records_bbox(lons, lats)
        columns = [c for c in out.columns if c != "geometry"]
        seed = uuid.uuid4().hex[:8]
        uri = _write_paired_fgb(out, seed, _output_dir)

    n_paired = int(len(out))
    n_dropped = len(dropped)
    if n_dropped:
        notes.append(f"Dropped {n_dropped} observation(s): {_reason_summary(dropped)}.")
    logger.info(
        "extract_model_at_observations: model=%s obs=%s -> n_paired=%d n_dropped=%d "
        "temporal=%s datum=%s",
        model_layer_uri,
        observations_layer_uri,
        n_paired,
        n_dropped,
        alignment.get("temporal"),
        alignment.get("datum"),
    )
    return PairedObsLayerURI(
        layer_id=f"model-obs-pairs-{seed}",
        name=f"Model-obs pairs ({n_paired} points)",
        layer_type="vector",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        bbox=tuple(round(float(v), 6) for v in bbox_4326),
        paired_table_uri=uri,
        n_paired=n_paired,
        n_dropped=n_dropped,
        dropped=dropped,
        alignment=alignment,
        columns=columns,
        units_warning=units_warning,
        flags=flags,
        notes=notes,
    )
