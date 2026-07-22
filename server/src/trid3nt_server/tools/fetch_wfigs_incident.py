"""``fetch_wfigs_incident`` atomic tool -- NIFC/WFIGS named-incident lookup (fire demo S1/J1).

Resolves a NAMED wildland-fire incident (e.g. "Iron", "Hastings", "Santa Rosa
Island") against the National Interagency Fire Center (NIFC) WFIGS Incident
Locations Current ArcGIS REST FeatureService, returning the authoritative point
(InitialLatitude / InitialLongitude), the FireDiscoveryDateTime, the incident
size, and a derived bbox suitable for a satellite-animation AOI.

This is the upstream "resolve the fire by NAME" step both fire-animation demos
need: the news-ingest front half (``model_news_event_ingest``) geocodes a free-
text LOCATION to a bbox, but it does NOT resolve a named INCIDENT to an
authoritative point + discovery time. WFIGS does -- and it resolves by name even
when the fire is somewhere a county geocode would miss (Santa Rosa Island is an
offshore island in Channel Islands National Park, NOT a normal CONUS county
geometry, so it MUST be resolved by IncidentName, not county).

Endpoint (same ArcGIS org T4QMspbfLg3qTGWY as the already-wired NIFC perimeters
fetcher; live-verified for the demo incidents 2026-06-22)::

    https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/
        WFIGS_Incident_Locations_Current/FeatureServer/0/query

Query parameters used::

    where=UPPER(IncidentName) LIKE '%<NAME>%'           (+ optional POOState IN (...))
    outFields=IncidentName,FireDiscoveryDateTime,InitialLatitude,
              InitialLongitude,IncidentSize,PercentContained,POOState,
              POOCounty,IrwinID,UniqueFireIdentifier
    outSR=4326
    returnGeometry=true
    f=json

WFIGS quirks (carried from the design spike):

- ``POOState`` is ISO 3166-2 ('US-UT' / 'US-NV' / 'US-CA'), NOT 'UT'/'NV'/'CA'.
  The ``state`` argument accepts either form; bare 2-letter codes are upcased and
  prefixed with 'US-'.
- ``IncidentName`` is the BARE token ('Iron', not 'Iron Fire'); the lookup uses a
  case-insensitive LIKE so either form matches, but pass the bare token when you
  can.
- Coordinates come from ``InitialLatitude`` / ``InitialLongitude`` attribute
  fields; the feature geometry is a backup when those are absent.

Cache: ``dynamic-1h`` (an active incident's size / containment move; the point +
discovery time are stable, but one-hour bucketing keeps the demo fresh). Cache key
is over ``(incident_name_lower, state_norm, bbox_pad_deg)``.

FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_wfigs_incident",
    "WFIGSIncidentError",
    "WFIGSIncidentInputError",
    "WFIGSIncidentUpstreamError",
    "WFIGSIncidentNotFoundError",
    "_normalize_state",
    "_build_wfigs_params",
    "_select_best_feature",
    "_bbox_from_point",
    "_feature_point",
    "_significant_name_tokens",
    "WFIGS_INCIDENT_BASE",
    "WFIGS_INCIDENT_YTD_BASE",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_wfigs_incident")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class WFIGSIncidentError(RuntimeError):
    """Base class for fetch_wfigs_incident failures."""

    error_code: str = "WFIGS_INCIDENT_ERROR"
    retryable: bool = True


class WFIGSIncidentInputError(WFIGSIncidentError):
    """Invalid argument (empty name, malformed state)."""

    error_code = "WFIGS_INCIDENT_INPUT_INVALID"
    retryable = False


class WFIGSIncidentUpstreamError(WFIGSIncidentError):
    """WFIGS ArcGIS REST query failed (network, HTTP, non-JSON, or error envelope)."""

    error_code = "WFIGS_INCIDENT_UPSTREAM_ERROR"
    retryable = True


class WFIGSIncidentNotFoundError(WFIGSIncidentError):
    """The query succeeded but matched no incident with the requested name.

    Distinct from an upstream failure: the service answered, there is just no
    such named incident currently active. Surfaced as a typed error (NOT a
    silent empty result the LLM could narrate as success -- data-source
    fallback norm: honest typed dead-end, never a hallucinated hit).
    """

    error_code = "WFIGS_INCIDENT_NOT_FOUND"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

WFIGS_INCIDENT_BASE = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)

#: The WFIGS Year-To-Date (all-incidents) sibling service. Same NIFC org,
#: same layer-0 attribute shape, but it carries CONTAINED / recently-finished
#: incidents the "Current" feed has already dropped. A recently-contained fire
#: (e.g. the ~18k-acre Santa Rosa Island fire) resolves here even when the
#: "Current" feed returns 0 matches. We query "Current" first (live, smallest)
#: and fall back to YearToDate so a recent-but-contained incident still resolves.
WFIGS_INCIDENT_YTD_BASE = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_YearToDate/FeatureServer/0/query"
)

#: Ordered list of WFIGS query endpoints, tried in turn until one matches:
#: the live "Current" active feed first, then the "YearToDate" all-incidents
#: feed (which also carries contained fires). ADDITIVE: callers that resolved
#: against "Current" before still resolve against it first, byte-identically.
_WFIGS_INCIDENT_BASES = (WFIGS_INCIDENT_BASE, WFIGS_INCIDENT_YTD_BASE)

#: Attribute fields requested from each WFIGS incident feature.
_OUT_FIELDS = (
    "IncidentName",
    "FireDiscoveryDateTime",
    "InitialLatitude",
    "InitialLongitude",
    "IncidentSize",
    "PercentContained",
    "POOState",
    "POOCounty",
    "IrwinID",
    "UniqueFireIdentifier",
)

#: Default half-width of the derived bbox around the incident point, in degrees.
#: 0.25 deg (~27 km N-S) gives a single-fire AOI comfortably inside a satellite
#: sector; the animation workflow can widen it to cover a multi-fire cluster.
_DEFAULT_BBOX_PAD_DEG = 0.25

#: User-Agent -- NIFC asks automated clients to identify themselves.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_HTTP_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_wfigs_incident",
    ttl_class="dynamic-1h",
    source_class="wfigs_incident",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Pure helpers (also importable for tests).
# ---------------------------------------------------------------------------


def _normalize_state(state: str | None) -> str | None:
    """Normalize a state argument to the WFIGS ISO 3166-2 ``US-XX`` form.

    Accepts ``"UT"`` / ``"ut"`` / ``"US-UT"`` / ``"us-ut"`` and returns
    ``"US-UT"``. Returns ``None`` for a falsy / blank input (no state filter).
    Raises ``WFIGSIncidentInputError`` for an obviously malformed code.
    """
    if not state or not str(state).strip():
        return None
    s = str(state).strip().upper().replace("_", "-")
    if s.startswith("US-"):
        body = s[3:]
    else:
        body = s
    if len(body) != 2 or not body.isalpha():
        raise WFIGSIncidentInputError(
            f"state={state!r} is not a 2-letter US state code (e.g. 'UT' or 'US-UT')"
        )
    return f"US-{body}"


#: Tokens dropped from a multi-word incident name before the loose token-OR
#: match (noise words that would over-broaden a contains-LIKE). "Fire" is the
#: most common; geographic generics ("island", "creek", ...) are NOT dropped
#: because they can be the discriminating token of a real incident name.
_NAME_STOP_TOKENS = frozenset({"FIRE", "THE", "OF", "AND", "COMPLEX"})

#: Minimum significant-token length kept for the loose token-OR match (drops
#: 1-2 char fragments that would match almost everything).
_MIN_TOKEN_LEN = 3


def _significant_name_tokens(name: str) -> list[str]:
    """Split a name into UPPER significant tokens for the loose token-OR match.

    Drops the noise stop-tokens (``FIRE`` etc.) and very short fragments, so
    "Santa Rosa Island Fire" -> ["SANTA", "ROSA", "ISLAND"]. Returns ``[]`` when
    nothing significant remains (the caller then falls back to the whole-string
    contains match).
    """
    toks = []
    for raw in (name or "").upper().replace("/", " ").split():
        t = raw.strip("'\"().,;:")
        if len(t) >= _MIN_TOKEN_LEN and t not in _NAME_STOP_TOKENS:
            toks.append(t)
    return toks


def _build_wfigs_params(
    incident_name: str,
    state_norm: str | None,
) -> dict[str, str]:
    """Build the WFIGS FeatureServer query params for a name (+ optional state) lookup.

    Uses a case-insensitive ``UPPER(IncidentName) LIKE '%<NAME>%'`` so the bare
    token ('Iron') matches 'Iron' AND a fuller 'Iron Fire' label, and so casing
    does not matter. A trailing ' FIRE' on the user's input is stripped before
    the LIKE so "Iron Fire" still matches the bare 'Iron' incident name.

    LOOSE token-OR (fire demo J5 fix): a MULTI-word name ALSO matches on ANY of
    its significant tokens (``LIKE '%SANTA%' OR LIKE '%ROSA%' OR
    LIKE '%ISLAND%'``), so a user phrasing that does not exactly equal the WFIGS
    ``IncidentName`` token still resolves (e.g. "Santa Rosa Island" matching a
    feed entry labelled just "Santa Rosa", or a record carrying extra words).
    A single-token name keeps the original whole-string contains match. The
    optional ``POOState`` filter is ANDed across the whole name clause.
    """
    name = (incident_name or "").strip()
    # Strip a trailing " Fire" so "Santa Rosa Island Fire" matches the bare
    # "Santa Rosa Island" IncidentName token.
    if name.upper().endswith(" FIRE"):
        name = name[: -len(" FIRE")].strip()
    # ArcGIS SQL escapes a single quote by doubling it.
    safe = name.upper().replace("'", "''")
    whole = f"UPPER(IncidentName) LIKE '%{safe}%'"

    # Loosen: for a multi-word name, OR-match on each significant token so a
    # near-miss phrasing still resolves. The whole-string contains stays first
    # (so an exact substring still wins selection by size, downstream).
    tokens = _significant_name_tokens(name)
    if len(tokens) >= 2:
        clauses = [whole]
        for tok in tokens:
            safe_tok = tok.replace("'", "''")
            clauses.append(f"UPPER(IncidentName) LIKE '%{safe_tok}%'")
        where = "(" + " OR ".join(clauses) + ")"
    else:
        where = whole

    if state_norm:
        where = f"({where}) AND POOState = '{state_norm}'"
    return {
        "where": where,
        "outFields": ",".join(_OUT_FIELDS),
        "outSR": "4326",
        "returnGeometry": "true",
        "f": "json",
    }


def _feature_point(feature: dict[str, Any]) -> tuple[float, float] | None:
    """Return ``(lon, lat)`` for a WFIGS feature, or ``None`` if unresolvable.

    Prefers the ``InitialLongitude`` / ``InitialLatitude`` attribute fields (the
    authoritative point of origin); falls back to the feature point geometry.
    """
    attrs = feature.get("attributes") or {}
    lon = attrs.get("InitialLongitude")
    lat = attrs.get("InitialLatitude")
    try:
        if lon is not None and lat is not None:
            flon, flat = float(lon), float(lat)
            if _is_finite_lonlat(flon, flat):
                return flon, flat
    except (TypeError, ValueError):
        pass
    geom = feature.get("geometry") or {}
    glon, glat = geom.get("x"), geom.get("y")
    try:
        if glon is not None and glat is not None:
            flon, flat = float(glon), float(glat)
            if _is_finite_lonlat(flon, flat):
                return flon, flat
    except (TypeError, ValueError):
        pass
    return None


def _is_finite_lonlat(lon: float, lat: float) -> bool:
    """True iff (lon, lat) is a finite, in-range geographic coordinate."""
    import math

    return (
        math.isfinite(lon)
        and math.isfinite(lat)
        and -180.0 <= lon <= 180.0
        and -90.0 <= lat <= 90.0
        # Reject the ArcGIS null-island / 0,0 sentinel that some rows carry when
        # the point of origin was never set.
        and not (lon == 0.0 and lat == 0.0)
    )


def _select_best_feature(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the single best WFIGS incident feature from a LIKE-match result set.

    Selection (deterministic):
    1. Drop features with no resolvable point.
    2. Prefer the LARGEST ``IncidentSize`` (acres) -- a named multi-source query
       usually wants the major incident, and size is the most reliable
       disambiguator across same-named small fires.
    3. Tie-break on the most-recent ``FireDiscoveryDateTime``.

    Returns ``None`` when no feature has a usable point.
    """
    usable = [f for f in features if _feature_point(f) is not None]
    if not usable:
        return None

    def _size(f: dict[str, Any]) -> float:
        v = (f.get("attributes") or {}).get("IncidentSize")
        try:
            return float(v) if v is not None else -1.0
        except (TypeError, ValueError):
            return -1.0

    def _disc(f: dict[str, Any]) -> float:
        v = (f.get("attributes") or {}).get("FireDiscoveryDateTime")
        try:
            return float(v) if v is not None else -1.0
        except (TypeError, ValueError):
            return -1.0

    usable.sort(key=lambda f: (_size(f), _disc(f)), reverse=True)
    return usable[0]


