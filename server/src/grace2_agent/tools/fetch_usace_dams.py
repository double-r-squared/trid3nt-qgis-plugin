"""``fetch_usace_dams`` atomic tool — USACE National Inventory of Dams (job-A5).

Wraps the U.S. Army Corps of Engineers (USACE) National Inventory of Dams
(NID) public ArcGIS REST FeatureService. Returns FlatGeobuf POINT geometries
of dam infrastructure together with the canonical NID attribute payload —
NIDID, name, owner type, dam type, primary purpose, dam height, year
completed, hazard potential classification, and assorted spillway / storage /
condition fields downstream tools (Pelicun damage assessment, flood-routing
workflows, levee/dam infrastructure overlays) consume.

Source resolution order (authoritative -> mirror -> honest typed error):

1. AUTHORITATIVE: the USACE NID ArcGIS REST server at
   ``geospatial.sec.usace.army.mil/server/rest/services/NID`` is the
   regulatory source-of-truth. The whole ``NID`` folder is token-gated:
   a request with no token returns the ArcGIS error envelope
   ``{"error":{"code":499,"message":"Token Required"}}``; a request with a
   bad/expired token returns ``{"error":{"code":498,"message":"Invalid
   Token"}}`` (both verified live 2026-06-27). The token is resolved via
   the canonical 3-path secret loader (kwarg -> per-Case ``secret_ref`` ->
   ``GRACE2_USACE_NID_TOKEN`` env), the SAME pattern eBird / ERA5 / GTSM
   use. When NO token resolves we DO NOT raise a missing-key error and
   strand the user -- we degrade to the public mirror (2). When a token
   resolves but the server REJECTS it (498), we raise
   ``USACEDAMSAuthError`` (a credential-shaped typed error the agent's
   generic credential-card pipeline surfaces) so the user can re-enter a
   valid token.

2. MIRROR (fallback): the publicly mirrored ESRI Living Atlas feature
   service the NID program ships for unauthenticated public consumption
   (verified 2026-06-09 / re-verified 2026-06-27):

       https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/
           NID_v1/FeatureServer/0/query

Layer 0 is the dam point inventory; ``geometryType=esriGeometryPoint``,
``maxRecordCount=2000``. The schema preserves both the dam-condition rollup
(``HAZARD_POTENTIAL``, ``CONDITION_ASSESSMENT``, ``EAP_PREPARED``) and the
physical-structure fields the SFINCS / dam-break / inundation engines need
(``DAM_HEIGHT``, ``NID_STORAGE``, ``MAX_DISCHARGE``, ``DRAINAGE_AREA``).

Server-side ``where``-clause filters (job-A5 upgrade): the previously inert
``hazard_potential`` and ``state`` params are now live -- they compose into
an ArcGIS ``where`` clause applied to BOTH the authoritative endpoint and
the mirror (so "show every high-hazard dam in Nevada" filters server-side
instead of pulling the whole inventory). 2026-07-07: ``min_height_ft``
joins them (``DAM_HEIGHT >= {value}``, feet; numeric comparison verified
live against the mirror).

Query parameters used:
    where=1=1
    geometry={bbox}            (omitted when bbox is None — CONUS sweep)
    geometryType=esriGeometryEnvelope
    inSR=4326
    outFields=<allow-list>
    outSR=4326
    f=geojson

Cache: ``static-30d`` (NID is a regulatory inventory; updates are quarterly
at fastest — a 30-day TTL matches FR-DC-2 static-state semantics).
``cacheable=True``; ``source_class="usace_nid_dams"``.

``supports_global_query=True`` (Wave 1.5 schema amendment): the bbox=None
semantics return the CONUS+AK+HI dam population (~91k features today),
which exceeds a single FeatureServer page. Pagination is implemented via
``resultOffset`` + ``resultRecordCount`` per the kickoff. The
catalog/discovery layer can route "show every dam in the US" queries here
without forcing a bbox parameter, although in practice the agent should
prefer narrowing by bbox or state to keep payload tractable — see
``estimate_payload_mb`` and the Wave-1.5 chat-warning gate, which fires
on the global sweep.

FR-DC-3/4: routed through ``read_through`` so identical bbox calls reuse
the cached FlatGeobuf. FR-AS-11: ``USACEDAMSError`` / sub-classes carry
``error_code`` + ``retryable`` for the agent's retry/clarify/fallback
surface. FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usace_dams",
    "USACEDAMSError",
    "USACEDAMSInputError",
    "USACEDAMSUpstreamError",
    "USACEDAMSEmptyError",
    "USACEDAMSAuthError",
    "estimate_payload_mb",
    "_build_nid_url",
    "_bbox_to_envelope",
    "_build_where_clause",
    "_validate_hazard_potential",
    "_validate_state",
    "_validate_min_height",
    "_resolve_nid_token",
    "set_persistence_for_secrets",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_fetch_nid_geojson_page",
    "_fetch_nid_all_features",
    "_geojson_to_fgb",
    "_fetch_nid_bytes",
    "CONUS_BBOX",
    "PRESERVED_PROPERTIES",
    "VALID_HAZARD_POTENTIALS",
]

logger = logging.getLogger("grace2_agent.tools.fetch_usace_dams")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class USACEDAMSError(RuntimeError):
    """Base class for fetch_usace_dams failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "USACE_DAMS_ERROR"
    retryable: bool = True


class USACEDAMSInputError(USACEDAMSError):
    """Caller passed an invalid bbox or unsupported parameter."""

    error_code = "USACE_DAMS_INPUT_INVALID"
    retryable = False


