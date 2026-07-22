"""``fetch_nws_event`` atomic tool — NWS active alerts/events fetcher (job-0090).

Wraps the National Weather Service ``api.weather.gov/alerts/active`` endpoint
and emits FlatGeobuf polygons + properties (severity, headline, event, onset,
ends, description, ...). Tier-1 free (no API key required); a descriptive
``User-Agent`` header is REQUIRED by NWS or the API returns 403.

Usage modes (``area`` polymorphism):

- 2-letter US state code ("FL", "TX", ...) → ``?area={STATE}``
- US county FIPS (5-digit string, e.g. "12071" for Lee County, FL) → ``?area=FIPS``
- bbox tuple ``(min_lon, min_lat, max_lon, max_lat)`` (EPSG:4326) → converted
  to a point center (lat, lon) and passed as ``?point={lat},{lon}`` for the
  zone lookup (NWS does not accept bbox queries directly; point lookup returns
  all alerts whose forecast zones contain that point).

Cache: ``dynamic-1h`` (FR-DC-2 active-state) — alerts change frequently, but
a one-hour bucket is the FR-DC-3 minimum window and keeps repeat queries
inside a short demo / research session cheap.

Cache key: SHA-256 of ``(area_canonicalized, event_types_sorted, status,
message_type)`` — see ``read_through`` for the full canonicalization rules.

Returns: ``LayerURI(layer_type="vector", role="context", units=None)`` pointing
at a FlatGeobuf in the cache bucket containing the alert polygons + properties.

FR-TA-2 / FR-AS-3 docstring discipline applies.

Geographic-correctness check (job-0086 lesson, codified):
The live test verifies that bbox→point conversion produces the EXACT center
of the input bbox (algebraic identity, not just round-trip), so a sign-flip
or axis-swap bug in the point computation surfaces as a wrong polygon.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import urllib.parse
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers.us_states import NWS_AREA_CODES, resolve_state_code

__all__ = [
    "fetch_nws_event",
    "NWSError",
    "NWSUpstreamError",
    "NWSInputError",
    "NWSEmptyError",
    "_bbox_to_point_center",
    "_canonicalize_area",
    "_build_nws_url",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_nws_event")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NWSError(RuntimeError):
    """Base class for fetch_nws_event failures.

    ``error_code`` maps to the WebSocket A.6 error frame the agent surface
    emits. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NWS_EVENT_ERROR"
    retryable: bool = True


class NWSInputError(NWSError):
    """Caller passed an invalid ``area``/``event_types``/``status``/``message_type``."""

    error_code = "NWS_EVENT_INPUT_INVALID"
    retryable = False


class NWSUpstreamError(NWSError):
    """api.weather.gov request failed (network, 5xx, malformed JSON).

    Marked retryable=True per audit.md (transient NWS issues recover on retry;
    the agent FR-AS-11 surface decides whether to actually re-issue).
    """

    error_code = "NWS_EVENT_UPSTREAM_ERROR"
    retryable = True


class NWSEmptyError(NWSError):
    """NWS returned an empty FeatureCollection — informational, not retryable.

    Empty results are LEGITIMATE for `fetch_nws_event` (no active alerts in
    the requested area is the most common steady state). Tests and callers
    treat this as a valid response, not an error — but it's surfaced as a
    typed subclass so consumers that DO want to assert non-emptiness can.

    Currently NOT raised by the tool body (we serialize an empty FGB instead),
    but kept available for future strict-mode opt-in.
    """

    error_code = "NWS_EVENT_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NWS_BASE = "https://api.weather.gov"

# REQUIRED per NWS policy — without a descriptive User-Agent identifying the
# app + contact, NWS returns HTTP 403. The kickoff spec calls this out.
_USER_AGENT = (
    "trid3nt-server/0.1 (Hazard Modeling Agent; contact: trid3nt-ops@local)"
)

# Valid status values per NWS alert schema.
_VALID_STATUSES = frozenset({"actual", "exercise", "system", "test", "draft"})

# Valid messageType values per NWS alert schema.
_VALID_MESSAGE_TYPES = frozenset({"alert", "update", "cancel"})

# 2-letter US state codes (50 + DC + 5 territories + marine zones) accepted
# by /alerts/active. Shared with fetch_nws_alerts_conus via us_states
# (job-0261) so the two NWS tools can never diverge.
_VALID_STATE_CODES = NWS_AREA_CODES

# 5-digit FIPS code pattern.
_FIPS_PATTERN = re.compile(r"^\d{5}$")

# Request timeout per audit.md.
_HTTP_TIMEOUT_S = 30.0