def _bbox_from_point(
    lon: float,
    lat: float,
    pad_deg: float = _DEFAULT_BBOX_PAD_DEG,
) -> tuple[float, float, float, float]:
    """Build a (min_lon, min_lat, max_lon, max_lat) bbox padded around a point.

    The N-S pad is ``pad_deg``; the E-W pad is widened by ``1/cos(lat)`` so the
    AOI is roughly square on the ground at the incident's latitude (a fixed
    degree pad would look narrow E-W at high latitudes). Clamped to valid ranges.
    """
    import math

    pad = max(0.01, float(pad_deg))
    cos_lat = math.cos(math.radians(max(-89.0, min(89.0, lat))))
    ew_pad = pad / max(0.2, cos_lat)
    min_lon = max(-180.0, lon - ew_pad)
    max_lon = min(180.0, lon + ew_pad)
    min_lat = max(-90.0, lat - pad)
    max_lat = min(90.0, lat + pad)
    return (
        round(min_lon, 6),
        round(min_lat, 6),
        round(max_lon, 6),
        round(max_lat, 6),
    )


def _epoch_ms_to_iso(value: Any) -> str | None:
    """Convert an ArcGIS epoch-milliseconds datetime to an ISO-8601 UTC string.

    WFIGS ``FireDiscoveryDateTime`` is epoch milliseconds (Esri convention).
    Returns ``None`` for a missing / non-numeric value.
    """
    if value is None:
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Network fetch.
# ---------------------------------------------------------------------------