class USACEDAMSUpstreamError(USACEDAMSError):
    """USACE NID ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "USACE_DAMS_UPSTREAM_ERROR"
    retryable = True


class USACEDAMSEmptyError(USACEDAMSError):
    """NID returned an empty FeatureCollection — informational, not retryable.

    NOT raised by the tool body (we serialize an empty FGB instead — an empty
    bbox over open ocean / Antarctica is LEGITIMATE), but kept available for
    future strict-mode opt-in.
    """

    error_code = "USACE_DAMS_EMPTY"
    retryable = False


class USACEDAMSAuthError(USACEDAMSError):
    """The AUTHORITATIVE NID endpoint rejected the supplied token.

    Fired when a token resolved (kwarg / secret_ref / env) but the
    ``geospatial.sec.usace.army.mil`` ArcGIS server returned the ESRI
    ``code:498 Invalid Token`` envelope (or an HTTP 401/403) — the token is
    wrong, expired, or revoked. The ``_AUTH_ERROR`` error-code suffix and the
    ``USACEDAMSAuthError`` class name are both recognised by the agent's
    provider-agnostic credential pipeline
    (``credential_registry.is_credential_shaped_error``), so the server
    surfaces a NAME-ONLY credential card (NATE principle 3) prompting the user
    to re-enter a valid USACE NID token — no per-provider registry entry is
    required. ``retryable=False`` because retrying the same bad token is futile;
    the agent waits for a fresh token.

    NOTE: a MISSING token (no token resolves at all) does NOT raise this — the
    tool degrades to the public mirror so a key-less user still gets dam data.
    Only an explicitly-supplied-but-rejected token surfaces the card.
    """

    error_code = "USACE_DAMS_AUTH_ERROR"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Public ESRI Living Atlas mirror of the USACE NID feature service. The
# authoritative ``geospatial.sec.usace.army.mil`` REST endpoint requires a
# token, but the NID program publishes this unauthenticated mirror for public
# consumption. Verified 2026-06-09; re-verified 2026-06-27.
_NID_BASE = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "NID_v1/FeatureServer/0/query"
)

# AUTHORITATIVE USACE NID ArcGIS REST endpoint. The whole ``NID`` folder is
# token-gated (verified live 2026-06-27): no token -> ESRI ``code:499 Token
# Required``; bad token -> ``code:498 Invalid Token``. Used FIRST when a token
# resolves; otherwise the tool degrades to ``_NID_BASE`` (the public mirror).
# The exact published layer name under the NID folder is token-gated and so is
# not enumerable without a key; we target the conventional ``NID/MapServer/0``
# layer and detect a service-name miss (HTTP 404) as a fallback trigger, while
# the ``code:498``/``code:499`` envelopes and HTTP 401/403 are auth signals.
_NID_AUTHORITATIVE_BASE = (
    "https://geospatial.sec.usace.army.mil/server/rest/services/"
    "NID/NID/MapServer/0/query"
)

# Env-var fallback name for the authoritative-endpoint token. Same naming
# convention the credential pipeline uses for a generic provider scope
# (UPPER_SNAKE of the credential). Resolution order: kwarg -> secret_ref ->
# this env var.
_NID_TOKEN_ENV = "GRACE2_USACE_NID_TOKEN"

# ESRI ArcGIS token error codes. 499 = token required (none supplied),
# 498 = token invalid/expired. Both indicate the authoritative endpoint
# needs a (valid) credential.
_ESRI_TOKEN_REQUIRED_CODE = 499
_ESRI_TOKEN_INVALID_CODE = 498

# Canonical NID HAZARD_POTENTIAL classification values (USACE controlled
# vocabulary). Used to validate + normalize the ``hazard_potential`` filter.
VALID_HAZARD_POTENTIALS: dict[str, str] = {
    "high": "High",
    "significant": "Significant",
    "low": "Low",
    "undetermined": "Undetermined",
}

# Properties preserved from each NID feature. The kickoff named the canonical
# NID dam-identification + physical + regulatory fields; we keep an explicit
# allow-list so future NID schema growth doesn't quietly bloat the wire
# payload, and so the FlatGeobuf column set is stable across versions.
PRESERVED_PROPERTIES: tuple[str, ...] = (
    # Identification.
    "OBJECTID",
    "NIDID",
    "FEDERAL_ID",
    "NAME",
    "OTHER_NAMES",
    "STATE",
    "COUNTYSTATE",
    "CITY",
    "LATITUDE",
    "LONGITUDE",
    "RIVER_OR_STREAM",
    "CONGDIST",
    # Ownership / regulation.
    "OWNER_TYPES",
    "PRIMARY_OWNER_TYPE",
    "STATE_REGULATED",
    "STATE_JURISDICTION",
    "STATE_REGULATORY_AGENCY",
    "PRIMARY_SOURCE_AGENCY",
    # Physical / structural.
    "PRIMARY_PURPOSE",
    "PURPOSES",
    "PRIMARY_DAM_TYPE",
    "DAM_TYPES",
    "DAM_HEIGHT",
    "HYDRAULIC_HEIGHT",
    "STRUCTURAL_HEIGHT",
    "NID_HEIGHT",
    "DAM_LENGTH",
    "DAM_VOLUME",
    "YEAR_COMPLETED",
    # Reservoir / hydrology.
    "NID_STORAGE",
    "MAX_STORAGE",
    "NORMAL_STORAGE",
    "SURFACE_AREA",
    "DRAINAGE_AREA",
    "MAX_DISCHARGE",
    "SPILLWAY_TYPE",
    "SPILLWAY_WIDTH",
    # Hazard / inspection.
    "HAZARD_POTENTIAL",
    "CONDITION_ASSESSMENT",
    "CONDITION_ASSESS_DATE",
    "EAP_PREPARED",
    "EAP_LAST_REV_DATE",
    "LAST_INSPECTION_DATE",
    "INSPECTION_FREQUENCY",
    "OPERATIONAL_STATUS",
    "OPERATIONAL_STATUS_DATE",
    "DATA_UPDATED",
)

# User-Agent — ESRI tracks unauthenticated clients; identify this client clearly
# so the NID team can attribute traffic.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Request timeout. NID's ArcGIS cluster usually responds <2s for a state-sized
# bbox; 30s matches the envelope we give other ArcGIS REST fetchers.
_HTTP_TIMEOUT_S = 30.0

# Server-enforced max page size on the NID FeatureService.
_NID_PAGE_SIZE = 2000

# CONUS+AK+HI envelope used as default bbox when caller passes None. Generous
# on the AK/HI side; matches the envelope used by ``fetch_nifc_fire_perimeters``.
CONUS_BBOX: tuple[float, float, float, float] = (-180.0, 13.0, -65.0, 72.0)

# Hard cap on number of features paginated in one call. The NID is ~91k
# features nationwide; we cap at 50k to keep the FlatGeobuf payload + GCS
# write tractable. Callers wanting larger sweeps should narrow by bbox.
_MAX_TOTAL_FEATURES = 50_000


# ---------------------------------------------------------------------------
# Payload estimator hook (Wave 1.5 / FR-DC-9).
# ---------------------------------------------------------------------------

# Empirical sizing: each NID feature serializes to ~1 KB of FlatGeobuf
# (point geometry + ~30 scalar attributes). A typical county-sized bbox
# pulls 20-200 dams (~0.05-0.2 MB); a state-sized bbox pulls 500-5000 dams
# (~0.5-5 MB); the CONUS sweep pulls ~91k dams (~90 MB before pagination
# cap). The estimator returns a scale-aware upper bound.
_BYTES_PER_FEATURE_ESTIMATE = 1024

# CONUS area (sq deg) used to scale the estimator by bbox area.
_CONUS_AREA_DEG = (CONUS_BBOX[2] - CONUS_BBOX[0]) * (CONUS_BBOX[3] - CONUS_BBOX[1])
_CONUS_FEATURE_COUNT_ESTIMATE = 91_000


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator (called by chat-warning gate).

    Scales the estimate by bbox area relative to CONUS. A None / missing
    bbox returns the CONUS sweep estimate (~90 MB before pagination cap,
    reported as ~50 MB to reflect the ``_MAX_TOTAL_FEATURES`` cap).

    The signature accepts ``**args`` to match the Wave-1.5 estimator
    convention (the chat-warning gate passes the tool's kwargs unchanged).
    """
    bbox = args.get("bbox")
    if bbox is None:
        # CONUS sweep — pagination cap of 50k features × 1KB ≈ 50 MB.
        return float(_MAX_TOTAL_FEATURES * _BYTES_PER_FEATURE_ESTIMATE) / (1024 * 1024)
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        # Caller passed garbage; bail out with the CONUS-sweep estimate
        # rather than raising — the estimator is advisory only.
        return float(_MAX_TOTAL_FEATURES * _BYTES_PER_FEATURE_ESTIMATE) / (1024 * 1024)
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return float(_MAX_TOTAL_FEATURES * _BYTES_PER_FEATURE_ESTIMATE) / (1024 * 1024)
    area = max(0.0, (max_lon - min_lon)) * max(0.0, (max_lat - min_lat))
    if _CONUS_AREA_DEG <= 0:
        return 0.1
    fraction = min(1.0, area / _CONUS_AREA_DEG)
    est_features = max(1, int(_CONUS_FEATURE_COUNT_ESTIMATE * fraction))
    est_bytes = est_features * _BYTES_PER_FEATURE_ESTIMATE
    return float(est_bytes) / (1024 * 1024)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=True`` because the bbox=None semantics genuinely
