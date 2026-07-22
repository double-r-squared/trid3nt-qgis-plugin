"""``fetch_nws_alerts_conus`` atomic tool — NWS CONUS-wide active alerts (job-0105).

CONUS-wide companion to ``fetch_nws_event`` (job-0090). Where ``fetch_nws_event``
takes ``area = state | county-FIPS | bbox``, this sibling fetches ALL active
alerts nationwide in a single call — the right tool for "show me current
warnings across America" use cases. NWS typically has ~500 active alerts
nationwide at any moment; a single CONUS sweep is far cheaper than 50 state
calls.

F88 zone-reference resolution: NWS alerts frequently carry NULL inline geometry
and reference their affected areas by ``properties.affectedZones`` (NWS zone API
URLs) and/or ``properties.geocode.UGC`` codes. Such alerts have nothing to draw,
so a mostly/entirely zone-referenced alert set rendered NO polygons (NATE's live
Florida test: "7 active alerts" + zero polygons). The converter now RESOLVES
those zone references to real polygons (``affectedZones`` primary, ``geocode.UGC``
fallback) before the FlatGeobuf write so MapLibre actually draws them. Zone
fetches are de-duped per call (alerts share zones) and capped. Size note: a
state-scoped sweep stays small (~a few seconds, well under 1 MB); the unscoped
nationwide sweep resolves ~1700 distinct zones and is correspondingly larger
(~18 MB FGB at peak, under the 25 MB warn threshold), paid once per cache hour.

Endpoint:
    https://api.weather.gov/alerts/active?status={status}
    https://api.weather.gov/alerts/active?area={STATE}&status={status}  (job-0261)
    https://api.weather.gov/zones/forecast/{UGC}  (F88 zone resolution)
    https://api.weather.gov/zones/county/{UGC}    (F88 zone resolution)

job-0261 state-aware path: the live demo "weather alerts for Texas" rendered
alerts in surrounding states because this tool had NO geographic filter and
the state-scoped sibling was outside the allowed set. The optional ``area``
param now accepts a US state (2-letter code OR full name, e.g. "TX" /
"Texas") and applies NWS's precise server-side ``?area=`` filter so a named
state never spills into its neighbors. When ``area`` is omitted the original
unscoped CONUS sweep is preserved.

``event_types`` filtering is applied client-side after fetch (preserving
cache reuse across event-type filters of a single sweep).

Cache: ``dynamic-1h`` — alerts change frequently; one-hour bucketing matches
the FR-DC-3 minimum window for active-state data.

Returns: ``LayerURI(layer_type="vector", role="primary", units=None)`` pointing
at a FlatGeobuf in the cache bucket of CONUS alert polygons + properties.

FR-TA-2 / FR-AS-3 docstring discipline applies; NWS REQUIRES a descriptive
``User-Agent`` (returns 403 otherwise).

Geographic-correctness check (job-0086 lesson, codified):
The live test verifies that every returned alert with a polygon geometry
falls inside the CONUS+territories envelope (or marine zones); a sign-flip
or axis-swap in the GeoJSON→FGB conversion would surface as features outside
that envelope.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers.us_states import resolve_state_code, state_display_name

__all__ = [
    "fetch_nws_alerts_conus",
    "NWSConusError",
    "NWSConusInputError",
    "NWSConusUpstreamError",
    "NWSConusEmptyError",
    "_build_nws_conus_url",
    "_filter_features_by_event_types",
    "_geojson_to_fgb",
    "_resolve_area_or_raise",
    "_ugc_to_zone_url",
    "_zone_urls_for_feature",
    "_fetch_zone_geometry",
    "_resolve_zone_geometries",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NWSConusError(RuntimeError):
    """Base class for fetch_nws_alerts_conus failures.

    ``error_code`` maps to the WebSocket A.6 error frame the agent surface
    emits. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NWS_CONUS_ERROR"
    retryable: bool = True


class NWSConusInputError(NWSConusError):
    """Caller passed an invalid ``status`` or non-string ``event_types``."""

    error_code = "NWS_CONUS_INPUT_INVALID"
    retryable = False


class NWSConusUpstreamError(NWSConusError):
    """api.weather.gov request failed (network, 5xx, malformed JSON).

    Marked retryable=True (transient NWS issues recover on retry; the agent's
    FR-AS-11 surface decides whether to actually re-issue).
    """

    error_code = "NWS_CONUS_UPSTREAM_ERROR"
    retryable = True