# Properties preserved from each NWS alert feature (audit.md spec).
# We keep the FULL set of NWS-documented properties so downstream visualization
# / styling has everything; the audit list is the MINIMUM.
_PRESERVED_PROPERTIES = (
    "event", "headline", "description", "severity", "urgency", "certainty",
    "effective", "onset", "ends", "expires", "senderName", "sender",
    "category", "messageType", "status", "areaDesc", "instruction",
    "response", "id",
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_nws_event",
    ttl_class="dynamic-1h",
    source_class="nws_event",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Area canonicalization + URL building.
# ---------------------------------------------------------------------------


def _bbox_to_point_center(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Return the (lat, lon) center of ``bbox = (min_lon, min_lat, max_lon, max_lat)``.

    Algebraic identity:
        lat_center = (min_lat + max_lat) / 2
        lon_center = (min_lon + max_lon) / 2

    Per the codified job-0086 lesson, the GEOGRAPHIC correctness of this
    function is what unit tests assert — NOT just "did the bytes survive".
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_center = (min_lat + max_lat) / 2.0
    lon_center = (min_lon + max_lon) / 2.0
    return (lat_center, lon_center)


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NWSInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NWSInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NWSInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NWSInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NWSInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NWSInputError(
            f"bbox degenerate (min must be < max on both axes): {bbox!r}"
        )


def _canonicalize_area(
    area: str | tuple[float, float, float, float],
) -> dict[str, Any]:
    """Reduce ``area`` to a stable {kind, value, ...} dict for cache-keying + URL building.

    Returns one of:
        {"kind": "state", "value": "FL"}
        {"kind": "fips", "value": "12071"}
        {"kind": "point", "lat": 26.6, "lon": -81.8, "bbox": [...]}
    """
    if isinstance(area, tuple):
        _validate_bbox(area)
        lat, lon = _bbox_to_point_center(area)
        # Round to 4dp (~11m) for cache-key stability — NWS zones are
        # much coarser than that, so the snap loses no useful precision.
        return {
            "kind": "point",
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "bbox": [round(v, 6) for v in area],
        }
    if isinstance(area, str):
        s = area.strip().upper()
        if _FIPS_PATTERN.match(s):
            return {"kind": "fips", "value": s}
        if s in _VALID_STATE_CODES:
            return {"kind": "state", "value": s}
        # job-0261: accept full state/territory names ("Texas", "state of
        # texas") — the LLM passes location text verbatim more often than it
        # abbreviates, and rejecting "Texas" pushed the live agent into the
        # unscoped CONUS sweep (alerts spilled beyond the named state).
        name_code = resolve_state_code(area)
        if name_code is not None:
            return {"kind": "state", "value": name_code}
        raise NWSInputError(
            f"area={area!r} is not a recognized US state name, 2-letter "
            f"state code, 5-digit county FIPS, or bbox tuple"
        )
    raise NWSInputError(
        f"area must be str (state code or FIPS) or tuple bbox; got {type(area).__name__}"
    )


def _build_nws_url(
    canon_area: dict[str, Any],
    event_types: list[str] | None,
    status: str,
    message_type: str,
) -> str:
    """Build the api.weather.gov/alerts/active URL for the canonicalized area.

    NWS supports repeatable ``&event=`` params for filtering; we URL-encode
    each. ``status`` and ``message_type`` are validated by the tool body.
    """
    params: list[tuple[str, str]] = []
    if canon_area["kind"] == "state":
        params.append(("area", canon_area["value"]))
    elif canon_area["kind"] == "fips":
        # NWS treats FIPS the same as state code via ?area= (zone lookup).
        params.append(("area", canon_area["value"]))
    elif canon_area["kind"] == "point":
        params.append(
            ("point", f"{canon_area['lat']},{canon_area['lon']}")
        )
    else:  # pragma: no cover — _canonicalize_area only emits the three above
        raise NWSInputError(f"unknown canon_area kind: {canon_area['kind']!r}")

    params.append(("status", status))
    params.append(("message_type", message_type))

    if event_types:
        for et in event_types:
            params.append(("event", et))

    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{_NWS_BASE}/alerts/active?{query}"


# ---------------------------------------------------------------------------
# Upstream call + GeoJSON → FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _fetch_nws_geojson(url: str) -> dict[str, Any]:
    """GET the NWS alerts URL with the required headers; return parsed JSON.

    Raises:
        ``NWSUpstreamError``: network / 5xx / non-JSON / malformed body.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/geo+json",
    }
    logger.info("fetch_nws_event: GET %s", url)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise NWSUpstreamError(
            f"NWS request failed url={url}: {exc}"
        ) from exc

    if resp.status_code == 403:
        raise NWSUpstreamError(
            f"NWS returned 403 — User-Agent header is required + must identify the app. "
            f"Sent: {_USER_AGENT!r}; url={url}"
        )
    if resp.status_code >= 400:
        raise NWSUpstreamError(
            f"NWS returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NWSUpstreamError(
            f"NWS returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise NWSUpstreamError(
            f"NWS response is not a GeoJSON FeatureCollection url={url}: type={body.get('type') if isinstance(body, dict) else type(body).__name__!r}"
        )

    return body


def _geojson_to_fgb(geojson: dict[str, Any]) -> bytes:
    """Convert an NWS GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves the audit.md-listed properties (event, headline, severity, ...)
    plus the rest of the NWS-documented fields. Features WITHOUT a geometry
    (NWS sometimes returns alerts that have only zone/county references) are
    materialized with a NULL geometry so the property table is still preserved
    — FlatGeobuf supports null geometries.

    Returns FlatGeobuf bytes (always non-empty: an empty FeatureCollection
    still yields a valid header-only FGB).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NWSUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    features = geojson.get("features", []) or []

    # Build a list of records that geopandas can ingest. We trim each feature's
    # properties to the preserved-set + 'geometry' so we don't bloat the FGB
    # with unbounded NWS fields. Missing properties become None.
    rows: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        row: dict[str, Any] = {}
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            # NWS sometimes returns nested objects/arrays in properties (e.g.
            # parameters, geocode). Coerce non-scalar values to JSON strings
            # so geopandas/pyogrio can write them — FlatGeobuf needs scalar
            # column types per field.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[key] = v
        row["geometry"] = feat.get("geometry")
        rows.append(row)

    if not rows:
        # Empty FeatureCollection — emit a minimal valid FGB with one row of
        # all-None and immediately filter it out. pyogrio refuses to write a
        # truly empty layer, so we use a sentinel approach: build a 1-row gdf,
        # then write only if non-empty; else write a zero-feature placeholder
        # JSON-shape that the cache still preserves. This mirrors what the
        # cache_path slot expects.
        # Simplest robust path: serialize empty as an empty GeoDataFrame
        # with the geometry column declared; pyogrio handles this.
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

    # NWS zone-based alerts (e.g. statewide watches) carry NULL geometry;
    # pyogrio's FlatGeobuf writer rejects them while building the spatial
    # index ("ICreateFeature: NULL geometry not supported"). Drop them --
    # they have no map footprint to draw anyway (2026-07-06 local sweep).
    n_null = int(gdf.geometry.isna().sum()) if len(gdf) else 0
    if n_null:
        gdf = gdf[gdf.geometry.notna()]
        logger.info(
            "fetch_nws_event: dropped %d geometry-less zone alert(s) before "
            "FlatGeobuf write (%d drawable remain)",
            n_null,
            len(gdf),
        )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nws_"
        ) as f:
            tmp_fgb = f.name
        try:
            # pyogrio is the geopandas default writer; FlatGeobuf is its
            # native fast path. Use SPATIAL_INDEX=NO for empty layers
            # (pyogrio errors if the input has zero features and we request
            # a spatial index).
            if len(gdf) == 0:
                gdf.to_file(
                    tmp_fgb, driver="FlatGeobuf", engine="pyogrio",
                    SPATIAL_INDEX="NO",
                )
            else:
                gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NWSUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_nws_event: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_nws_event_bytes(
    canon_area: dict[str, Any],
    event_types: list[str] | None,
    status: str,
    message_type: str,
) -> bytes:
    """End-to-end fetcher: build URL → GET JSON → convert to FlatGeobuf bytes.

    Wrapped in a single try so we never leak an httpx exception past the typed
    error boundary.
    """
    url = _build_nws_url(canon_area, event_types, status, message_type)
    geojson = _fetch_nws_geojson(url)
    return _geojson_to_fgb(geojson)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_nws_event(
    area: str | tuple[float, float, float, float],
    event_types: list[str] | None = None,
    status: str = "actual",
    message_type: str = "alert",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch active National Weather Service alerts scoped to a US state, county, or bbox.

    **What it does:** Issues a call to ``api.weather.gov/alerts/active``
    filtered to a specific US geographic scope — a 2-letter state code, a
    5-digit county FIPS, or a bbox (converted to a point center for the NWS
    forecast-zone lookup). Returns alert polygons with severity, headline,
    event type, onset/ends timestamps, and instruction text as a FlatGeobuf
    vector layer. Cached ``dynamic-1h`` (FR-DC-2 active-state).

    **When to use:**
    - User asks about current weather hazards in a specific state or county
      (e.g. "are there any flood warnings in Lee County, FL?").
    - Agent needs geographically scoped alerts to drive the Hazard Event
      Pipeline for a bounded study area rather than the full CONUS sweep.
    - Workflow requires per-FIPS or per-state alert filtering before
      intersecting with a hazard footprint.

    **When NOT to use:**
    - CONUS-wide alert sweeps — use ``fetch_nws_alerts_conus`` instead
      (single call, no area argument required).
    - Historical alert lookups (NWS active-alerts is current-only, 0-7 days);
      use ``fetch_storm_events_db`` for past events.
    - Rainfall return periods or river forecast data (different NWS surfaces).
    - International weather alerts (NWS is US/territory-only).

    **Parameters:**
    - ``area`` (str or tuple): 2-letter state code (``"FL"``) or full state
      name (``"Florida"``, case-insensitive), 5-digit county FIPS
      (``"12071"`` for Lee County FL), or bbox tuple
      ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (converted to
      point center for NWS zone lookup — use state/FIPS when the user names
      one; for sub-state areas prefer clipping to the admin polygon via
      ``fetch_administrative_boundaries`` + ``clip_vector_to_polygon``
      instead of trusting a rectangle).
    - ``event_types`` (list[str] or None): NWS event-type filter strings, e.g.
      ``["Hurricane Warning", "Flood Warning"]``. ``None`` returns all types.
    - ``status`` (str): one of ``"actual"`` (default), ``"exercise"``,
      ``"system"``, ``"test"``, ``"draft"``.
    - ``message_type`` (str): one of ``"alert"`` (default), ``"update"``,
      ``"cancel"``.

    **Returns:**
    ``LayerURI(layer_type="vector", role="context", units=None)`` pointing at
    a FlatGeobuf with fields: ``event``, ``headline``, ``description``,
    ``severity``, ``urgency``, ``certainty``, ``effective``, ``onset``,
    ``ends``, ``expires``, ``senderName``, ``areaDesc``, ``instruction``,
    ``response``, ``id``.

    **Cross-tool dependencies:**
    - Narrower-scope alternative to: ``fetch_nws_alerts_conus`` (CONUS sweep).
    - Upstream of: hazard-event-pipeline tools that consume NWS alert
      geometry + severity metadata as forcing evidence (FR-HEP-2 Tier 1).
    - For historical context: pair with ``fetch_storm_events_db``.
    """
    # Validate status / message_type early — the kickoff says these have
    # fixed enums on the NWS side. Bad values are caller error, not retryable.
    if status not in _VALID_STATUSES:
        raise NWSInputError(
            f"status={status!r} not in {sorted(_VALID_STATUSES)}"
        )
    if message_type not in _VALID_MESSAGE_TYPES:
        raise NWSInputError(
            f"message_type={message_type!r} not in {sorted(_VALID_MESSAGE_TYPES)}"
        )

    canon_area = _canonicalize_area(area)

    # Sort event_types for cache-key stability (per audit.md "event_types sorted").
    # None and [] are equivalent — both mean "no filter".
    sorted_event_types: list[str] | None = None
    if event_types:
        if not all(isinstance(e, str) for e in event_types):
            raise NWSInputError(
                f"event_types must be list[str]; got {event_types!r}"
            )
        sorted_event_types = sorted({e.strip() for e in event_types if e.strip()})
        if not sorted_event_types:
            sorted_event_types = None

    params: dict[str, Any] = {
        "area": canon_area,
        "event_types": sorted_event_types,
        "status": status,
        "message_type": message_type,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nws_event_bytes(
            canon_area, sorted_event_types, status, message_type,
        ),
    )
    assert result.uri is not None, (
        "fetch_nws_event is cacheable; uri must be set by read_through"
    )

    # LayerURI display name reflects the area kind for diagnostics.
    if canon_area["kind"] == "state":
        area_label = f"State {canon_area['value']}"
        layer_id = f"nws-state-{canon_area['value']}"
    elif canon_area["kind"] == "fips":
        area_label = f"FIPS {canon_area['value']}"
        layer_id = f"nws-fips-{canon_area['value']}"
    else:  # point
        area_label = (
            f"Point ({canon_area['lat']:.4f}, {canon_area['lon']:.4f})"
        )
        layer_id = (
            f"nws-point-{canon_area['lat']:.4f}-{canon_area['lon']:.4f}"
        )

    return LayerURI(
        layer_id=layer_id,
        name=f"NWS Active Alerts — {area_label}",
        layer_type="vector",
        uri=result.uri,
        style_preset="nws_alerts",  # placeholder; NWS-specific QML preset is a follow-up
        role="context",
        units=None,
    )