# return the CONUS+AK+HI dam population. The Wave-1.5 chat-warning gate
# uses ``estimate_payload_mb`` to warn the user before a large sweep is
# committed.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_usace_dams",
    ttl_class="static-30d",
    source_class="usace_nid_dams",
    cacheable=True,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``USACEDAMSInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise USACEDAMSInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise USACEDAMSInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise USACEDAMSInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise USACEDAMSInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise USACEDAMSInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``geometryType=esriGeometryEnvelope`` string.

    ArcGIS REST envelope format is the literal ``xmin,ymin,xmax,ymax`` —
    no JSON wrapping when ``geometryType=esriGeometryEnvelope`` is set.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# Filter validation + WHERE-clause construction (job-A5 upgrade — activate
# the formerly-inert hazard_potential / state params).
# ---------------------------------------------------------------------------


def _sql_escape(value: str) -> str:
    """Escape a string literal for an ArcGIS SQL ``where`` clause.

    ArcGIS REST uses standard SQL single-quote literals; a single quote inside
    the value is escaped by doubling it. We ONLY ever interpolate validated /
    normalized tokens (see ``_validate_hazard_potential`` / ``_validate_state``)
    into the clause, so this is defense-in-depth, not the primary guard.
    """
    return value.replace("'", "''")