class NWSConusEmptyError(NWSConusError):
    """NWS returned an empty FeatureCollection — informational, not retryable.

    Empty results are LEGITIMATE for CONUS-wide queries during very quiet
    weather periods (rare but possible). Currently NOT raised by the tool
    body (we serialize an empty FGB instead), but kept available for future
    strict-mode opt-in.
    """

    error_code = "NWS_CONUS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NWS_BASE = "https://api.weather.gov"

# REQUIRED per NWS policy — without a descriptive User-Agent identifying the
# app + contact, NWS returns HTTP 403.
_USER_AGENT = (
    "trid3nt-server/0.1 (Hazard Modeling Agent; contact: trid3nt-ops@local)"
)

# Valid status values per NWS alert schema.
_VALID_STATUSES = frozenset({"actual", "exercise", "system", "test", "draft"})

# Request timeout — for the RAW /alerts/active JSON list GET (the alert list
# itself is small, ~200KB regardless of scope); 30s is generous. The much
# larger zone-geometry resolution (F88) runs as separate GETs bounded by
# _ZONE_HTTP_TIMEOUT_S, not this timeout.
_HTTP_TIMEOUT_S = 30.0

# F88 zone-resolution discipline. NWS alerts frequently carry NULL inline
# geometry and instead reference affected areas by ``properties.affectedZones``
# (a list of NWS zone API URLs) and/or ``properties.geocode.UGC`` codes. A
# NULL-geometry row has nothing to draw, so we resolve those zone references to
# real polygons before the FlatGeobuf write.
#
# Payload discipline (per the data-source-fallback norm + payload caps):
# - DEDUPE zone fetches across alerts (alerts share zones) via an in-call cache.
# - Bound per-zone request time with a short timeout.
# - Cap the TOTAL number of distinct zone fetches per call so a pathological
#   sweep can't fan out without bound. The cap is sized to comfortably cover a
#   full nationwide sweep (empirically ~1700 distinct zones at peak), so the
#   common case never clips; it only guards against a degenerate response. The
#   work is bounded by the 1h cache TTL — a CONUS sweep resolves zones once per
#   hour. Size/latency note: a state-scoped sweep (the recommended path for a
#   named state) resolves in a few seconds and stays small; the unscoped
#   nationwide sweep resolves ~1700 zones (~150s, ~18 MB FGB at peak) — under
#   the 25 MB payload-warning threshold, paid once per cache hour, and the
#   price of actually DRAWING the alerts rather than rendering an empty layer.
_ZONE_HTTP_TIMEOUT_S = 15.0
_MAX_ZONE_FETCHES = 2500

# Properties preserved from each NWS alert feature (mirrors fetch_nws_event).
_PRESERVED_PROPERTIES = (
    "event", "headline", "description", "severity", "urgency", "certainty",
    "effective", "onset", "ends", "expires", "senderName", "sender",
    "category", "messageType", "status", "areaDesc", "instruction",
    "response", "id",
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# supports_global_query=True (resolves OQ-0105-GLOBAL-QUERY-FIELD): the
# natural use of this tool IS the unscoped nationwide sweep
# (``/alerts/active``) — "show me current warnings across America". The raw
# alert LIST is small (~500 active alerts, ~200KB) regardless of scope; after
# F88 zone-geometry resolution the rendered FGB peaks at ~18 MB for the full
# nationwide sweep (state-scoped stays a few hundred KB) — still under the
# 25 MB payload-warning threshold and paid once per 1h cache, so a no-bbox
# global query remains both meaningful and payload-safe. The optional ``area``
# param narrows to a single state when supplied; omitting it is the CONUS-wide
# default, not an error. (Field landed via the Wave 1.5 schema amendment,
# job-0114.)
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_nws_alerts_conus",
    ttl_class="dynamic-1h",
    source_class="nws_alerts_conus",
    cacheable=True,
    # CONUS-wide /alerts/active sweep is the primary use; ~200KB alert list,
    # up to ~18 MB rendered FGB nationwide post-F88 zone resolution (< 25 MB
    # warn threshold, cached 1h). See the rationale block above.
    supports_global_query=True,
)


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_nws_conus_url(status: str, area_code: str | None = None) -> str:
    """Build the api.weather.gov/alerts/active URL for the sweep.

    ``area_code`` (job-0261): a resolved 2-letter NWS area code ("TX"). When
    provided, NWS filters server-side via ``?area={code}`` — the precise
    state-scoped query that prevents a named state's alerts from spilling
    into neighbors. When ``None``, the original unscoped CONUS sweep URL is
    returned.

    ``event_types`` filtering is applied client-side after fetch so the
    payload can be reused across queries that differ only in event-type
    filter (same cache hit).

    Note: ``message_type`` is intentionally NOT sent — omitting it returns
    the full union of active alert/update messages, which matches the
    "show me all warnings" semantics.
    """
    # No urlencode needed — ASCII enum values only.
    if area_code:
        return f"{_NWS_BASE}/alerts/active?area={area_code}&status={status}"
    return f"{_NWS_BASE}/alerts/active?status={status}"