def _fetch_wfigs_json(
    params: dict[str, str],
    base_url: str = WFIGS_INCIDENT_BASE,
) -> dict[str, Any]:
    """GET a WFIGS FeatureServer query against ``base_url`` and return parsed JSON.

    ``base_url`` selects the endpoint -- the live "Current" active feed
    (default) or the "YearToDate" all-incidents sibling that also carries
    contained fires. Raises ``WFIGSIncidentUpstreamError`` on network / HTTP /
    non-JSON / ArcGIS error-envelope responses.
    """
    logger.info("fetch_wfigs_incident: GET %s where=%s", base_url, params.get("where"))
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                base_url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.HTTPError as exc:
        raise WFIGSIncidentUpstreamError(
            f"WFIGS request failed: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise WFIGSIncidentUpstreamError(
            f"WFIGS returned HTTP {resp.status_code}: {resp.text[:300]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise WFIGSIncidentUpstreamError(
            f"WFIGS returned non-JSON: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise WFIGSIncidentUpstreamError(
            f"WFIGS response is not a JSON object: type={type(body).__name__!r}"
        )
    if "error" in body:
        raise WFIGSIncidentUpstreamError(
            f"WFIGS query returned error envelope: {body['error']}"
        )
    return body


def _resolve_incident(
    incident_name: str,
    state_norm: str | None,
    pad_deg: float,
) -> dict[str, Any]:
    """Resolve a named incident -> a structured dict (the cache fetch_fn payload).

    Returns a dict carrying ``incident_name`` / ``lat`` / ``lon`` / ``bbox`` /
    ``fire_discovery_datetime`` / ``incident_size_acres`` / ``percent_contained``
    / ``poo_state`` / ``poo_county`` / ``irwin_id``. Raised typed errors:
    ``WFIGSIncidentNotFoundError`` (no match), ``WFIGSIncidentUpstreamError``.

    Queries each endpoint in ``_WFIGS_INCIDENT_BASES`` in turn -- the live
    "Current" active feed first, then the "YearToDate" all-incidents sibling --
    and returns the first feed that yields a usable feature. A recently-contained
    fire that the "Current" feed has already dropped (0 matches) resolves against
    "YearToDate". Only when BOTH feeds miss does the typed not-found raise (the
    fire-animation workflow now falls back to FIRMS-derived localization, so this
    no-match is no longer a hard gate -- it is an honest typed dead-end).
    """
    params = _build_wfigs_params(incident_name, state_norm)
    best: dict[str, Any] | None = None
    total_features = 0
    for base_url in _WFIGS_INCIDENT_BASES:
        body = _fetch_wfigs_json(params, base_url=base_url)
        features = body.get("features") or []
        if isinstance(features, list):
            total_features += len(features)
            candidate = _select_best_feature(features)
            if candidate is not None:
                best = candidate
                logger.info(
                    "fetch_wfigs_incident: matched %r against %s",
                    incident_name,
                    base_url,
                )
                break
    if best is None:
        raise WFIGSIncidentNotFoundError(
            f"no WFIGS incident matched name={incident_name!r} "
            f"state={state_norm!r} across Current + YearToDate feeds "
            f"(matched {total_features} feature(s), none with a usable point)"
        )
    point = _feature_point(best)
    assert point is not None  # _select_best_feature guarantees this
    lon, lat = point
    bbox = _bbox_from_point(lon, lat, pad_deg)
    attrs = best.get("attributes") or {}
    return {
        "incident_name": attrs.get("IncidentName") or incident_name,
        "lat": lat,
        "lon": lon,
        "bbox": list(bbox),
        "fire_discovery_datetime": _epoch_ms_to_iso(
            attrs.get("FireDiscoveryDateTime")
        ),
        "incident_size_acres": attrs.get("IncidentSize"),
        "percent_contained": attrs.get("PercentContained"),
        "poo_state": attrs.get("POOState"),
        "poo_county": attrs.get("POOCounty"),
        "irwin_id": attrs.get("IrwinID"),
        "unique_fire_identifier": attrs.get("UniqueFireIdentifier"),
    }