def _validate_hazard_potential(
    hazard_potential: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Normalize the ``hazard_potential`` filter to canonical NID values.

    Accepts a single value or a list/tuple of values, case-insensitive. Each
    must be one of the NID controlled vocabulary
    (``High`` / ``Significant`` / ``Low`` / ``Undetermined``). Returns the
    normalized canonical-case list (empty list if ``None``).

    Raises ``USACEDAMSInputError`` on an unknown classification.
    """
    if hazard_potential is None:
        return []
    if isinstance(hazard_potential, str):
        raw = [hazard_potential]
    elif isinstance(hazard_potential, (list, tuple)):
        raw = list(hazard_potential)
    else:
        raise USACEDAMSInputError(
            f"hazard_potential must be a str or list of str; got "
            f"{type(hazard_potential).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise USACEDAMSInputError(
                f"hazard_potential entries must be str; got {item!r}"
            )
        key = item.strip().lower()
        canon = VALID_HAZARD_POTENTIALS.get(key)
        if canon is None:
            raise USACEDAMSInputError(
                f"hazard_potential {item!r} is not a valid NID classification; "
                f"expected one of {sorted(set(VALID_HAZARD_POTENTIALS.values()))}"
            )
        if canon not in out:
            out.append(canon)
    return out


def _validate_state(
    state: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Normalize the ``state`` filter to NID ``STATE`` values (Title Case names).

    The NID ``STATE`` column stores full state NAMES in Title Case
    (e.g. ``"Nevada"``, ``"North Carolina"``), NOT two-letter abbreviations
    (verified live 2026-06-27). We accept a single value or a list/tuple, and
    Title-Case each (so ``"nevada"`` / ``"NEVADA"`` -> ``"Nevada"``). A
    two-letter abbreviation is expanded via a built-in USPS map so callers can
    pass either form. Returns the normalized list (empty list if ``None``).

    Raises ``USACEDAMSInputError`` on a non-string / empty entry.
    """
    if state is None:
        return []
    if isinstance(state, str):
        raw = [state]
    elif isinstance(state, (list, tuple)):
        raw = list(state)
    else:
        raise USACEDAMSInputError(
            f"state must be a str or list of str; got {type(state).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise USACEDAMSInputError(f"state entries must be str; got {item!r}")
        s = item.strip()
        if not s:
            raise USACEDAMSInputError("state entries must be non-empty")
        # Expand a 2-letter USPS abbreviation to the full NID name.
        if len(s) == 2 and s.upper() in _USPS_TO_NAME:
            canon = _USPS_TO_NAME[s.upper()]
        else:
            # Title-case full names; preserve interior structure (handles
            # "north carolina" -> "North Carolina", "district of columbia").
            canon = " ".join(w.capitalize() for w in s.split())
        if canon not in out:
            out.append(canon)
    return out


def _validate_min_height(
    min_height_ft: float | int | None,
) -> float | None:
    """Normalize the ``min_height_ft`` filter to a finite non-negative float.

    Accepts an int/float (or a numeric string, since LLM callers sometimes
    stringify numbers). Returns None when the filter is unset. Raises
    ``USACEDAMSInputError`` on a non-numeric, negative, NaN, or infinite
    value. Units are FEET - the NID ``DAM_HEIGHT`` column is reported in
    feet.
    """
    if min_height_ft is None:
        return None
    if isinstance(min_height_ft, bool):
        raise USACEDAMSInputError(
            f"min_height_ft must be a number (feet); got {min_height_ft!r}"
        )
    if isinstance(min_height_ft, str):
        try:
            min_height_ft = float(min_height_ft.strip())
        except ValueError:
            raise USACEDAMSInputError(
                f"min_height_ft must be a number (feet); got {min_height_ft!r}"
            ) from None
    if not isinstance(min_height_ft, (int, float)):
        raise USACEDAMSInputError(
            f"min_height_ft must be a number (feet); got "
            f"{type(min_height_ft).__name__}"
        )
    value = float(min_height_ft)
    if math.isnan(value) or math.isinf(value):
        raise USACEDAMSInputError(
            f"min_height_ft must be finite; got {min_height_ft!r}"
        )
    if value < 0:
        raise USACEDAMSInputError(
            f"min_height_ft must be >= 0; got {min_height_ft!r}"
        )
    return value


def _build_where_clause(
    hazard_potentials: list[str],
    states: list[str],
    min_height_ft: float | None = None,
) -> str:
    """Compose an ArcGIS ``where`` clause from the normalized filters.

    String filters are applied with ``IN (...)`` lists; the numeric
    ``min_height_ft`` filter as ``DAM_HEIGHT >= {value}`` (NID reports
    ``DAM_HEIGHT`` in feet; verified live 2026-07-07 that the mirror
    evaluates the numeric comparison server-side). All clauses are ANDed
    together. When no filter is set, returns the ``1=1`` tautology (the
    original behaviour). All interpolated string values are pre-validated +
    SQL-escaped; the numeric value is pre-validated to a finite float.

    Example::

        _build_where_clause(["High", "Significant"], ["Nevada"], 50.0)
        -> "HAZARD_POTENTIAL IN ('High','Significant') AND STATE IN ('Nevada')
            AND DAM_HEIGHT >= 50"
    """
    clauses: list[str] = []
    if hazard_potentials:
        ins = ",".join(f"'{_sql_escape(h)}'" for h in hazard_potentials)
        clauses.append(f"HAZARD_POTENTIAL IN ({ins})")
    if states:
        ins = ",".join(f"'{_sql_escape(s)}'" for s in states)
        clauses.append(f"STATE IN ({ins})")
    if min_height_ft is not None:
        # %g drops a trailing ".0" so cache keys / URLs stay stable for the
        # common integer case (50.0 -> "50").
        clauses.append(f"DAM_HEIGHT >= {min_height_ft:g}")
    if not clauses:
        return "1=1"
    return " AND ".join(clauses)


# USPS 2-letter -> NID full state name. Covers the 50 states + DC + the
# territories the NID inventories. Used so a caller may pass either form.
_USPS_TO_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District Of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
    "GU": "Guam", "VI": "Virgin Islands", "AS": "American Samoa",
}


# ---------------------------------------------------------------------------
# Authoritative-endpoint token resolution (canonical 3-path secret loader —
# mirrors fetch_ebird_observations / fetch_era5_reanalysis).
# ---------------------------------------------------------------------------

# Module-level Persistence binding. The agent service sets this at startup via
# ``set_persistence_for_secrets`` so this fetcher can resolve a ``secret_ref``
# without importing the MCP client. Tests inject a mock via the same setter.
_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for secret materialization.

    Called once at startup by the agent service (parallels
    ``fetch_ebird_observations.set_persistence_for_secrets``). Tests call this
    in a fixture and reset to ``None`` on teardown.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from sync context.

    Uses ``asyncio.run`` when no loop is running (test / CLI path); falls back
    to a one-shot worker-thread loop when called from within a running loop
    (agent-runtime path). Both paths close the loop they create. Mirrors the
    eBird fetcher's bridge so the secret-loader semantics are identical.
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            result_box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            error_box["err"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "err" in error_box:
        raise error_box["err"]
    return result_box["value"]


def _materialize_secret(secret_ref: Any) -> str:
    """Bridge ``Persistence.get_secret_value`` (async) into a sync caller.

    A ``str`` ``secret_ref`` is accepted verbatim (the test surface injects a
    known token this way without standing up Persistence). Otherwise the bound
    ``Persistence`` resolves the per-Case vault reference.
    """
    if isinstance(secret_ref, str):
        return secret_ref

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise USACEDAMSAuthError(
            "Persistence not bound; cannot resolve secret_ref for the USACE "
            "NID authoritative endpoint. Pass token=... explicitly in this "
            "context, or rely on the public mirror."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


def _resolve_nid_token(
    token: str | None,
    secret_ref: Any | None,
) -> str | None:
    """Resolve the authoritative-endpoint token, or ``None`` if none is set.

    Priority (canonical 3-path secret loader):

    1. Explicit ``token`` kwarg (live-test / dev override).
    2. ``secret_ref`` (a ``SecretRecord``) -> ``Persistence.get_secret_value``
       (the per-Case production path).
    3. ``GRACE2_USACE_NID_TOKEN`` env var (dev convenience).

    UNLIKE the eBird / ERA5 fetchers (which RAISE a missing-key error when no
    key resolves), this returns ``None`` when no token is found -- the caller
    then degrades to the public mirror so a key-less user still gets dam data.
    A token that resolves but is REJECTED by the server surfaces
    ``USACEDAMSAuthError`` downstream (the credential-card path).
    """
    if token:
        return token
    if secret_ref is not None:
        try:
            resolved = _materialize_secret(secret_ref)
        except USACEDAMSAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as auth error
            raise USACEDAMSAuthError(
                f"USACE NID secret_ref lookup failed: {exc}"
            ) from exc
        if resolved:
            return resolved
    env_token = os.environ.get(_NID_TOKEN_ENV)
    if env_token:
        return env_token
    return None


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_nid_url(
    bbox: tuple[float, float, float, float] | None,
    *,
    where: str = "1=1",
    base_url: str = _NID_BASE,
    token: str | None = None,
    result_offset: int = 0,
    result_record_count: int = _NID_PAGE_SIZE,
) -> tuple[str, dict[str, str]]:
    """Build the NID FeatureServer query URL + params dict.

    When ``bbox`` is None, the query omits the geometry filter and returns the
    full CONUS+AK+HI dam population (paginated).  When a bbox is given, it is
    converted to ``esriGeometryEnvelope`` + ``inSR=4326`` server-side spatial
    filter.

    ``where`` carries the hazard/state ``IN (...)`` filter clause (job-A5
    upgrade); ``"1=1"`` is the unfiltered tautology. ``base_url`` selects the
    authoritative endpoint (``_NID_AUTHORITATIVE_BASE``) vs the public mirror
    (``_NID_BASE``). ``token``, when set, is appended for the authoritative
    ArcGIS token gate.

    ``result_offset`` + ``result_record_count`` drive the pagination loop in
    ``_fetch_nid_all_features``. The NID FeatureServer enforces
    ``maxRecordCount=2000``; requesting more is silently truncated.
    """
    out_fields = ",".join(PRESERVED_PROPERTIES)
    params: dict[str, str] = {
        "where": where or "1=1",
        "outFields": out_fields,
        "outSR": "4326",
        "f": "geojson",
        "resultOffset": str(result_offset),
        "resultRecordCount": str(min(result_record_count, _NID_PAGE_SIZE)),
        # orderByFields gives the pagination a stable cursor — without it,
        # ArcGIS occasionally drops rows across page boundaries.
        "orderByFields": "OBJECTID ASC",
    }
    if bbox is not None:
        params["geometry"] = _bbox_to_envelope(bbox)
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
        params["inSR"] = "4326"
    if token:
        params["token"] = token
    return base_url, params


# ---------------------------------------------------------------------------
# NID HTTP fetch — single page.
# ---------------------------------------------------------------------------


def _fetch_nid_geojson_page(
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """GET one page of the NID FeatureServer query and return parsed GeoJSON.

    Raises:
        ``USACEDAMSUpstreamError``: network / 5xx / non-JSON / error-envelope /
        non-FeatureCollection response.
    """
    logger.info(
        "fetch_usace_dams: GET %s offset=%s count=%s",
        url,
        params.get("resultOffset"),
        params.get("resultRecordCount"),
    )
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.HTTPError as exc:
        raise USACEDAMSUpstreamError(
            f"USACE NID request failed url={url}: {exc}"
        ) from exc

    # HTTP 401/403 from the authoritative endpoint => credential signal.
    if resp.status_code in (401, 403):
        raise USACEDAMSAuthError(
            f"USACE NID authoritative endpoint rejected the token "
            f"(HTTP {resp.status_code}) url={url}: {resp.text[:300]!r}"
        )

    if resp.status_code >= 400:
        raise USACEDAMSUpstreamError(
            f"USACE NID returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise USACEDAMSUpstreamError(
            f"USACE NID returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise USACEDAMSUpstreamError(
            f"USACE NID response is not a JSON object url={url}: "
            f"type={type(body).__name__!r}"
        )

    if "error" in body:
        err = body["error"]
        # ESRI token gate: code 499 (token required) / 498 (invalid token).
        # The authoritative ``geospatial.sec.usace.army.mil`` folder is
        # token-gated; either code is a credential signal the agent surfaces
        # as a credential card (via the generic credential pipeline).
        err_code = err.get("code") if isinstance(err, dict) else None
        if err_code in (_ESRI_TOKEN_REQUIRED_CODE, _ESRI_TOKEN_INVALID_CODE):
            raise USACEDAMSAuthError(
                f"USACE NID authoritative endpoint requires a valid token "
                f"(ESRI code {err_code}) url={url}: {err}"
            )
        raise USACEDAMSUpstreamError(
            f"USACE NID query returned error envelope url={url}: {err}"
        )

    if body.get("type") != "FeatureCollection":
        raise USACEDAMSUpstreamError(
            f"USACE NID response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type')!r}"
        )

    return body


# ---------------------------------------------------------------------------
# Pagination loop.
# ---------------------------------------------------------------------------


def _fetch_nid_all_features(
    bbox: tuple[float, float, float, float] | None,
    *,
    where: str = "1=1",
    base_url: str = _NID_BASE,
    token: str | None = None,
    max_features: int = _MAX_TOTAL_FEATURES,
) -> dict[str, Any]:
    """Page through the NID FeatureService, accumulating up to ``max_features``.

    Returns a single GeoJSON FeatureCollection assembled from all pages.
    Stops when a page returns fewer features than the page size, when
    ``max_features`` is reached, or when the cumulative feature count
    exceeds the cap.

    ``where`` carries the hazard/state filter clause; ``base_url`` + ``token``
    select the authoritative endpoint vs the mirror.

    Raises ``USACEDAMSUpstreamError`` if any page errors, or
    ``USACEDAMSAuthError`` if the authoritative endpoint rejects the token.
    """
    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, params = _build_nid_url(
            bbox,
            where=where,
            base_url=base_url,
            token=token,
            result_offset=offset,
            result_record_count=_NID_PAGE_SIZE,
        )
        page = _fetch_nid_geojson_page(url, params)
        page_features = page.get("features") or []
        accumulated.extend(page_features)
        logger.debug(
            "fetch_usace_dams: page offset=%d returned %d features (total %d)",
            offset,
            len(page_features),
            len(accumulated),
        )
        if len(page_features) < _NID_PAGE_SIZE:
            # Last page (server returned a short page).
            break
        if len(accumulated) >= max_features:
            logger.warning(
                "fetch_usace_dams: hit max_features=%d cap; truncating sweep",
                max_features,
            )
            accumulated = accumulated[:max_features]
            break
        offset += _NID_PAGE_SIZE
    return {"type": "FeatureCollection", "features": accumulated}


# ---------------------------------------------------------------------------
# GeoJSON -> FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _geojson_to_fgb(geojson: dict[str, Any]) -> bytes:
    """Convert a NID GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves ``PRESERVED_PROPERTIES``. Features without a point geometry
    are dropped (NID is by definition a point inventory; null-geom rows
    are junk for this layer). Always emits a valid FlatGeobuf — an empty
    input yields a header-only FGB.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise USACEDAMSUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    features = geojson.get("features", []) or []

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        props = feat.get("properties") or {}
        row_props: dict[str, Any] = {}
        for key in PRESERVED_PROPERTIES:
            v = props.get(key)
            # Coerce non-scalar values to JSON strings — FlatGeobuf needs
            # scalar column types per field.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row_props[key] = v
        cleaned.append({
            "type": "Feature",
            "properties": row_props,
            "geometry": geom,
        })

    if not cleaned:
        gdf = gpd.GeoDataFrame(
            {k: [] for k in PRESERVED_PROPERTIES},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_usace_dams_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise USACEDAMSUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_usace_dams: FlatGeobuf = %d bytes (%d feature(s))",
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


# ---------------------------------------------------------------------------
# End-to-end fetcher (pagination → GeoJSON → FGB bytes).
# ---------------------------------------------------------------------------


def _fetch_nid_bytes(
    bbox: tuple[float, float, float, float] | None,
    *,
    where: str = "1=1",
    token: str | None = None,
) -> bytes:
    """Run pagination + conversion with authoritative -> mirror -> error logic.

    Source resolution order:

    1. AUTHORITATIVE (``geospatial.sec.usace.army.mil``): attempted ONLY when a
       ``token`` resolved. On a token-rejection (498 / 401 / 403) we raise
       ``USACEDAMSAuthError`` immediately -- the supplied token is bad and the
       agent surfaces a credential card rather than silently masking it with
       mirror data. On a NON-auth authoritative failure (service-name 404,
       network, 5xx) we log + fall through to the mirror.
    2. MIRROR (public ESRI Living Atlas): the fallback (and the primary path
       when no token resolved). On mirror failure we raise the mirror's typed
       error -- an honest dead-end, never a fabricated success.

    The hazard/state ``where`` clause is applied to whichever endpoint serves.
    """
    # 1. Authoritative endpoint — only when a token is present.
    if token:
        try:
            geojson = _fetch_nid_all_features(
                bbox,
                where=where,
                base_url=_NID_AUTHORITATIVE_BASE,
                token=token,
            )
            logger.info(
                "fetch_usace_dams: served from AUTHORITATIVE NID endpoint "
                "(%d feature(s))",
                len(geojson.get("features") or []),
            )
            return _geojson_to_fgb(geojson)
        except USACEDAMSAuthError:
            # A resolved-but-rejected token is a credential signal — do NOT
            # mask it with mirror data; surface the card so the user fixes it.
            raise
        except USACEDAMSError as exc:
            # Non-auth authoritative failure (wrong service path / network /
            # 5xx) — degrade to the mirror honestly.
            logger.warning(
                "fetch_usace_dams: authoritative endpoint failed (%s); "
                "falling back to public mirror",
                exc,
            )

    # 2. Public mirror (fallback, or primary when no token).
    geojson = _fetch_nid_all_features(
        bbox,
        where=where,
        base_url=_NID_BASE,
        token=None,
    )
    logger.info(
        "fetch_usace_dams: served from public MIRROR (%d feature(s))",
        len(geojson.get("features") or []),
    )
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
def fetch_usace_dams(
    bbox: tuple[float, float, float, float] | None = None,
    hazard_potential: str | list[str] | None = None,
    state: str | list[str] | None = None,
    min_height_ft: float | int | None = None,
    token: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """USACE National Inventory of Dams (NID) as a FlatGeobuf point layer.

    What it does:
        Fetches U.S. Army Corps of Engineers National Inventory of Dams
        records as point features with full NID attribute payload —
        identification, ownership, physical structure (height, length,
        storage, drainage area), hazard potential classification,
        condition assessment, and emergency-action-plan status.
        Resolves AUTHORITATIVE-first: when a USACE NID token is available
        (kwarg / per-Case ``secret_ref`` / ``GRACE2_USACE_NID_TOKEN`` env) it
        queries the regulatory source-of-truth at
        ``geospatial.sec.usace.army.mil``; otherwise (and on any non-auth
        authoritative failure) it degrades to the public ESRI Living Atlas
        mirror of the NID FeatureService. A supplied-but-rejected token
        raises a credential-shaped error so the agent surfaces a credential
        card to re-enter a valid token.

    When to use:
        - User asks about dams in a region ("what dams are upstream of X?",
          "show me every high-hazard dam in California").
        - Flood-modeling workflow needs upstream dam locations / spillway
          capacity / storage volume to gauge dam-break or controlled-release
          scenarios.
        - Damage / risk assessment needs to overlay critical infrastructure
          (high-hazard-potential dams) on hazard footprints.
        - Pelicun building / asset analysis needs dam infrastructure context.

    When NOT to use:
        - DO NOT use for levees — use a future ``fetch_usace_nld_levees``
          tool (National Levee Database is a sibling but separate inventory).
        - DO NOT use for building structures — use ``fetch_usace_nsi``
          (National Structure Inventory) for Pelicun assets.
        - DO NOT use for downstream hydrologic routing — query NHD via
          ``fetch_river_geometry`` or NWM streamflow forecasts separately.
        - DO NOT use for non-US dams — NID is US-only.
        - DO NOT use for live reservoir operations data — NID is a static
          inventory; CWMS / USGS NWIS handle real-time reservoir levels.

    Parameters:
        bbox: Optional ``(min_lon, min_lat, max_lon, max_lat)`` envelope in
            EPSG:4326. Type: 4-float tuple, lon/lat ordered min-then-max
            on each axis. Example: ``(-82.5, 26.0, -81.0, 27.0)`` for the
            Fort Myers / Cape Coral area returns ~10-20 dam features.
            When None, the tool sweeps the full CONUS+AK+HI dam population
            (capped at 50k features); the Wave-1.5 chat-warning gate uses
            ``estimate_payload_mb`` to warn the user before a global sweep
            commits.
        hazard_potential: Optional NID hazard-potential filter. A single value
            or a list, case-insensitive, each one of ``"High"`` /
            ``"Significant"`` / ``"Low"`` / ``"Undetermined"``. Applied
            server-side as ``HAZARD_POTENTIAL IN (...)`` so "show every
            high-hazard dam in X" pulls only matching dams. Example:
            ``hazard_potential="High"`` or
            ``hazard_potential=["High", "Significant"]``.
        state: Optional NID state filter. A single value or list; full state
            NAMES (``"Nevada"``, ``"North Carolina"``) or 2-letter USPS
            abbreviations (``"NV"``) are both accepted and normalized to the
            NID ``STATE`` column form. Applied server-side as
            ``STATE IN (...)``.
        min_height_ft: Optional minimum dam height in FEET (the NID
            ``DAM_HEIGHT`` unit). A single non-negative number; applied
            server-side as ``DAM_HEIGHT >= {value}`` so "show dams taller
            than 100 ft" pulls only matching dams. Example:
            ``min_height_ft=100``. Composable with ``hazard_potential`` /
            ``state`` (clauses are ANDed).
        token: Optional explicit USACE NID authoritative-endpoint token
            (highest-priority resolution path). When set, the authoritative
            ``geospatial.sec.usace.army.mil`` endpoint is queried first.
        secret_ref: Optional ``SecretRecord`` (from the per-Case secrets
            panel) -> resolved to the token via ``Persistence.get_secret_value``
            at invocation time (the production path). A token that resolves
            but is rejected by the server raises ``USACEDAMSAuthError`` (the
            credential-card path); a token that does NOT resolve degrades to
            the public mirror.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
        ``s3://trid3nt-cache/cache/static-30d/usace_nid_dams/<key>.fgb``
        containing point geometries (``Point`` in EPSG:4326) and the
        canonical NID attribute schema — ``NIDID``, ``NAME``, ``STATE``,
        ``DAM_HEIGHT``, ``NID_STORAGE``, ``HAZARD_POTENTIAL``,
        ``CONDITION_ASSESSMENT``, ``EAP_PREPARED``, ``YEAR_COMPLETED``,
        ``PRIMARY_DAM_TYPE``, ``PRIMARY_PURPOSE``, etc. Downstream tools
        consume ``NIDID`` (join key), ``HAZARD_POTENTIAL`` (filter), and
        ``NID_STORAGE`` / ``DAM_HEIGHT`` (sizing). ``layer_type="vector"``,
        ``role="primary"``, ``units=None``.

    Cross-tool dependencies:
        Consumes optional bbox from ``fetch_administrative_boundaries`` /
        ``geocode_location`` (typical agent workflow: geocode "Lake Mead" →
        derive bbox → call this tool). Feeds into ``clip_vector_to_polygon``
        (clip dams to watershed / county / Case AOI), and into
        ``compute_zonal_statistics`` / Pelicun composers that pair dam
        location with hazard footprints from ``run_model_flood_scenario``.

    Cache: ``static-30d`` (NID is updated quarterly at fastest; a 30-day
    bucket gives ~12x amortization). Cache key: SHA-256 of bbox-rounded-6dp
    or "global" sentinel.

    External-API resilience (NFR-R-1): The ESRI Living Atlas cluster
    occasionally returns 5xx during ESRI maintenance windows. On network
    failure / non-2xx / malformed JSON / ArcGIS error envelope the tool
    raises ``USACEDAMSUpstreamError(retryable=True)`` so the agent's
    FR-AS-11 surface decides whether to retry, clarify, or fall back.

    Source-tier: FR-HEP-2 Tier 1 (USACE is the regulatory authority for
    the NID). Claims derived from this tool should be marked
    ``source_authority_tier=1`` in any ``ClaimSet`` aggregation.
    """
    # bbox quantization for cache-key stability + pre-flight validation.
    q_bbox: tuple[float, float, float, float] | None
    if bbox is None:
        q_bbox = None
    else:
        _validate_bbox(bbox)
        q_bbox = _round_bbox_to_6dp(bbox)

    # Filter validation + WHERE-clause construction (job-A5 upgrade).
    hazard_norm = _validate_hazard_potential(hazard_potential)
    state_norm = _validate_state(state)
    min_height_norm = _validate_min_height(min_height_ft)
    where = _build_where_clause(hazard_norm, state_norm, min_height_norm)

    # Authoritative-endpoint token resolution (None => mirror-only path).
    resolved_token = _resolve_nid_token(token=token, secret_ref=secret_ref)

    # Cache-key params. The token is INTENTIONALLY excluded from the key —
    # the underlying dam inventory does not vary by caller/token (the
    # authoritative + mirror return the same regulatory records); per-token
    # keying would needlessly fragment the cache. The filter clause IS part
    # of the key (different filters yield different result sets).
    params: dict[str, Any] = {
        "bbox": list(q_bbox) if q_bbox is not None else None,
        "hazard_potential": hazard_norm or None,
        "state": state_norm or None,
    }
    # Only key on min_height_ft when SET, so pre-existing cache entries for
    # unfiltered calls keep their original keys (no mass invalidation).
    if min_height_norm is not None:
        params["min_height_ft"] = min_height_norm

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nid_bytes(
            q_bbox, where=where, token=resolved_token
        ),
    )
    assert result.uri is not None, (
        "fetch_usace_dams is cacheable; uri must be set by read_through"
    )

    # Filter suffix so distinct filtered layers get distinct ids/names.
    filt_bits: list[str] = []
    if hazard_norm:
        filt_bits.append("-".join(h.lower() for h in hazard_norm))
    if state_norm:
        filt_bits.append("-".join(s.lower().replace(" ", "") for s in state_norm))
    if min_height_norm is not None:
        filt_bits.append(f"ge{min_height_norm:g}ft")
    filt_id = ("-" + "-".join(filt_bits)) if filt_bits else ""
    filt_name = (
        " [" + "; ".join(
            ([", ".join(hazard_norm) + " hazard"] if hazard_norm else [])
            + ([", ".join(state_norm)] if state_norm else [])
            + (
                [f">= {min_height_norm:g} ft"]
                if min_height_norm is not None
                else []
            )
        ) + "]"
    ) if filt_bits else ""

    if q_bbox is None:
        name = "USACE National Inventory of Dams — CONUS+AK+HI" + filt_name
        layer_id = "usace-nid-dams-global" + filt_id
    else:
        name = (
            f"USACE National Inventory of Dams — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
            + filt_name
        )
        layer_id = (
            f"usace-nid-dams-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}" + filt_id
        )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="usace_nid_dams",
        role="primary",
        units=None,
    )