def _resolve_area_or_raise(area: str | None) -> str | None:
    """Resolve the LLM-supplied ``area`` to a 2-letter NWS code, or raise.

    - ``None`` / empty string → ``None`` (unscoped CONUS sweep).
    - "TX" / "Texas" / "state of texas" → "TX".
    - Anything unrecognized → ``NWSConusInputError`` (non-retryable) with
      guidance so Gemini routes county/bbox-scoped queries to
      ``fetch_nws_event`` instead of silently rendering nationwide alerts.
    """
    if area is None:
        return None
    if not isinstance(area, str):
        raise NWSConusInputError(
            f"area must be a US state name or 2-letter code (str); "
            f"got {type(area).__name__}"
        )
    if not area.strip():
        return None
    code = resolve_state_code(area)
    if code is None:
        raise NWSConusInputError(
            f"area={area!r} is not a recognized US state/territory name or "
            f"2-letter code. For county-FIPS or bbox scoping use "
            f"fetch_nws_event; omit area entirely for the nationwide sweep."
        )
    return code


# ---------------------------------------------------------------------------
# Client-side event_types filter.
# ---------------------------------------------------------------------------


def _filter_features_by_event_types(
    features: list[dict[str, Any]],
    event_types: list[str] | None,
) -> list[dict[str, Any]]:
    """Narrow ``features`` to those whose ``properties.event`` matches one of
    ``event_types``.

    Returns ``features`` unchanged when ``event_types`` is None or empty.
    Comparison is case-sensitive against the canonical NWS event-type strings
    (e.g. "Hurricane Warning", "Flood Watch") — NWS uses Title Case throughout.
    """
    if not event_types:
        return features
    allowed = {e.strip() for e in event_types if isinstance(e, str) and e.strip()}
    if not allowed:
        return features
    out: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        ev = props.get("event")
        if isinstance(ev, str) and ev in allowed:
            out.append(feat)
    return out