def _resolve_incident_bytes(
    incident_name: str,
    state_norm: str | None,
    pad_deg: float,
) -> bytes:
    """Cache fetch_fn: resolve the incident and serialize the dict to JSON bytes."""
    payload = _resolve_incident(incident_name, state_norm, pad_deg)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True, openWorldHint=True (external ArcGIS REST),
    # destructiveHint=False, idempotentHint=True (cache dedupes).
    open_world_hint=True,
)
def fetch_wfigs_incident(
    incident_name: str,
    state: str | None = None,
    bbox_pad_deg: float = _DEFAULT_BBOX_PAD_DEG,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Resolve a NAMED wildfire incident (NIFC/WFIGS) -> authoritative point + bbox + discovery time.

    **What it does:** Looks a named wildland-fire incident up in the NIFC WFIGS
    Incident Locations Current ArcGIS FeatureService and returns its authoritative
    point of origin (lat/lon), the FireDiscoveryDateTime (ISO UTC), the incident
    size (acres) + percent contained, the point-of-origin state/county, the IRWIN
    id, AND a padded AOI bbox built around the point. Resolves BY NAME (a case-
    insensitive LIKE), so it works for incidents a county geocode would miss
    (e.g. Santa Rosa Island, an offshore island in Channel Islands National Park).

    **When to use:**
    - The user names a specific active wildfire ("the Iron Fire near Eureka Utah",
      "the Santa Rosa Island fire") and you need its exact location + when it
      started before fetching imagery / drawing an AOI.
    - The upstream step of a satellite fire-animation workflow: resolve the
      incident -> bbox + a discovery-time sanity floor for the animation window.
    - Disambiguating a fire by name when a free-text geocode is too coarse.

    **When NOT to use:**
    - Active-fire pixel detections (use ``fetch_firms_active_fire``).
    - Fire perimeter polygons (use ``fetch_nifc_fire_perimeters``).
    - A generic place name with no named incident (use ``geocode_location``).
    - Historical / contained fires (WFIGS "Current" carries only active
      incidents; for past fires use ``fetch_mtbs_burn_severity``).

    **Parameters:**
    - ``incident_name`` (str): the incident name token, e.g. ``"Iron"`` or
      ``"Santa Rosa Island"``. A trailing " Fire" is stripped automatically and
      matching is case-insensitive, so ``"Iron Fire"`` also matches.
    - ``state`` (str, optional): a US state filter, ``"UT"`` or ``"US-UT"``
      (either form). Narrows a common name to the right region.
    - ``bbox_pad_deg`` (float, default 0.25): half-width in degrees of the AOI
      bbox built around the incident point (E-W widened by 1/cos(lat) so the AOI
      is roughly square on the ground).

    **Returns:** a JSON-compatible dict with ``incident_name``, ``lat``, ``lon``,
    ``bbox`` (``[min_lon, min_lat, max_lon, max_lat]`` EPSG:4326),
    ``fire_discovery_datetime`` (ISO-8601 UTC or null), ``incident_size_acres``,
    ``percent_contained``, ``poo_state``, ``poo_county``, ``irwin_id``,
    ``unique_fire_identifier``. Cached ``dynamic-1h``.

    **Cross-tool dependencies:**
    - Upstream of: ``fetch_goes_animation`` / ``fetch_viirs_day_fire`` (the AOI
      bbox + the discovery-time floor for the animation window),
      ``run_model_satellite_fire_animation``.
    - Pairs with: ``fetch_firms_active_fire`` + ``fetch_nifc_fire_perimeters``
      (co-registered hot-pixel + perimeter overlays around the resolved point).
    """
    if not isinstance(incident_name, str) or not incident_name.strip():
        raise WFIGSIncidentInputError(
            f"incident_name must be a non-empty string; got {incident_name!r}"
        )
    try:
        pad = float(bbox_pad_deg)
    except (TypeError, ValueError) as exc:
        raise WFIGSIncidentInputError(
            f"bbox_pad_deg must be a number; got {bbox_pad_deg!r}"
        ) from exc
    if not (0.0 < pad <= 10.0):
        raise WFIGSIncidentInputError(
            f"bbox_pad_deg must be in (0, 10]; got {bbox_pad_deg!r}"
        )
    state_norm = _normalize_state(state)

    name_key = incident_name.strip().lower()
    params = {
        "incident_name": name_key,
        "state": state_norm,
        "pad_deg": round(pad, 4),
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="json",
        fetch_fn=lambda: _resolve_incident_bytes(incident_name, state_norm, pad),
    )
    payload = json.loads(result.data.decode("utf-8"))
    logger.info(
        "fetch_wfigs_incident: resolved %r -> (%.5f, %.5f) disc=%s size=%s",
        payload.get("incident_name"),
        payload.get("lat"),
        payload.get("lon"),
        payload.get("fire_discovery_datetime"),
        payload.get("incident_size_acres"),
    )
    return payload
