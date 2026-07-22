"""model_nws_flood_event_scenario — Case 3 composer (sprint-13 job-0229).

The **Case 3 demo composer**: a live NWS active flood warning drives an
observed-precip SFINCS inundation run over the warning area, producing a
three-layer accumulation the UI renders together:

    1. fetch_nws_alerts_conus(event_types=FLOOD_WARNING_EVENTS)
         → CONUS active alerts (FlatGeobuf, published warning-polygon layer)
       ↓ filter to Flood Warning / Flash Flood Warning, pick highest severity
         (or the caller-specified ``warning_index``), extract the warning
         polygon + its bbox
    2. fetch_mrms_qpe(bbox=warning_polygon_bbox, accumulation="24h")
         → observed accumulated precip raster (MRMS QPE Pass2 COG)
    3. model_flood_scenario(bbox=warning_bbox, forcing_raster_uri=mrms_uri)
         → SFINCS flood-depth COG (the job-0225 v2 area-mean netamt branch)
    4. return the 3-layer accumulation contract so the client renders the
       warning polygon, the precip raster, AND the flood-depth layer together.

Per Decision G + FR-TA-1 + Invariant 2 this is **deterministic Python
composition** — there is no LLM in the chain. Alert filtering, severity
selection, and polygon extraction are pure functions over the NWS GeoJSON.

Graceful degrade (kickoff requirement + Invariant 7 — no silent wrong answer):
when the queried area has NO active Flood Warning / Flash Flood Warning, the
workflow returns a STRUCTURED no-op result (``status="no_active_flood_warning"``)
listing what WAS active (the distinct event types + counts in the response) —
it never raises, so the agent surface narrates honestly ("there are no active
flood warnings right now; the active alerts are …") instead of fabricating a
flood layer.

Polygon-source note: the registered ``fetch_nws_alerts_conus`` tool returns a
published FlatGeobuf ``LayerURI`` (the warning-polygon layer the UI renders),
but the FGB is opaque to in-process geometry inspection without a read-back.
For the selection + bbox-extraction step we call the tool module's
``_fetch_nws_conus_geojson`` + ``_filter_features_by_event_types`` helpers
directly to obtain the raw GeoJSON features (geometry + severity + properties)
— a single shared CONUS sweep feeds both the published layer and the selection.

Cross-cutting principles in force:
- **Invariant 1 (Determinism boundary): preserves.** All return fields are
  typed/derived; ``summary_text`` is a deterministic format-string, never
  LLM-generated.
- **Invariant 2 (Deterministic workflows): preserves.** Straight-line
  composition; each fetcher's failure surfaces as a typed degrade result.
- **Invariant 3 (Engine registration, not modification): preserves.** Reuses
  ``fetch_nws_alerts_conus`` / ``fetch_mrms_qpe`` / ``model_flood_scenario``
  unchanged; no hazard-specific logic in the agent core.
- **Invariant 7 (no silent wrong answers): EXTENDS — the headline.** The
  no-warning degrade path returns a structured no-op instead of fabricating a
  flood scenario over an arbitrary bbox.
- **Invariant 8 (Cancellation is first-class): preserves.** The workflow
  awaits ``model_flood_scenario`` — any ``asyncio.CancelledError`` propagates
  through unchanged, triggering the cancel chain.
- **Invariant 10 (Minimal parameter surface): preserves.** The signature
  exposes intent only (a query bbox/state + which warning + accumulation);
  the warning polygon, precip raster, DEM, landcover, river geometry, and
  Manning's are all fetched inside.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import register_tool
from ..tools.fetchers.weather.fetch_mrms_qpe import fetch_mrms_qpe
from ..tools.fetchers.weather.fetch_nws_alerts_conus import (
    _fetch_nws_conus_geojson,
    _filter_features_by_event_types,
    fetch_nws_alerts_conus,
)
from .model_flood_scenario import model_flood_scenario

__all__ = [
    "model_nws_flood_event_scenario",
    "run_model_nws_flood_event_scenario",
    "FLOOD_WARNING_EVENT_TYPES",
    "NWS_SEVERITY_ORDER",
    "select_flood_warning",
    "extract_polygon_bbox",
    "Case3Error",
]

logger = logging.getLogger(
    "trid3nt_server.workflows.model_nws_flood_event_scenario"
)


# --------------------------------------------------------------------------- #
# Constants — the flood-warning event-type set + NWS severity ordering
# --------------------------------------------------------------------------- #

#: NWS event-type strings (Title Case, the NWS canonical form) that count as a
#: "flood warning" for Case 3. We deliberately keep this to the two WARNING
#: classes — a Watch is advisory, not an active-impact warning, and Case 3 is
#: "model the flood that is happening". Surfaced as a tunable constant rather
#: than a magic literal so the catalog/composer can widen it later.
FLOOD_WARNING_EVENT_TYPES: tuple[str, ...] = (
    "Flood Warning",
    "Flash Flood Warning",
)

#: NWS ``severity`` ranks (CAP standard), most→least severe. Used to pick the
#: highest-severity warning when the caller does not specify ``warning_index``.
#: Unknown/blank severities sort last (rank = len(order)).
NWS_SEVERITY_ORDER: tuple[str, ...] = (
    "Extreme",
    "Severe",
    "Moderate",
    "Minor",
    "Unknown",
)


class Case3Error(RuntimeError):
    """Raised for fatal composition errors that cannot be degraded.

    Most failure modes surface as a structured degrade result (the workflow
    never raises for "no warnings" or an upstream fetch hiccup). ``Case3Error``
    is reserved for genuinely unrecoverable misuse (e.g. a malformed
    ``warning_index`` type) — the agent surface emits a top-level error frame.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Pure selection + geometry helpers (deterministic — unit-tested directly)