# ---------------------------------------------------------------------------
# Upstream call + GeoJSON → FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _fetch_nws_conus_geojson(url: str) -> dict[str, Any]:
    """GET the NWS CONUS-wide alerts URL with required headers; return parsed JSON.

    Raises:
        ``NWSConusUpstreamError``: network / 5xx / non-JSON / malformed body.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/geo+json",
    }
    logger.info("fetch_nws_alerts_conus: GET %s", url)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise NWSConusUpstreamError(
            f"NWS CONUS request failed url={url}: {exc}"
        ) from exc

    if resp.status_code == 403:
        raise NWSConusUpstreamError(
            f"NWS returned 403 — User-Agent header is required + must identify the app. "
            f"Sent: {_USER_AGENT!r}; url={url}"
        )
    if resp.status_code >= 400:
        raise NWSConusUpstreamError(
            f"NWS returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NWSConusUpstreamError(
            f"NWS returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise NWSConusUpstreamError(
            f"NWS response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type') if isinstance(body, dict) else type(body).__name__!r}"
        )

    return body


# ---------------------------------------------------------------------------
# F88 zone-reference → polygon resolution.
#
# NWS alerts that lack inline geometry reference affected areas two ways:
#   1. properties.affectedZones — a list of full NWS zone API URLs, e.g.
#      "https://api.weather.gov/zones/forecast/LAZ091" (public/forecast zones)
#      or "https://api.weather.gov/zones/county/ILC011" (county zones). This is
#      the PRIMARY source: the URL already encodes the correct zone type.
#   2. properties.geocode.UGC — a list of UGC codes, e.g. "LAZ091" / "ILC011".
#      The 3rd character selects the zone type: "Z" → forecast zone,
#      "C" → county. This is the FALLBACK when affectedZones is absent/empty.
#
# Each zone URL resolves to a GeoJSON Feature whose .geometry is a Polygon or
# MultiPolygon. We attach the union of all resolved zone polygons to the alert.
# ---------------------------------------------------------------------------


def _ugc_to_zone_url(ugc: str) -> str | None:
    """Map a UGC code (e.g. "LAZ091", "ILC011") to its NWS zone API URL.

    The 3rd character of a UGC selects the zone collection:
      - "Z" → ``/zones/forecast/{UGC}`` (public/forecast zones)
      - "C" → ``/zones/county/{UGC}`` (county zones)

    Returns ``None`` for codes too short or of an unrecognized type (logged by
    the caller); we never guess a URL we can't justify.
    """
    if not isinstance(ugc, str):
        return None
    code = ugc.strip().upper()
    if len(code) < 3:
        return None
    kind = code[2]
    if kind == "Z":
        return f"{_NWS_BASE}/zones/forecast/{code}"
    if kind == "C":
        return f"{_NWS_BASE}/zones/county/{code}"
    return None


def _zone_urls_for_feature(props: dict[str, Any]) -> list[str]:
    """Collect ordered, de-duplicated zone API URLs for an alert's properties.

    Primary: ``properties.affectedZones`` (full URLs, correct zone type baked
    in). Fallback: ``properties.geocode.UGC`` codes mapped to URLs via
    ``_ugc_to_zone_url``. The two sources are unioned (affectedZones first) so
    that even a partially-populated alert resolves as many zones as possible.
    """
    seen: set[str] = set()
    urls: list[str] = []

    affected = props.get("affectedZones")
    if isinstance(affected, list):
        for u in affected:
            if isinstance(u, str) and u.strip():
                clean = u.strip()
                if clean not in seen:
                    seen.add(clean)
                    urls.append(clean)

    geocode = props.get("geocode")
    if isinstance(geocode, dict):
        ugc_list = geocode.get("UGC")
        if isinstance(ugc_list, list):
            for ugc in ugc_list:
                url = _ugc_to_zone_url(ugc)
                if url is not None and url not in seen:
                    seen.add(url)
                    urls.append(url)

    return urls


def _fetch_zone_geometry(url: str, client: httpx.Client) -> dict[str, Any] | None:
    """GET one NWS zone API URL; return its GeoJSON geometry dict, or ``None``.

    Per the data-source-fallback norm: a zone that fails to resolve (network
    error, 4xx/5xx, missing/empty geometry) is logged honestly and skipped —
    NEVER fabricated. The alert row is still kept by the caller so the property
    table survives even when no zone could be resolved.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/geo+json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("fetch_nws_alerts_conus: zone fetch failed %s: %s", url, exc)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "fetch_nws_alerts_conus: zone %s returned HTTP %s", url, resp.status_code
        )
        return None

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("fetch_nws_alerts_conus: zone %s non-JSON: %s", url, exc)
        return None

    if not isinstance(body, dict):
        return None
    geom = body.get("geometry")
    if not isinstance(geom, dict) or not geom.get("type") or not geom.get("coordinates"):
        logger.warning("fetch_nws_alerts_conus: zone %s has no usable geometry", url)
        return None
    return geom


