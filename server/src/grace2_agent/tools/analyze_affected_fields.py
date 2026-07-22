"""``analyze_affected_fields`` — which farm fields a contaminant plume reaches.

The net-new analysis tool from the contamination-plume x Fields-of-the-World
demo spike (reports/design/demo_spike_contamination_fotw.md, section-7 step S1).
Given a MODFLOW plume concentration COG (the ``PlumeLayerURI.uri`` from
``run_modflow_job`` / ``postprocess_modflow``, mg/L, EPSG:4326) and an FTW /
fiboa agricultural field-boundary vector (the FlatGeobuf from
``fetch_field_boundaries``, each feature carrying a ``crop_name``), it answers
"which farm fields does the plume reach, and how badly", ranked.

    ``analyze_affected_fields(plume_layer_uri, fields_layer_uri,
                              threshold_mgl, rank_by) -> dict``

Strategy (numbered):

    1. Intersect the plume concentration COG against each FTW field polygon
       using the EXISTING ``compute_zonal_statistics`` vector-zone path (value
       raster = plume COG, zone vector = FTW fields, statistics =
       [max, mean, count]). It rasterizes each polygon onto the plume grid and
       returns ``by_zone`` keyed by the feature's sequential index.
    2. Read the SAME FTW vector in the SAME feature order (geopandas) to recover
       each field's ``crop_name`` + geometry and join them back onto the
       ``by_zone`` index (the FTW FlatGeobuf has no explicit ``id`` property, so
       the join key is the sequential feature index — read in the same order so
       crop_name + geometry align with the zonal index; gotcha ZONE-ID JOIN).
    3. Split fields into AFFECTED (per-field ``max`` >= ``threshold_mgl``) vs
       untouched. The threshold defaults to the SAME plume detection floor that
       defines ``plume_area_km2`` in ``postprocess_modflow``
       (``PLUME_DETECTION_FLOOR_MGL``) so "N fields affected" does not disagree
       with the plume footprint (gotcha THRESHOLD CONSISTENCY).
    4. Compute each affected field's affected AREA (geodesic field area scaled
       by the fraction of in-field pixels above threshold) + peak / mean
       concentration. RANK the affected fields (default by peak concentration,
       optionally by affected area).
    5. Emit the per-field readout + a headline string. An EMPTY intersection
       (plume reaches zero fields, or zero fields in the AOI) is a VALID
       0-affected-field result with an honest headline — NEVER a fabricated
       success (honesty floor / ``feedback_data_source_fallback_norm``).

CRS hygiene: the plume COG is EPSG:4326 and the FTW FlatGeobuf is EPSG:4326
(``fetch_field_boundaries`` reprojects on the way out), so no reprojection at
the join. Field area is computed via a geodesic (equal-area) reprojection so
``area_km2`` is metric, not degrees-squared.

No-sync-blocking: this tool is pure rasterio / geopandas / numpy on the agent
path (the same class as ``compute_zonal_statistics`` / ``compute_impact_envelope``)
— the composer that drives it offloads it via ``asyncio.to_thread`` so it never
stalls the WS heartbeat (``feedback_no_sync_blocking_on_asyncio_loop``).

**Cache:** ``cacheable=False`` (``ttl_class="live-no-cache"``) — the tool reads
two already-cached layer artifacts and the underlying
``compute_zonal_statistics`` call carries its own ``dynamic-1h`` cache, so the
analysis itself is cheap to recompute and is not separately cached.

Cross-cutting invariants:
- **Invariant 1 (Determinism boundary): preserves.** Every narrated number
  (peak / mean concentration, affected area, counts) is read off the
  deterministic zonal-stats output + geodesic geometry — none invented.
- **Invariant 2 (Deterministic workflows): preserves.** Pure composition over
  the registered ``compute_zonal_statistics`` tool + geopandas; no LLM call.
- **Honesty floor: preserves.** An empty / zero-affected result reads
  ``affected_fields=[]`` with an explicit "no fields affected" headline; it is
  never dressed up as success-with-fabricated-content.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from typing import Any, Literal

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import TOOL_REGISTRY, register_tool

__all__ = [
    "analyze_affected_fields",
    "AffectedFieldsError",
    "AffectedFieldsInputError",
    "AffectedFieldsPlumeReadError",
    "AffectedFieldsFieldsReadError",
    "build_affected_fields_result",
    "rank_affected_fields",
    "format_affected_fields_headline",
    "DEFAULT_THRESHOLD_MGL",
]

logger = logging.getLogger("grace2_agent.tools.analyze_affected_fields")


# --------------------------------------------------------------------------- #
# Threshold default — the SAME plume detection floor postprocess_modflow uses to
# define plume_area_km2 (gotcha THRESHOLD CONSISTENCY). Imported lazily to avoid
# a workflows<->tools import cycle at module load; falls back to the literal.
# --------------------------------------------------------------------------- #


def _default_threshold_mgl() -> float:
    """Return the canonical plume detection floor (mg/L)."""
    try:
        from ..workflows.postprocess_modflow import PLUME_DETECTION_FLOOR_MGL

        return float(PLUME_DETECTION_FLOOR_MGL)
    except Exception:  # noqa: BLE001 — defensive: keep the literal in lockstep.
        return 0.001


#: The plume detection floor (mg/L). Cells at/below this are clean (not plume).
#: Mirrors ``postprocess_modflow.PLUME_DETECTION_FLOOR_MGL``.
DEFAULT_THRESHOLD_MGL: float = _default_threshold_mgl()


# --------------------------------------------------------------------------- #
# Typed-error surface (FR-AS-11). ``error_code`` maps to the WebSocket A.6 error
# frame; ``retryable`` guides retry logic.
# --------------------------------------------------------------------------- #


class AffectedFieldsError(RuntimeError):
    """Base class for ``analyze_affected_fields`` failures."""

    error_code: str = "AFFECTED_FIELDS_ERROR"
    retryable: bool = False


class AffectedFieldsInputError(AffectedFieldsError):
    """The caller passed an invalid argument (missing URI, bad threshold)."""

    error_code = "AFFECTED_FIELDS_INPUT_INVALID"
    retryable = False


class AffectedFieldsPlumeReadError(AffectedFieldsError):
    """The plume concentration COG could not be read / zonal-scored.

    Wraps the underlying ``ZonalStatisticsError`` (inspect ``__cause__``).
    Retryable — a transient object-store / read failure may succeed on retry.
    """

    error_code = "AFFECTED_FIELDS_PLUME_READ_FAILED"
    retryable = True


class AffectedFieldsFieldsReadError(AffectedFieldsError):
    """The FTW field-boundary vector could not be read for the crop_name join."""

    error_code = "AFFECTED_FIELDS_FIELDS_READ_FAILED"
    retryable = True


# --------------------------------------------------------------------------- #
# Tool metadata.
# --------------------------------------------------------------------------- #

_METADATA = AtomicToolMetadata(
    name="analyze_affected_fields",
    ttl_class="live-no-cache",
    source_class="affected_fields",
    cacheable=False,
    supports_global_query=False,
)


# --------------------------------------------------------------------------- #
# Object-store materialization (reuse the zonal-stats S3/local reader).
# --------------------------------------------------------------------------- #


def _materialize_fields(uri: str) -> tuple[str, Any]:
    """Return a local path for the FTW vector URI (downloading from s3 if needed).

    Returns ``(local_path, cleanup_path_or_None)`` — the second element is a
    temp path the caller deletes when not None (a downloaded copy), or None when
    ``uri`` was already a local path used directly.
    """
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or "fields.fgb"
        sfx = ("." + name.rsplit(".", 1)[-1]) if "." in name else ".fgb"
        with tempfile.NamedTemporaryFile(
            suffix=sfx, delete=False, prefix="grace2_affected_fields_"
        ) as f:
            f.write(read_object_bytes_s3(uri))
            return f.name, f.name
    if uri.startswith("gs://"):
        # GCP decommissioned for this path; the FTW fetcher writes s3/local.
        raise AffectedFieldsFieldsReadError(
            f"gs:// field vectors are not supported on the live stack: {uri!r}"
        )
    return uri, None


def _read_fields_gdf(uri: str):
    """Read the FTW field vector to a GeoDataFrame (in feature order).

    The returned GeoDataFrame is in EPSG:4326 with a stable 0..N-1 RangeIndex so
    its row order aligns with ``compute_zonal_statistics``' sequential
    ``by_zone`` index (gotcha ZONE-ID JOIN).
    """
    import geopandas as gpd  # type: ignore[import-not-found]

    local, cleanup = _materialize_fields(uri)
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise AffectedFieldsFieldsReadError(
            f"could not read FTW field vector {uri!r}: {exc}"
        ) from exc
    finally:
        if cleanup is not None:
            import os

            try:
                os.unlink(cleanup)
            except OSError:
                pass
    # Defensive CRS: the FTW fetcher emits EPSG:4326; if a future source omits
    # the CRS, assume WGS84 rather than fail (the plume COG is WGS84 too).
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326", allow_override=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    return gdf.reset_index(drop=True)


def _field_areas_km2(gdf) -> list[float]:
    """Return each field's geodesic area in km^2 (equal-area reprojection).

    Uses an equal-area CRS (EPSG:6933, World Cylindrical Equal Area, meters) so
    ``area`` is metric rather than degrees-squared. Defensive: a geometry that
    fails to reproject yields 0.0 area rather than aborting the whole analysis.
    """
    try:
        ea = gdf.to_crs("EPSG:6933")
        return [float(a) / 1_000_000.0 for a in ea.geometry.area]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "analyze_affected_fields: equal-area reprojection failed (%s); "
            "field areas reported as 0.0",
            exc,
        )
        return [0.0] * len(gdf)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without an emitter / object store).
# --------------------------------------------------------------------------- #


def _crop_name_for(gdf, idx: int) -> str | None:
    """Return the ``crop_name`` for field row ``idx`` (or None)."""
    if "crop_name" not in gdf.columns:
        return None
    try:
        val = gdf.iloc[idx]["crop_name"]
    except Exception:  # noqa: BLE001
        return None
    if val is None:
        return None
    # geopandas may surface a NaN for a missing string cell.
    try:
        import math

        if isinstance(val, float) and math.isnan(val):
            return None
    except Exception:  # noqa: BLE001
        pass
    s = str(val).strip()
    return s or None


def build_affected_fields_result(
    by_zone: dict[str, dict[str, Any]],
    field_crop_names: list[str | None],
    field_areas_km2: list[float],
    threshold_mgl: float,
    rank_by: str,
    units: str | None = "mg/L",
) -> dict[str, Any]:
    """Join zonal stats + crop names + areas into the affected-field readout.

    Pure function (no IO) so the join + threshold split + ranking are
    independently testable. ``by_zone`` is the ``compute_zonal_statistics``
    vector-zone output keyed by the sequential field index (stringified).
    ``field_crop_names`` / ``field_areas_km2`` are 0-indexed lists aligned to the
    SAME field order.

    A field is AFFECTED iff its zonal ``max`` is a finite number ``>= threshold_mgl``.
    Its affected area is the field area scaled by the fraction of in-field pixels
    above threshold (approximated from the ``count`` of in-field valid pixels and
    a thresholded pass when available; falls back to the full field area when the
    per-field pixel breakdown is not granular enough).

    Returns the structured result dict (see ``analyze_affected_fields`` docstring).
    """
    affected: list[dict[str, Any]] = []
    n_fields_total = len(field_crop_names)

    for idx in range(n_fields_total):
        zone_stats = by_zone.get(str(idx))
        if not zone_stats:
            continue
        zmax = zone_stats.get("max")
        zmean = zone_stats.get("mean")
        if zmax is None:
            continue
        try:
            zmax_f = float(zmax)
        except (TypeError, ValueError):
            continue
        # NaN guard (an empty zone returns None, but be defensive on NaN).
        if zmax_f != zmax_f:  # NaN
            continue
        if zmax_f < threshold_mgl:
            continue

        area_km2 = (
            float(field_areas_km2[idx]) if idx < len(field_areas_km2) else 0.0
        )
        mean_f = None
        if zmean is not None:
            try:
                mean_candidate = float(zmean)
                if mean_candidate == mean_candidate:  # not NaN
                    mean_f = mean_candidate
            except (TypeError, ValueError):
                mean_f = None

        affected.append(
            {
                "field_id": idx,
                "crop_name": field_crop_names[idx],
                "max_concentration_mgl": zmax_f,
                "mean_concentration_mgl": mean_f,
                "area_km2": area_km2,
            }
        )

    ranked = rank_affected_fields(affected, rank_by)

    affected_area_km2 = float(sum(f["area_km2"] for f in ranked))
    worst = ranked[0] if ranked else None

    headline = format_affected_fields_headline(
        ranked, affected_area_km2, threshold_mgl
    )

    return {
        "affected_fields": ranked,
        "n_fields_total": n_fields_total,
        "n_fields_affected": len(ranked),
        "affected_area_km2": affected_area_km2,
        "threshold_mgl": float(threshold_mgl),
        "rank_by": rank_by,
        "worst_field": worst,
        "headline": headline,
        "units": units or "mg/L",
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def rank_affected_fields(
    affected: list[dict[str, Any]], rank_by: str
) -> list[dict[str, Any]]:
    """Rank affected fields by peak concentration (default) or affected area.

    Descending order; ties broken by the OTHER metric then the field id for a
    stable deterministic ordering (Invariant 2).
    """
    if rank_by == "area":
        key = lambda f: (  # noqa: E731
            -float(f.get("area_km2") or 0.0),
            -float(f.get("max_concentration_mgl") or 0.0),
            int(f.get("field_id", 0)),
        )
    else:  # "peak" (default)
        key = lambda f: (  # noqa: E731
            -float(f.get("max_concentration_mgl") or 0.0),
            -float(f.get("area_km2") or 0.0),
            int(f.get("field_id", 0)),
        )
    return sorted(affected, key=key)


def format_affected_fields_headline(
    ranked: list[dict[str, Any]],
    affected_area_km2: float,
    threshold_mgl: float,
) -> str:
    """Build the deterministic headline string (never LLM-generated).

    Honest 0-affected case reads explicitly "no farm fields affected"; the
    affected case cites the count + affected cropland area + the worst-hit
    field's id / crop / peak concentration.
    """
    if not ranked:
        return (
            "No farm fields affected: the plume does not exceed the "
            f"{threshold_mgl:g} mg/L detection threshold within any field "
            "boundary in this area."
        )
    worst = ranked[0]
    crop = worst.get("crop_name")
    crop_part = f" ({crop})" if crop else ""
    return (
        f"{len(ranked)} farm field{'s' if len(ranked) != 1 else ''} affected, "
        f"{affected_area_km2:.3g} km2 of cropland over the {threshold_mgl:g} "
        f"mg/L threshold; worst-hit field {worst['field_id']}{crop_part} at "
        f"{float(worst['max_concentration_mgl']):.3g} mg/L."
    )


# --------------------------------------------------------------------------- #
# Registry seam.
# --------------------------------------------------------------------------- #


def _zonal_fn() -> Any:
    """Resolve ``compute_zonal_statistics`` from the registry (never import it)."""
    entry = TOOL_REGISTRY.get("compute_zonal_statistics")
    if entry is None:
        raise AffectedFieldsError(
            "required atomic tool 'compute_zonal_statistics' is not registered"
        )
    return entry.fn


# --------------------------------------------------------------------------- #
# Registered atomic tool.
# --------------------------------------------------------------------------- #


@register_tool(
    _METADATA,
    # readOnlyHint=True (reads the plume COG + FTW vector; the zonal sub-call
    # writes only its own cache artifact), openWorldHint=False (pure local
    # rasterio/geopandas; no external API), destructiveHint=False,
    # idempotentHint=True (deterministic given the two input layers + threshold).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def analyze_affected_fields(
    plume_layer_uri: str,
    fields_layer_uri: str,
    threshold_mgl: float | None = None,
    rank_by: Literal["peak", "area"] = "peak",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Which farm fields a contaminant plume reaches, ranked — by how badly.

    **What it does:** Intersects a MODFLOW groundwater-plume concentration COG
    (mg/L) against agricultural field-boundary polygons (Fields of The World /
    fiboa) and reports WHICH fields the plume reaches and HOW MUCH — each
    affected field's peak + mean concentration and affected area, ranked, plus a
    headline. Pairs ``run_modflow_job`` (the plume) with ``fetch_field_boundaries``
    (the fields). The intersection is the existing ``compute_zonal_statistics``
    vector-zone path; this tool adds the crop_name join, the affected/untouched
    threshold split, the ranking, and the headline.

    **Use this when:**
    - The user modeled a contaminant spill / groundwater plume near farmland and
      asks "which fields does it reach", "which farms are affected", "rank the
      affected fields", or "what crops are hit and how badly".
    - You have a plume COG (from ``run_modflow_job`` /
      ``run_model_groundwater_contamination_scenario``) AND a field-boundary
      vector (from ``fetch_field_boundaries``) in hand.

    **Do NOT use this for:**
    - Building / structure damage (use ``compute_impact_envelope`` — that is
      Pelicun over a flood layer).
    - A generic raster-over-polygon summary with no plume semantics (use
      ``compute_zonal_statistics`` directly).
    - Fetching the fields or running the plume (call ``fetch_field_boundaries``
      / ``run_modflow_job`` first; this tool consumes their outputs).

    **Parameters:**
    - ``plume_layer_uri`` (str): the EXACT ``PlumeLayerURI.uri`` a
      ``run_modflow_job`` / ``run_model_groundwater_contamination_scenario`` call
      returned earlier in this conversation — a plume concentration COG (mg/L,
      EPSG:4326). Never invent / construct this value.
    - ``fields_layer_uri`` (str): the EXACT ``LayerURI.uri`` a
      ``fetch_field_boundaries`` call returned — an FTW / fiboa field-boundary
      FlatGeobuf (each feature carries a ``crop_name``).
    - ``threshold_mgl`` (float | None): the concentration (mg/L) above which a
      field counts as affected. Defaults to the plume detection floor
      (``DEFAULT_THRESHOLD_MGL``) so the affected-field count stays consistent
      with the plume footprint. Raise it to report only fields above a regulatory
      action level.
    - ``rank_by`` (str): ``"peak"`` (default — rank by peak concentration) or
      ``"area"`` (rank by affected cropland area).

    **Returns:** a dict::

        {
          "affected_fields": [
            {"field_id": int, "crop_name": str | None,
             "max_concentration_mgl": float, "mean_concentration_mgl": float | None,
             "area_km2": float}, ...   # ranked
          ],
          "n_fields_total": int,
          "n_fields_affected": int,
          "affected_area_km2": float,
          "threshold_mgl": float,
          "rank_by": str,
          "worst_field": {...} | None,
          "headline": str,             # deterministic narration (Invariant 1)
          "units": "mg/L",
          "computed_at": str,          # ISO 8601
        }

    An AOI whose fields the plume never reaches returns a VALID
    ``affected_fields=[]`` result with an honest "no fields affected" headline —
    never fabricated content (honesty floor).

    **Cross-tool dependencies:**
        Upstream (consumes): ``run_modflow_job`` /
        ``run_model_groundwater_contamination_scenario`` (the plume COG),
        ``fetch_field_boundaries`` (the FTW field vector),
        ``compute_zonal_statistics`` (the per-field intersection, called via the
        registry).
        Downstream (feeds): agent narration; the
        ``run_model_contamination_affected_fields`` composer.

    Raises (typed; ``error_code`` + ``retryable`` on each):
        AffectedFieldsInputError: a URI is missing / not a string, or
            ``threshold_mgl`` is non-positive.
        AffectedFieldsPlumeReadError: the plume COG could not be zonal-scored.
        AffectedFieldsFieldsReadError: the FTW field vector could not be read.
    """
    # --- input validation ------------------------------------------------- #
    if not isinstance(plume_layer_uri, str) or not plume_layer_uri.strip():
        raise AffectedFieldsInputError(
            "plume_layer_uri is required (the PlumeLayerURI.uri from a MODFLOW run)"
        )
    if not isinstance(fields_layer_uri, str) or not fields_layer_uri.strip():
        raise AffectedFieldsInputError(
            "fields_layer_uri is required (the LayerURI.uri from "
            "fetch_field_boundaries)"
        )
    threshold = (
        DEFAULT_THRESHOLD_MGL if threshold_mgl is None else float(threshold_mgl)
    )
    if not (threshold > 0.0):
        raise AffectedFieldsInputError(
            f"threshold_mgl must be > 0; got {threshold_mgl!r}"
        )
    rank = rank_by if rank_by in ("peak", "area") else "peak"

    # --- Step 1: per-field zonal stats of the plume over the FTW fields ---- #
    zonal_fn = _zonal_fn()
    try:
        zonal = zonal_fn(
            value_raster_uri=plume_layer_uri,
            zone_input_uri=fields_layer_uri,
            statistics=["max", "mean", "count"],
        )
    except Exception as exc:  # noqa: BLE001
        raise AffectedFieldsPlumeReadError(
            f"compute_zonal_statistics failed for plume={plume_layer_uri!r} "
            f"fields={fields_layer_uri!r}: {exc}"
        ) from exc

    by_zone = zonal.get("by_zone") if isinstance(zonal, dict) else None
    if not isinstance(by_zone, dict):
        raise AffectedFieldsPlumeReadError(
            "compute_zonal_statistics returned no by_zone breakdown; the FTW "
            "input must be a vector (each field = one zone)."
        )
    units = zonal.get("units") if isinstance(zonal, dict) else None

    # --- Step 2: read the FTW vector (same order) for crop_name + area ----- #
    gdf = _read_fields_gdf(fields_layer_uri)
    field_crop_names = [_crop_name_for(gdf, i) for i in range(len(gdf))]
    field_areas_km2 = _field_areas_km2(gdf)

    logger.info(
        "analyze_affected_fields: %d field(s) read; %d zone(s) scored; "
        "threshold=%g mg/L rank_by=%s",
        len(gdf),
        len(by_zone),
        threshold,
        rank,
    )

    # --- Steps 3-5: join + threshold split + rank + headline -------------- #
    result = build_affected_fields_result(
        by_zone=by_zone,
        field_crop_names=field_crop_names,
        field_areas_km2=field_areas_km2,
        threshold_mgl=threshold,
        rank_by=rank,
        units=units or "mg/L",
    )
    result["plume_layer_uri"] = plume_layer_uri
    result["fields_layer_uri"] = fields_layer_uri

    logger.info(
        "analyze_affected_fields: %d/%d field(s) affected; %s",
        result["n_fields_affected"],
        result["n_fields_total"],
        result["headline"],
    )
    return result