# --------------------------------------------------------------------------- #


def _severity_rank(severity: Any) -> int:
    """Rank an NWS ``severity`` string by ``NWS_SEVERITY_ORDER`` (lower = more
    severe). Unknown / non-string / blank values sort last."""
    if not isinstance(severity, str):
        return len(NWS_SEVERITY_ORDER)
    try:
        return NWS_SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(NWS_SEVERITY_ORDER)


def _feature_has_polygon(feature: dict[str, Any]) -> bool:
    """True iff the feature carries a Polygon/MultiPolygon geometry.

    NWS sometimes returns alerts with only zone/county references and a NULL
    geometry; those cannot anchor a model bbox, so the selector skips them.
    """
    if not isinstance(feature, dict):
        return False
    geom = feature.get("geometry")
    if not isinstance(geom, dict):
        return False
    return geom.get("type") in ("Polygon", "MultiPolygon")


def select_flood_warning(
    features: list[dict[str, Any]],
    *,
    warning_index: int | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select ONE flood-warning feature from a list of NWS GeoJSON features.

    Filters ``features`` to those whose ``properties.event`` is in
    ``FLOOD_WARNING_EVENT_TYPES`` AND that carry a usable Polygon geometry
    (NULL-geometry zone alerts cannot anchor a model bbox). The survivors are
    sorted most-severe-first (``NWS_SEVERITY_ORDER``), with ``onset``/``sent``
    recency as the tiebreak (most recent first) so the choice is deterministic.

    Args:
        features: NWS GeoJSON ``features`` list (each a dict with ``properties``
            + ``geometry``). Already event-type-filtered or not — this function
            re-filters defensively to the flood-warning set.
        warning_index: when given, return the warning at this 0-based index
            into the severity-sorted list (so the agent can offer "the 2nd
            warning" after listing them). ``None`` → the highest-severity one.

    Returns:
        ``(selected_feature_or_None, sorted_flood_warnings)``. The second
        element is the full severity-sorted flood-warning list (so the caller
        can enumerate alternatives / count them). ``selected_feature`` is
        ``None`` only when ``sorted_flood_warnings`` is empty OR
        ``warning_index`` is out of range.
    """
    flood_warnings = [
        f
        for f in features
        if isinstance(f, dict)
        and isinstance(f.get("properties"), dict)
        and f["properties"].get("event") in FLOOD_WARNING_EVENT_TYPES
        and _feature_has_polygon(f)
    ]

    def _sort_key(feat: dict[str, Any]) -> tuple[int, str]:
        props = feat.get("properties") or {}
        sev = _severity_rank(props.get("severity"))
        # Recency tiebreak: NWS timestamps are ISO-8601, lexicographically
        # comparable. We invert (negate via reversed string trick is messy, so
        # we sort severity ascending + recency descending in two passes).
        onset = props.get("onset") or props.get("sent") or props.get("effective") or ""
        return (sev, onset if isinstance(onset, str) else "")

    # Sort by severity ascending (most severe first), then by recency
    # descending within the same severity. Two-key sort: stable sort lets us
    # sort by recency first (descending) then by severity (ascending).
    flood_warnings.sort(
        key=lambda f: (f.get("properties") or {}).get("onset")
        or (f.get("properties") or {}).get("sent")
        or (f.get("properties") or {}).get("effective")
        or "",
        reverse=True,
    )
    flood_warnings.sort(key=lambda f: _severity_rank((f.get("properties") or {}).get("severity")))

    if not flood_warnings:
        return None, []

    if warning_index is not None:
        if not isinstance(warning_index, int) or isinstance(warning_index, bool):
            raise Case3Error(
                "CASE3_BAD_WARNING_INDEX",
                f"warning_index must be an int; got {warning_index!r}",
            )
        if warning_index < 0 or warning_index >= len(flood_warnings):
            logger.warning(
                "select_flood_warning: warning_index=%d out of range "
                "[0, %d) — returning None",
                warning_index,
                len(flood_warnings),
            )
            return None, flood_warnings
        return flood_warnings[warning_index], flood_warnings

    return flood_warnings[0], flood_warnings


def extract_polygon_bbox(
    feature: dict[str, Any],
) -> tuple[float, float, float, float]:
    """Compute the ``(min_lon, min_lat, max_lon, max_lat)`` bbox of an NWS
    GeoJSON Polygon / MultiPolygon feature.

    Walks the coordinate rings directly (no shapely dependency — keeps the
    selector import-light for the unit tests). Longitude/latitude order is the
    GeoJSON convention ``[lon, lat]``.

    Raises:
        Case3Error("CASE3_NO_GEOMETRY"): the feature has no usable polygon
            coordinates.
    """
    geom = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(geom, dict):
        raise Case3Error(
            "CASE3_NO_GEOMETRY",
            f"feature has no geometry dict: {feature!r}",
        )
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        rings = coords or []
    elif gtype == "MultiPolygon":
        rings = [ring for poly in (coords or []) for ring in poly]
    else:
        raise Case3Error(
            "CASE3_NO_GEOMETRY",
            f"unsupported geometry type {gtype!r} for bbox extraction",
        )

    min_lon = math.inf
    min_lat = math.inf
    max_lon = -math.inf
    max_lat = -math.inf
    for ring in rings:
        for pt in ring or []:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            lon, lat = float(pt[0]), float(pt[1])
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            min_lon = min(min_lon, lon)
            min_lat = min(min_lat, lat)
            max_lon = max(max_lon, lon)
            max_lat = max(max_lat, lat)

    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        raise Case3Error(
            "CASE3_NO_GEOMETRY",
            f"polygon had no finite coordinates: {feature.get('properties', {}).get('id')}",
        )
    return (min_lon, min_lat, max_lon, max_lat)


def _accumulation_hours(accumulation: str) -> int:
    """Parse an MRMS accumulation token (``"24h"``, ``"6h"``, ``"01H"`` …) into
    an integer hour count for the SFINCS netamt window. Defaults to 24 on an
    unparseable value (defensive — fetch_mrms_qpe validates the token itself)."""
    s = str(accumulation).strip().lower().rstrip("h")
    try:
        return max(1, int(s))
    except (ValueError, TypeError):
        return 24


def _distinct_active_event_counts(
    features: list[dict[str, Any]],
) -> dict[str, int]:
    """Count active alerts by ``properties.event`` (for the degrade summary).

    Returns a dict ``{event_type: count}`` so the agent can narrate "what WAS
    active" when no flood warning is present (kickoff degrade requirement).
    """
    counts: dict[str, int] = {}
    for f in features:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        ev = props.get("event")
        if isinstance(ev, str) and ev:
            counts[ev] = counts.get(ev, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _warning_summary(feature: dict[str, Any]) -> dict[str, Any]:
    """Extract the narration-relevant fields from a selected warning feature."""
    props = feature.get("properties") or {}
    return {
        "event": props.get("event"),
        "severity": props.get("severity"),
        "headline": props.get("headline"),
        "area_desc": props.get("areaDesc"),
        "onset": props.get("onset"),
        "expires": props.get("expires"),
        "sender": props.get("senderName"),
        "id": props.get("id"),
    }


# --------------------------------------------------------------------------- #
# Degrade-result builder
# --------------------------------------------------------------------------- #


def _build_no_warning_result(
    *,
    queried_area: dict[str, Any],
    active_event_counts: dict[str, int],
    flood_warning_count: int,
    warning_polygon_layer: LayerURI | None,
    reason_code: str,
    reason_detail: str,
) -> dict[str, Any]:
    """Build the structured no-op result for the no-active-flood-warning path.

    Per Invariant 7 the workflow does NOT fabricate a flood layer; it returns a
    typed no-op listing what WAS active so the agent narrates honestly.
    ``warning_polygon_layer`` may still carry the (possibly empty) published
    CONUS-alerts layer so the user can see what's active on the map.
    """
    total_active = sum(active_event_counts.values())
    if active_event_counts:
        active_list = ", ".join(
            f"{n}× {ev}" for ev, n in list(active_event_counts.items())[:8]
        )
        if len(active_event_counts) > 8:
            active_list += f", +{len(active_event_counts) - 8} more event types"
        summary = (
            f"No active Flood Warning or Flash Flood Warning found in the "
            f"queried area. {total_active} other alert(s) are active "
            f"({active_list})."
        )
    else:
        summary = (
            "No active NWS alerts of any kind found in the queried area — "
            "the weather is currently quiet there."
        )

    return {
        "status": "no_active_flood_warning",
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "queried_area": queried_area,
        "flood_warning_count": flood_warning_count,
        "active_event_counts": active_event_counts,
        "warning_polygon_layer": (
            warning_polygon_layer.model_dump(mode="json")
            if warning_polygon_layer is not None
            else None
        ),
        "mrms_precip_layer": None,
        "flood_depth_layer": None,
        "summary_text": summary,
    }


# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_nws_flood_event_scenario(
    *,
    bbox: tuple[float, float, float, float] | None = None,
    state: str | None = None,
    warning_index: int | None = None,
    accumulation: str = "24h",
    return_period_yr: int = 100,
    duration_hr: int | None = None,
    compute_class: str = "medium",
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Compose the Case 3 chain: NWS flood warning → MRMS precip → SFINCS.

    Steps:
        1. Fetch active NWS alerts (CONUS sweep, filtered to the flood-warning
           event set) and publish the warning-polygon layer.
        2. Select the highest-severity (or ``warning_index``-th) Flood Warning /
           Flash Flood Warning with a usable polygon; extract its bbox.
        3. Fetch MRMS accumulated QPE over the warning-polygon bbox.
        4. Run ``model_flood_scenario(forcing_raster_uri=mrms_uri)`` over the
           warning area (the job-0225 v2 observed-precip area-mean netamt
           branch).
        5. Return the 3-layer accumulation contract.

    Geographic note: the ``bbox`` / ``state`` parameters scope which alerts to
    CONSIDER (the underlying CONUS sweep is filtered client-side); they do NOT
    re-bound the SFINCS run — the model runs over the SELECTED warning polygon's
    bbox so the inundation footprint matches the warning area (kickoff step 3).

    Args:
        bbox: optional ``(min_lon, min_lat, max_lon, max_lat)`` to restrict the
            candidate alerts to those intersecting this area (e.g. Idaho). When
            both ``bbox`` and ``state`` are ``None``, ALL CONUS flood warnings
            are candidates and the most-severe one nationwide is selected.
        state: optional 2-letter state code (e.g. ``"ID"``) used to narrow the
            candidate set via the alert ``areaDesc`` / ``geocode`` fields. A
            convenience over ``bbox`` for "flood warnings in Idaho".
        warning_index: 0-based index into the severity-sorted flood-warning
            list. ``None`` → the highest-severity warning.
        accumulation: MRMS QPE accumulation window (``"24h"`` default — the
            standard SFINCS pluvial window). Also reused as the SFINCS netamt
            accumulation window when ``duration_hr`` is unset.
        return_period_yr: forwarded to ``model_flood_scenario`` (ignored on the
            observed-precip branch, but kept for signature parity).
        duration_hr: SFINCS simulation / precip-accumulation window in hours.
            When ``None``, derived from ``accumulation`` (e.g. ``"24h"`` → 24).
        compute_class: FR-CE-3 compute class forwarded to the solver.
        project_id / session_id: ULID identifiers threaded into the underlying
            ``model_flood_scenario`` invocation.

    Returns:
        On success — a dict carrying the **3-layer accumulation contract**:
            - ``status``: ``"ok"``
            - ``warning_polygon_layer``: the published NWS alerts ``LayerURI``
              (dict) — the warning polygon(s) the UI renders.
            - ``mrms_precip_layer``: the MRMS QPE ``LayerURI`` (dict).
            - ``flood_depth_layer``: the SFINCS flood-depth ``LayerURI`` (dict),
              or ``None`` if the (mocked/real) SFINCS run produced no layer.
            - ``selected_warning``: the chosen warning's narration fields.
            - ``warning_bbox`` / ``flood_warning_count`` / ``summary_text``.
            - ``flood_envelope``: the full ``AssessmentEnvelope`` dict (for the
              determinism-boundary narration metrics).

        On the no-active-flood-warning degrade path — a dict with
        ``status="no_active_flood_warning"`` listing what WAS active (per the
        kickoff degrade requirement); ``mrms_precip_layer`` and
        ``flood_depth_layer`` are ``None``. Never raises for "no warnings".
    """
    queried_area = {"bbox": list(bbox) if bbox else None, "state": state}
    accum_hours = duration_hr if duration_hr is not None else _accumulation_hours(accumulation)

    logger.info(
        "model_nws_flood_event_scenario start bbox=%s state=%r warning_index=%s "
        "accumulation=%r duration_hr=%s",
        bbox,
        state,
        warning_index,
        accumulation,
        accum_hours,
    )

    # --- Step 1a: published warning-polygon layer (the UI render surface) ---
    # We publish the CONUS alerts filtered to the flood-warning event set so the
    # warning polygon(s) land on the map. Non-fatal: if publish fails the
    # selection step (1b) still runs off the raw GeoJSON.
    warning_polygon_layer: LayerURI | None = None
    try:
        warning_polygon_layer = fetch_nws_alerts_conus(
            event_types=list(FLOOD_WARNING_EVENT_TYPES),
            status="actual",
        )
        # Mark this layer as context so it renders alongside (not replacing) the
        # primary flood-depth layer.
        warning_polygon_layer = warning_polygon_layer.model_copy(
            update={"role": "context"}
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "model_nws_flood_event_scenario: fetch_nws_alerts_conus (publish) "
            "failed: %s — continuing with raw-GeoJSON selection only",
            exc,
        )

    # --- Step 1b: raw GeoJSON for selection + bbox extraction ---
    # Pull the CONUS sweep's raw GeoJSON so we can read geometry + severity. The
    # cache-mediated published layer above and this read share the same upstream
    # hour bucket, so this is not a second meaningfully-different upstream hit.
    try:
        geojson = _fetch_nws_conus_geojson(
            "https://api.weather.gov/alerts/active?status=actual"
        )
    except Exception as exc:  # noqa: BLE001
        # Upstream NWS unavailable — degrade (do NOT loop/retry per kickoff).
        logger.warning(
            "model_nws_flood_event_scenario: NWS GeoJSON fetch failed: %s", exc
        )
        return _build_no_warning_result(
            queried_area=queried_area,
            active_event_counts={},
            flood_warning_count=0,
            warning_polygon_layer=warning_polygon_layer,
            reason_code=getattr(exc, "error_code", "NWS_FETCH_FAILED"),
            reason_detail=str(exc),
        )

    all_features = geojson.get("features", []) or []
    # Optionally narrow to the queried area (bbox/state) before selecting.
    candidate_features = _narrow_candidates(all_features, bbox=bbox, state=state)
    # Counts over the candidate set drive the degrade summary.
    active_event_counts = _distinct_active_event_counts(candidate_features)

    # --- Step 2: select the flood warning + extract its polygon bbox ---
    selected, flood_warnings = select_flood_warning(
        candidate_features, warning_index=warning_index
    )
    flood_warning_count = len(flood_warnings)

    if selected is None:
        # Degrade: no flood warning (or warning_index out of range).
        reason = (
            "WARNING_INDEX_OUT_OF_RANGE"
            if (warning_index is not None and flood_warning_count > 0)
            else "NO_ACTIVE_FLOOD_WARNING"
        )
        logger.info(
            "model_nws_flood_event_scenario: no selectable flood warning "
            "(count=%d, index=%s, reason=%s)",
            flood_warning_count,
            warning_index,
            reason,
        )
        return _build_no_warning_result(
            queried_area=queried_area,
            active_event_counts=active_event_counts,
            flood_warning_count=flood_warning_count,
            warning_polygon_layer=warning_polygon_layer,
            reason_code=reason,
            reason_detail=(
                f"{flood_warning_count} flood warning(s) found; "
                f"warning_index={warning_index} not selectable"
            ),
        )

    try:
        warning_bbox = extract_polygon_bbox(selected)
    except Case3Error as exc:
        logger.warning(
            "model_nws_flood_event_scenario: selected warning has no usable "
            "polygon (%s) — degrading", exc
        )
        return _build_no_warning_result(
            queried_area=queried_area,
            active_event_counts=active_event_counts,
            flood_warning_count=flood_warning_count,
            warning_polygon_layer=warning_polygon_layer,
            reason_code=exc.error_code,
            reason_detail=str(exc),
        )

    selected_summary = _warning_summary(selected)
    logger.info(
        "model_nws_flood_event_scenario: selected %s (severity=%s) bbox=%s",
        selected_summary.get("event"),
        selected_summary.get("severity"),
        warning_bbox,
    )

    # --- Step 3: MRMS QPE over the warning-polygon bbox ---
    try:
        mrms_layer = fetch_mrms_qpe(bbox=warning_bbox, accumulation=accumulation)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "model_nws_flood_event_scenario: fetch_mrms_qpe failed: %s — "
            "degrading (warning polygon still rendered)", exc
        )
        result = _build_no_warning_result(
            queried_area=queried_area,
            active_event_counts=active_event_counts,
            flood_warning_count=flood_warning_count,
            warning_polygon_layer=warning_polygon_layer,
            reason_code=getattr(exc, "error_code", "MRMS_FETCH_FAILED"),
            reason_detail=str(exc),
        )
        # A flood warning WAS selected — surface it so the agent narrates the
        # warning even though precip could not be fetched.
        result["status"] = "mrms_fetch_failed"
        result["selected_warning"] = selected_summary
        result["warning_bbox"] = list(warning_bbox)
        return result

    # --- Step 4: SFINCS over the warning area, forced by observed MRMS precip ---
    flood_envelope = await model_flood_scenario(
        bbox=warning_bbox,
        forcing_raster_uri=mrms_layer.uri,
        return_period_yr=return_period_yr,
        duration_hr=accum_hours,
        compute_class=compute_class,
        project_id=project_id,
        session_id=session_id,
    )

    flood_depth_layer = _flood_layer_from_envelope(flood_envelope, warning_bbox)
    flood_failed, flood_error_code = _detect_flood_failure(flood_envelope)

    # --- Step 5: assemble the 3-layer accumulation contract ---
    summary_text = _format_case3_summary(
        selected=selected_summary,
        warning_bbox=warning_bbox,
        accumulation=accumulation,
        flood_failed=flood_failed,
        flood_error_code=flood_error_code,
        flood_envelope=flood_envelope,
        flood_warning_count=flood_warning_count,
    )

    return {
        "status": "ok",
        "warning_polygon_layer": (
            warning_polygon_layer.model_dump(mode="json")
            if warning_polygon_layer is not None
            else None
        ),
        "mrms_precip_layer": mrms_layer.model_dump(mode="json"),
        "flood_depth_layer": (
            flood_depth_layer.model_dump(mode="json")
            if flood_depth_layer is not None
            else None
        ),
        "selected_warning": selected_summary,
        "warning_bbox": list(warning_bbox),
        "flood_warning_count": flood_warning_count,
        "active_event_counts": active_event_counts,
        "flood_envelope": flood_envelope.model_dump(mode="json"),
        "summary_text": summary_text,
    }


# --------------------------------------------------------------------------- #
# Candidate narrowing (bbox / state) + envelope helpers
# --------------------------------------------------------------------------- #


def _narrow_candidates(
    features: list[dict[str, Any]],
    *,
    bbox: tuple[float, float, float, float] | None,
    state: str | None,
) -> list[dict[str, Any]]:
    """Narrow the CONUS alert features to those matching ``bbox`` and/or ``state``.

    - ``state``: keep features whose ``properties.geocode.UGC`` codes or
      ``areaDesc`` reference the 2-letter state code (UGC codes start with the
      state abbreviation, e.g. ``"IDC001"`` / ``"IDZ012"`` for Idaho).
    - ``bbox``: keep features whose polygon bbox intersects the query bbox.

    When both are ``None``, returns ``features`` unchanged (all-CONUS
    candidates). Conservative: a feature passes if EITHER filter is satisfied
    when that filter is active; we AND the active filters.
    """
    if bbox is None and state is None:
        return features

    state_uc = state.strip().upper() if isinstance(state, str) and state.strip() else None
    out: list[dict[str, Any]] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        if state_uc is not None and not _feature_in_state(f, state_uc):
            continue
        if bbox is not None and not _feature_intersects_bbox(f, bbox):
            continue
        out.append(f)
    return out


def _feature_in_state(feature: dict[str, Any], state_uc: str) -> bool:
    """True iff the feature references the given 2-letter state code.

    Checks NWS UGC geocodes (``properties.geocode.UGC`` — each code's first two
    chars are the state abbreviation) and falls back to a substring match on
    ``areaDesc`` (e.g. ``", ID"`` / ``"Idaho"`` is harder, so UGC is primary).
    """
    props = feature.get("properties") or {}
    geocode = props.get("geocode") or {}
    if isinstance(geocode, dict):
        for key in ("UGC", "SAME"):
            codes = geocode.get(key)
            if isinstance(codes, list):
                for code in codes:
                    if isinstance(code, str) and code[:2].upper() == state_uc:
                        return True
    # Fallback: areaDesc substring (", ID" or "ID;" patterns).
    area_desc = props.get("areaDesc")
    if isinstance(area_desc, str):
        if f", {state_uc}" in area_desc.upper() or area_desc.upper().endswith(
            f" {state_uc}"
        ):
            return True
    return False


def _feature_intersects_bbox(
    feature: dict[str, Any],
    query_bbox: tuple[float, float, float, float],
) -> bool:
    """True iff the feature's polygon bbox intersects ``query_bbox``."""
    try:
        fb = extract_polygon_bbox(feature)
    except Case3Error:
        return False
    qmin_lon, qmin_lat, qmax_lon, qmax_lat = query_bbox
    fmin_lon, fmin_lat, fmax_lon, fmax_lat = fb
    return not (
        fmax_lon < qmin_lon
        or fmin_lon > qmax_lon
        or fmax_lat < qmin_lat
        or fmin_lat > qmax_lat
    )


def _flood_layer_from_envelope(
    envelope: Any,
    warning_bbox: tuple[float, float, float, float],
) -> LayerURI | None:
    """Extract the primary flood-depth ``LayerURI`` from a flood envelope.

    The envelope's ``layers`` are ``ResultLayer`` instances (no bbox field); we
    re-wrap the primary as a ``LayerURI`` carrying ``warning_bbox`` so the
    pipeline emitter's zoom-to fires on the warning area. Returns ``None`` for a
    failed envelope (empty layers).
    """
    layers = getattr(envelope, "layers", None) or []
    if not layers:
        return None
    primary = layers[0]
    return LayerURI(
        layer_id=getattr(primary, "layer_id", ""),
        name=getattr(primary, "name", ""),
        layer_type=getattr(primary, "layer_type", "raster"),
        uri=getattr(primary, "uri", ""),
        style_preset=getattr(primary, "style_preset", "continuous_flood_depth"),
        temporal=getattr(primary, "temporal", None),
        role=getattr(primary, "role", "primary"),
        units=getattr(primary, "units", None),
        bbox=warning_bbox,
    )


def _detect_flood_failure(envelope: Any) -> tuple[bool, str | None]:
    """Inspect a ``model_flood_scenario`` envelope for partial-failure markers.

    The partial-failure envelope encodes the error code into
    ``flood.metrics.solver_version`` as ``"failed:<CODE>"`` (per job-0042).
    Returns ``(failed, error_code)``.
    """
    flood = getattr(envelope, "flood", None)
    if flood is None:
        return (True, None)
    metrics = getattr(flood, "metrics", None)
    if metrics is None:
        return (True, None)
    sv = getattr(metrics, "solver_version", "") or ""
    if isinstance(sv, str) and sv.startswith("failed:"):
        return (True, sv.split(":", 1)[1] or None)
    if not getattr(envelope, "layers", None):
        return (True, None)
    return (False, None)


def _format_case3_summary(
    *,
    selected: dict[str, Any],
    warning_bbox: tuple[float, float, float, float],
    accumulation: str,
    flood_failed: bool,
    flood_error_code: str | None,
    flood_envelope: Any,
    flood_warning_count: int,
) -> str:
    """Build the deterministic narration string for a successful Case 3 run.

    Format-string only — no LLM in the chain (Invariant 1). Flood metrics are
    cited from the envelope's ``FloodMetrics`` (the determinism boundary).
    """
    event = selected.get("event") or "Flood Warning"
    severity = selected.get("severity") or "Unknown"
    area = selected.get("area_desc") or "the warning area"
    parts: list[str] = [
        f"Active {event} ({severity}) over {area}"
    ]
    if flood_warning_count > 1:
        parts.append(f" (1 of {flood_warning_count} active flood warning(s))")
    parts.append(
        f". Forced an SFINCS inundation run with {accumulation} observed MRMS "
        f"precipitation over the warning area"
    )
    if flood_failed:
        parts.append(
            f", but flood modeling did not complete "
            f"(error: {flood_error_code or 'UNKNOWN'})"
        )
    else:
        flood = getattr(flood_envelope, "flood", None)
        metrics = getattr(flood, "metrics", None) if flood is not None else None
        if metrics is not None:
            max_d = getattr(metrics, "max_depth_m", None)
            mean_d = getattr(metrics, "mean_depth_m", None)
            area_km2 = getattr(metrics, "flooded_area_km2", None)
            pieces: list[str] = []
            if max_d is not None:
                pieces.append(f"max depth {float(max_d):.2f} m")
            if mean_d is not None:
                pieces.append(f"mean {float(mean_d):.2f} m")
            if area_km2 is not None:
                pieces.append(f"{float(area_km2):.1f} km² inundated")
            if pieces:
                parts.append(f": {', '.join(pieces)}")
    parts.append(
        f". Warning bbox=[{warning_bbox[0]:.4f}, {warning_bbox[1]:.4f}, "
        f"{warning_bbox[2]:.4f}, {warning_bbox[3]:.4f}]."
    )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_CASE_THREE_METADATA = AtomicToolMetadata(
    name="run_model_nws_flood_event_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_CASE_THREE_METADATA)
async def run_model_nws_flood_event_scenario(
    bbox: tuple[float, float, float, float] | None = None,
    state: str | None = None,
    warning_index: int | None = None,
    accumulation: str = "24h",
    return_period_yr: int = 100,
    duration_hr: int | None = None,
    compute_class: str = "medium",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model the flood from a live NWS flood warning (Case 3: NWS → MRMS → SFINCS).

    Use this (not run_model_flood_scenario) when the flood is driven by a LIVE NWS flood warning/alert (NWS -> MRMS -> SFINCS).

    Five-step deterministic composition (zero LLM calls inside):
    1. ``fetch_nws_alerts_conus(event_types=["Flood Warning", "Flash Flood
       Warning"])`` — active NWS flood-warning polygons, published as a map layer.
    2. Select the highest-severity (or ``warning_index``-th) flood warning with
       a usable polygon and extract its bounding box.
    3. ``fetch_mrms_qpe(bbox=warning_bbox, accumulation)`` — observed accumulated
       radar-gauge precipitation over the warning area.
    4. ``run_model_flood_scenario(bbox=warning_bbox,
       forcing_raster_uri=mrms_uri)`` — SFINCS inundation forced by the OBSERVED
       precip (not a design storm).
    5. Return the warning polygon, the precip raster, AND the flood-depth layer
       (a 3-layer accumulation the client renders together).

    When to use:
        - User asks to "model the flood that's happening", "model the current
          flood warning", "show flood warnings in Idaho and model the flood",
          or any request that ties an ACTIVE NWS flood/flash-flood warning to a
          flood inundation model over the warned area.
        - Real-data, real-time flood scenario (observed precipitation), as
          opposed to a hypothetical return-period design storm.

    When NOT to use:
        - A hypothetical / design-storm flood for a named place with no active
          warning — use ``run_model_flood_scenario`` with a
          ``return_period_yr`` instead.
        - Just listing active alerts with no modeling — use
          ``fetch_nws_alerts_conus`` or ``fetch_nws_event`` directly.
        - Non-flood hazards.

    Params:
        bbox: optional ``(min_lon, min_lat, max_lon, max_lat)`` (EPSG:4326) to
            restrict candidate warnings to a region. ``None`` → all CONUS.
        state: optional 2-letter state code (e.g. ``"ID"``) to narrow candidate
            warnings (e.g. "flood warnings in Idaho").
        warning_index: 0-based index into the severity-sorted flood-warning
            list. ``None`` → the highest-severity warning.
        accumulation: MRMS QPE accumulation window — ``"1h"``, ``"6h"``,
            ``"24h"`` (default), ``"72h"``. Also the SFINCS precip-accumulation
            window unless ``duration_hr`` overrides it.
        return_period_yr: forwarded to the flood model (ignored on the
            observed-precip branch; kept for parity). Default 100.
        duration_hr: SFINCS simulation / precip window in hours. ``None`` →
            derived from ``accumulation`` (``"24h"`` → 24).
        compute_class: FR-CE-3 compute class. Default ``"medium"``.

    Returns:
        A dict with ``status`` and the 3-layer accumulation contract:
            - ``"ok"`` → ``warning_polygon_layer`` (LayerURI dict),
              ``mrms_precip_layer`` (LayerURI dict), ``flood_depth_layer``
              (LayerURI dict or None), ``selected_warning``, ``warning_bbox``,
              ``summary_text``, and the full ``flood_envelope`` (for the
              narration metrics — determinism boundary).
            - ``"no_active_flood_warning"`` → a structured no-op listing
              ``active_event_counts`` (what WAS active) so the agent narrates
              honestly; ``mrms_precip_layer`` / ``flood_depth_layer`` are None.
            - ``"mrms_fetch_failed"`` → a flood warning was selected but precip
              could not be fetched; the warning polygon is still rendered.

    FR-DC-6: declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — same shape as
    ``run_model_flood_scenario`` / ``run_model_flood_habitat_scenario``. The
    composer runs through cacheable atomic tools, so identical inputs still
    benefit from per-tool cache hits even though the composer itself is uncached.

    Cross-tool dependencies:
        Upstream (step chain):
        - ``fetch_nws_alerts_conus`` → step 1 (warning-polygon layer)
        - ``fetch_mrms_qpe`` → step 3 (observed precip raster)
        - ``run_model_flood_scenario`` (forcing_raster_uri branch) → step 4
          (SFINCS inundation; itself a 9-step chain)
        Downstream (feeds):
        - Agent narration — cites ``flood_envelope.flood.metrics`` (max/mean
          depth, inundated area) verbatim (Invariant 7).
        - The client renders all three returned ``LayerURI`` dicts as a
          stacked accumulation (warning polygon + precip + flood depth).
    """
    return await model_nws_flood_event_scenario(
        bbox=bbox,
        state=state,
        warning_index=warning_index,
        accumulation=accumulation,
        return_period_yr=return_period_yr,
        duration_hr=duration_hr,
        compute_class=compute_class,
    )