def _union_geometries(geoms: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Union a list of GeoJSON Polygon/MultiPolygon geometries into one.

    A single geometry is returned as-is; multiple are merged into one
    MultiPolygon by concatenating their polygon rings. Returns ``None`` for an
    empty input. We do not do a true topological union (no shapely dependency
    needed) — for NWS zone polygons a MultiPolygon of the constituent zones is
    exactly the renderable affected-area, which is all the map needs.
    """
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]

    polygons: list[Any] = []
    for geom in geoms:
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon" and isinstance(coords, list):
            polygons.append(coords)
        elif gtype == "MultiPolygon" and isinstance(coords, list):
            polygons.extend(coords)
    if not polygons:
        return None
    return {"type": "MultiPolygon", "coordinates": polygons}


def _resolve_zone_geometries(geojson: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``geojson`` with NULL-geometry alerts resolved to polygons.

    For each feature lacking a usable inline geometry, resolve its zone
    references (``affectedZones`` primary, ``geocode.UGC`` fallback) to real
    polygons via the NWS zone API and attach their union. Features that already
    carry geometry are passed through untouched.

    Payload discipline:
    - One ``httpx.Client`` is reused for all zone GETs (connection pooling).
    - Zone fetches are DE-DUPED in an in-call cache keyed by URL — alerts
      heavily share zones, so a CONUS sweep resolves far fewer distinct zones
      than it has alerts.
    - Total distinct zone fetches are capped at ``_MAX_ZONE_FETCHES``; once the
      cap is hit, further unseen zones are skipped (logged), and the affected
      alert keeps whatever zones resolved before the cap (or NULL geometry).

    Per the data-source-fallback norm: an alert whose zones cannot be resolved
    keeps its row (property table preserved) with NULL geometry and an honest
    log line — geometry is never fabricated.
    """
    features = geojson.get("features", []) or []

    # Which features even need resolution? (NULL / missing inline geometry.)
    needs = [
        f for f in features
        if isinstance(f, dict) and not f.get("geometry")
    ]
    if not needs:
        return geojson

    # In-call zone cache: url -> geometry dict | None (None = tried, failed).
    zone_cache: dict[str, dict[str, Any] | None] = {}
    fetched = 0
    capped = False

    resolved_count = 0
    unresolved_count = 0

    with httpx.Client(timeout=_ZONE_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        out_features: list[Any] = []
        for feat in features:
            if not isinstance(feat, dict) or feat.get("geometry"):
                out_features.append(feat)
                continue

            props = feat.get("properties") or {}
            urls = _zone_urls_for_feature(props)
            zone_geoms: list[dict[str, Any]] = []
            for url in urls:
                if url in zone_cache:
                    cached = zone_cache[url]
                    if cached is not None:
                        zone_geoms.append(cached)
                    continue
                if fetched >= _MAX_ZONE_FETCHES:
                    capped = True
                    continue
                fetched += 1
                geom = _fetch_zone_geometry(url, client)
                zone_cache[url] = geom
                if geom is not None:
                    zone_geoms.append(geom)

            unioned = _union_geometries(zone_geoms)
            if unioned is not None:
                # Shallow-copy the feature so the input collection is untouched.
                new_feat = dict(feat)
                new_feat["geometry"] = unioned
                out_features.append(new_feat)
                resolved_count += 1
            else:
                # Keep the row (property table) but leave geometry NULL. Honest
                # log; never fabricate. Downstream FGB write tolerates NULL geom.
                out_features.append(feat)
                unresolved_count += 1
                logger.info(
                    "fetch_nws_alerts_conus: alert id=%s event=%r could not "
                    "resolve any of %d zone reference(s) — kept with NULL geometry",
                    props.get("id"),
                    props.get("event"),
                    len(urls),
                )

    if capped:
        logger.warning(
            "fetch_nws_alerts_conus: zone-fetch cap (%d) reached; some alerts "
            "may render with partial or NULL geometry",
            _MAX_ZONE_FETCHES,
        )
    logger.info(
        "fetch_nws_alerts_conus: zone resolution — %d alert(s) resolved to "
        "polygons, %d kept with NULL geometry, %d distinct zone fetch(es)",
        resolved_count,
        unresolved_count,
        fetched,
    )

    return {
        "type": "FeatureCollection",
        "features": out_features,
    }


def _geojson_to_fgb(geojson: dict[str, Any]) -> bytes:
    """Convert an NWS GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves ``_PRESERVED_PROPERTIES``. Zone-referenced alerts (NULL inline
    geometry) should already have been resolved to real polygons upstream by
    ``_resolve_zone_geometries`` (F88). Any feature that STILL lacks a geometry
    here (a zone that could not be resolved) is materialized with a NULL
    geometry so the property table is preserved — never fabricated.

    Returns FlatGeobuf bytes (always non-empty: an empty FeatureCollection
    still yields a valid header-only FGB).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NWSConusUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    features = geojson.get("features", []) or []

    rows: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        row: dict[str, Any] = {}
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            # Coerce non-scalar values to JSON strings — FlatGeobuf needs
            # scalar column types per field.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[key] = v
        row["geometry"] = feat.get("geometry")
        rows.append(row)

    if not rows:
        gdf = gpd.GeoDataFrame(
            {k: [] for k in _PRESERVED_PROPERTIES},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(
            [
                {
                    "type": "Feature",
                    "properties": {k: r[k] for k in _PRESERVED_PROPERTIES},
                    "geometry": r["geometry"],
                }
                for r in rows
            ],
            crs="EPSG:4326",
        )

    # NWS CONUS responses commonly include features with NULL geometry
    # (alerts that carry only zone/county references). FlatGeobuf's spatial-
    # index code path rejects NULL geometries (pyogrio: "ICreateFeature: NULL
    # geometry not supported with spatial index"). For the CONUS sweep we
    # disable the spatial index whenever ANY null geometry is present so the
    # property table is preserved for downstream identify/style use cases.
    # Trade-off: read paths that rely on FGB's spatial index skip those rows.
    # Acceptable for a context overlay layer.
    has_null_geom = bool(len(gdf)) and bool(gdf.geometry.isna().any())

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nws_conus_"
        ) as f:
            tmp_fgb = f.name
        try:
            if len(gdf) == 0 or has_null_geom:
                gdf.to_file(
                    tmp_fgb, driver="FlatGeobuf", engine="pyogrio",
                    SPATIAL_INDEX="NO",
                )
            else:
                gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NWSConusUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_nws_alerts_conus: FlatGeobuf = %d bytes (%d feature(s))",
            len(fgb_bytes),
            len(gdf),
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _fetch_nws_alerts_conus_bytes(
    status: str,
    event_types: list[str] | None,
    area_code: str | None = None,
) -> bytes:
    """End-to-end fetcher: build URL → GET GeoJSON → client-side filter →
    convert to FlatGeobuf bytes.

    ``area_code`` (job-0261): resolved 2-letter NWS code for the precise
    server-side state filter; ``None`` keeps the unscoped CONUS sweep.

    Wrapped in a single try so we never leak an httpx exception past the typed
    error boundary.
    """
    url = _build_nws_conus_url(status, area_code)
    geojson = _fetch_nws_conus_geojson(url)
    features = geojson.get("features", []) or []
    filtered = _filter_features_by_event_types(features, event_types)
    # Re-wrap into FeatureCollection for the converter.
    filtered_collection = {
        "type": "FeatureCollection",
        "features": filtered,
    }
    # F88: resolve zone-referenced alerts (NULL inline geometry) to real
    # polygons BEFORE conversion so they carry drawable geometry into the FGB.
    # Done after the event-type filter so we never fetch zones for alerts the
    # caller is discarding.
    resolved_collection = _resolve_zone_geometries(filtered_collection)
    return _geojson_to_fgb(resolved_collection)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls api.weather.gov external REST API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_nws_alerts_conus(
    event_types: list[str] | None = None,
    status: str = "actual",
    area: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch active National Weather Service alerts — nationwide, or precisely scoped to one US state.

    **What it does:** Calls ``api.weather.gov/alerts/active`` and returns the
    active NWS alert polygons as a FlatGeobuf vector layer. Alerts that carry
    only zone/county references (NULL inline geometry) have their affected zones
    resolved to real polygons (F88) so they actually draw on the map. With
    ``area`` omitted it sweeps the entire US (typically ~500 features; the
    nationwide sweep resolves ~1700 zones and runs larger/slower, ~18 MB FGB at
    peak, paid once per cache hour). With ``area`` set to a US state ("TX" or
    "Texas") it applies NWS's precise server-side state filter (``?area=TX``) so
    ONLY that state's alerts are returned — never the surrounding states, and
    the zone resolution stays small and fast. Optional client-side event-type
    filtering narrows the result without issuing a new upstream call.

    **When to use:**
    - User asks for weather alerts in a SPECIFIC STATE ("weather alerts for
      Texas") — ALWAYS pass ``area="TX"`` (2-letter code) or ``area="Texas"``
      (full name). Without ``area`` the result is nationwide and will render
      alerts far outside the state the user asked about.
    - User asks "show me every hurricane warning in America right now" or
      "summarize today's severe weather nationwide" — omit ``area`` for the
      single CONUS sweep (far cheaper than 50 state-level calls).
    - The Hazard Event Pipeline needs all active alerts matching a hazard
      type (e.g. all Flash Flood Watches) without a known state.
    - Situational-awareness overlay for a multi-state or national dashboard.

    **When NOT to use:**
    - County- or bbox-scoped queries — use ``fetch_nws_event`` with a 5-digit
      county FIPS or a bbox; for sub-state areas of interest, clip the result
      to the admin polygon (``fetch_administrative_boundaries`` +
      ``clip_vector_to_polygon``) rather than trusting a rectangle.
    - Historical weather events — NWS active-alerts is current-only (0–7 day
      horizon); use ``fetch_storm_events_db`` for historical data.
    - International alerts — NWS covers US states, territories, and marine
      zones only.

    **Parameters:**
    - ``event_types`` (list[str] | None, default None): Optional NWS event-type
      filter. Examples: ``["Hurricane Warning"]``, ``["Flood Warning",
      "Flash Flood Watch"]``. Filtering is case-sensitive Title Case (NWS
      convention). None means return all active alert types.
    - ``status`` (str, default ``"actual"``): NWS alert status. Valid values:
      ``"actual"``, ``"exercise"``, ``"system"``, ``"test"``, ``"draft"``.
      Always use ``"actual"`` for production use.
    - ``area`` (str | None, default None): US state/territory scope. Accepts
      a 2-letter code (``"TX"``) or full name (``"Texas"``, case-insensitive).
      None means nationwide. Unrecognized values raise a non-retryable input
      error (cities/counties are NOT valid — geocode or use
      ``fetch_nws_event`` for those).

    **Returns:**
    A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
    ``s3://trid3nt-cache/cache/dynamic-1h/nws_alerts_conus/<key>.fgb``
    containing the alert polygons. Properties: ``event``, ``headline``,
    ``description``, ``severity``, ``urgency``, ``certainty``, ``effective``,
    ``onset``, ``ends``, ``expires``, ``senderName``, ``areaDesc``,
    ``instruction``. ``layer_type="vector"``, ``role="primary"``.
    Cached for 1 hour (``dynamic-1h``), keyed per (status, area, event_types).
    Source-tier FR-HEP-2 Tier 1.

    **Cross-tool dependencies:**
    - No upstream tool required (no bbox needed; state scoping is built in).
    - Sibling to: ``fetch_nws_event`` (county-FIPS/bbox-scoped variant).
    - Feeds: Hazard Event Pipeline claim aggregation; map overlay display.
    """
    # Validate status early — fixed enum on the NWS side. Bad values are caller
    # error, not retryable.
    if status not in _VALID_STATUSES:
        raise NWSConusInputError(
            f"status={status!r} not in {sorted(_VALID_STATUSES)}"
        )

    # Resolve the state scope (job-0261). None → nationwide sweep; "Texas" /
    # "TX" → server-side ?area=TX filter; garbage → typed input error.
    area_code = _resolve_area_or_raise(area)

    # Validate event_types shape.
    sorted_event_types: list[str] | None = None
    if event_types:
        if not all(isinstance(e, str) for e in event_types):
            raise NWSConusInputError(
                f"event_types must be list[str]; got {event_types!r}"
            )
        sorted_event_types = sorted({e.strip() for e in event_types if e.strip()})
        if not sorted_event_types:
            sorted_event_types = None

    params: dict[str, Any] = {
        "status": status,
        "event_types": sorted_event_types,
        "area": area_code,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nws_alerts_conus_bytes(
            status, sorted_event_types, area_code,
        ),
    )
    assert result.uri is not None, (
        "fetch_nws_alerts_conus is cacheable; uri must be set by read_through"
    )

    # LayerURI display name reflects the scope + filter for diagnostics.
    scope_label = (
        f"{state_display_name(area_code)} ({area_code})" if area_code else "CONUS"
    )
    scope_slug = area_code if area_code else "conus"
    if sorted_event_types:
        filter_label = ", ".join(sorted_event_types[:3])
        if len(sorted_event_types) > 3:
            filter_label += f", +{len(sorted_event_types) - 3} more"
        name = f"NWS Active Alerts — {scope_label} ({filter_label})"
        layer_id = (
            f"nws-{scope_slug}-{status}-"
            + "-".join(t.replace(" ", "_") for t in sorted_event_types[:2])
        )
    else:
        name = f"NWS Active Alerts — {scope_label} (all events)"
        layer_id = f"nws-{scope_slug}-{status}-all"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="nws_alerts",  # shared with fetch_nws_event preset
        role="primary",
        units=None,
    )
