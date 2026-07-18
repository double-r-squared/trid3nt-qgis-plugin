"""``fetch_openfema_disasters`` atomic tool — FEMA disaster declarations as a
county-level FlatGeobuf (historical hazard-declaration context).

Fetches **real** US federal disaster declarations (Major Disaster ``DR``,
Emergency ``EM``, Fire-Management ``FM``) from the FEMA OpenFEMA API
(``www.fema.gov/api/open/v2/DisasterDeclarationsSummaries``), aggregates the
per-declaration / per-county rows up to ONE record per affected county, and
joins each county's aggregate to its TIGERweb county polygon (by 5-digit county
FIPS). The result is a county-polygon FlatGeobuf overlay carrying the
declaration count, the distinct incident types, the disaster numbers, the
declaration types, the latest declaration date, and the IA/PA program flags.

This is the canonical **historical hazard-declaration context** source: "which
counties in Florida have had a federally-declared disaster", "show me the
hurricane declarations in Texas since 2017", "where have flood disasters been
declared near here". It is NOT a live-hazard feed (that is the GOES / FIRMS /
NWS-warning family) and NOT a modeled-hazard layer (SFINCS / MODFLOW) — it is
the record of where FEMA has formally declared a disaster.

**API surface** (OpenFEMA, free, NO API key required):

    PRIMARY — Disaster Declarations Summaries (one row per disaster x county):
        https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries
            ?$filter=state eq 'FL' and fyDeclared ge 2017
            &$orderby=declarationDate desc
            &$top=1000&$skip=0&$format=json
    The OData ``$filter`` selects by ``state`` (2-letter USPS) plus an optional
    ``incidentType`` and an optional ``fyDeclared`` (federal fiscal year) lower
    bound. ``$top`` caps a page at 1000 rows; we page with ``$skip`` until a
    short page is returned (the full record set can exceed 1000 for a long
    state history). The body is ``{"DisasterDeclarationsSummaries": [...]}``.

    COUNTY GEOMETRY — Census TIGERweb State_County FeatureServer (layer 1):
        https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/
            State_County/MapServer/1/query
            ?where=STATE='12'&outFields=GEOID,NAME,STATE,COUNTY
            &returnGeometry=true&outSR=4326&f=geojson
    Returns county polygons (EPSG:4326) keyed by ``GEOID`` = the 5-digit county
    FIPS (2-digit state + 3-digit county). We fetch ALL counties for each state
    in scope ONCE and join the OpenFEMA aggregates in by FIPS.

**Per-declaration -> per-county aggregation**: each OpenFEMA row carries
``fipsStateCode`` (2-digit) + ``fipsCountyCode`` (3-digit). We build the 5-digit
county FIPS and group:

    - ``n_declarations``      — count of declarations touching the county
    - ``disaster_numbers``    — distinct disaster numbers (comma-joined)
    - ``incident_types``      — distinct incident types (Hurricane, Flood, ...)
    - ``declaration_types``   — distinct DR/EM/FM codes
    - ``latest_declaration``  — most-recent ``declarationDate`` (ISO-Z)
    - ``ia_program`` / ``pa_program`` — any row had Individual / Public
      Assistance declared

Rows whose ``fipsCountyCode`` is ``"000"`` (statewide / non-county-specific
designations, e.g. tribal / management) cannot be joined to a county polygon and
are excluded from the county overlay (counted separately for the honest-empty
gate). The county-keyed rows are the overlay.

**Spatial selector** (pass EXACTLY ONE):
    - ``state_code`` (2-letter USPS, e.g. ``"FL"``) — PREFERRED for state-level
      asks; one OpenFEMA paged query + one TIGERweb county fetch for that state.
    - ``bbox`` (west, south, east, north, EPSG:4326) — derives the intersecting
      states (via a state-envelope table), queries OpenFEMA per state, joins all
      counties, then CLIPS the county overlay to the bbox (counties whose
      polygon intersects the bbox are kept). A small metro bbox yields a handful
      of counties; a multi-state bbox fans out across each state.

**Fallback norm** (primary -> honest typed error): OpenFEMA is the sole
declaration source (there is no second declaration feed). If OpenFEMA returns
zero county-keyed declarations in scope, or TIGERweb returns no county geometry
for the joined FIPS, we raise a typed ``OpenFemaNoDeclarationsError`` /
``OpenFemaUpstreamError`` — never an empty success-shaped layer.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
county-polygon FeatureCollection serialized as FlatGeobuf, rendered via the
inline-vector path. ``style_preset="fema_disaster_declarations"``;
``LayerURI.bbox`` is the joined counties' extent so the camera auto-zooms.

Tier-1, no auth, ``supports_global_query=False`` (US states + territories only).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_openfema_disasters",
    "estimate_payload_mb",
    "OpenFemaError",
    "OpenFemaInputError",
    "OpenFemaUpstreamError",
    "OpenFemaNoDeclarationsError",
    "_validate_bbox",
    "_validate_state_code",
    "_resolve_states",
    "_build_openfema_filter",
    "_parse_declarations",
    "_aggregate_by_county",
    "_build_flatgeobuf",
    "_fetch_openfema_disasters_bytes",
    "OPENFEMA_URL",
    "TIGER_COUNTY_URL",
    "STATE_FIPS",
    "VALID_INCIDENT_TYPES",
]

logger = logging.getLogger("grace2_agent.tools.fetch_openfema_disasters")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class OpenFemaError(RuntimeError):
    """Base class for fetch_openfema_disasters failures."""

    error_code: str = "OPENFEMA_ERROR"
    retryable: bool = True


class OpenFemaInputError(OpenFemaError):
    """Invalid inputs — bad bbox shape, bad state code, no spatial selector,
    bad incident type, bad date/year window. Not retryable as-is."""

    error_code = "OPENFEMA_INPUT_ERROR"
    retryable = False


class OpenFemaUpstreamError(OpenFemaError):
    """An upstream request failed (OpenFEMA or TIGERweb network / HTTP 5xx /
    bad body). Retryable — transient outages recover on retry."""

    error_code = "OPENFEMA_UPSTREAM_ERROR"
    retryable = True


class OpenFemaNoDeclarationsError(OpenFemaError):
    """No federally-declared disasters found for the requested scope (or none
    that join to a county polygon). Not retryable — widen the scope, drop the
    incident-type filter, or extend the year window."""

    error_code = "OPENFEMA_NO_DECLARATIONS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: OpenFEMA Disaster Declarations Summaries endpoint (v2, keyless).
OPENFEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"

#: Census TIGERweb State_County FeatureServer — layer 1 = Counties (current
#: vintage), returns GEOID (5-digit county FIPS) + NAME + STATE + COUNTY.
TIGER_COUNTY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/1/query"
)

#: User-Agent (a descriptive UA is courteous to the federal endpoints).
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: HTTP timeout (seconds). County-geometry fetches for a large state can be slow.
_HTTP_TIMEOUT = 90.0

#: OpenFEMA page size. The service pages large result sets; we $skip until a
#: short page. 1000 is the conventional OpenFEMA page cap.
_PAGE_SIZE = 1000

#: Safety cap on total declaration rows pulled (avoids a runaway page loop on a
#: pathological filter). ~10 pages covers the longest single-state history.
_MAX_ROWS = 12000

#: 2-letter USPS state/territory code -> 2-digit FIPS state code. Mirrors the
#: ``fipsStateCode`` field OpenFEMA returns, so the OpenFEMA aggregate and the
#: TIGERweb ``STATE`` filter share a key. Covers 50 states + DC + 5 territories.
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72", "VI": "78", "GU": "66",
    "AS": "60", "MP": "69",
}

#: State FIPS -> approximate WGS84 envelope, for the bbox -> intersecting-states
#: derivation. Same source as fetch_administrative_boundaries' table (50 states
#: + DC + PR + VI); generous ~10km buffers near borders.
_STATE_FIPS_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "01": (-88.5, 30.1, -84.9, 35.0), "02": (-180.0, 51.2, -130.0, 71.5),
    "04": (-114.8, 31.3, -109.0, 37.0), "05": (-94.6, 33.0, -89.7, 36.5),
    "06": (-124.5, 32.5, -114.1, 42.0), "08": (-109.1, 36.9, -102.0, 41.0),
    "09": (-73.7, 40.9, -71.8, 42.1), "10": (-75.8, 38.4, -75.0, 39.8),
    "11": (-77.1, 38.8, -76.9, 39.0), "12": (-87.6, 24.4, -80.0, 31.0),
    "13": (-85.6, 30.3, -80.8, 35.0), "15": (-160.3, 18.9, -154.8, 22.2),
    "16": (-117.2, 42.0, -111.0, 49.0), "17": (-91.5, 36.9, -87.0, 42.5),
    "18": (-88.1, 37.8, -84.8, 41.8), "19": (-96.6, 40.4, -90.1, 43.5),
    "20": (-102.1, 36.9, -94.6, 40.0), "21": (-89.6, 36.5, -82.0, 39.1),
    "22": (-94.0, 28.9, -89.0, 33.0), "23": (-71.1, 43.0, -67.0, 47.5),
    "24": (-79.5, 37.9, -75.0, 39.7), "25": (-73.5, 41.2, -69.9, 42.9),
    "26": (-90.4, 41.7, -82.4, 48.3), "27": (-97.2, 43.5, -89.5, 49.4),
    "28": (-91.7, 30.1, -88.1, 35.0), "29": (-95.8, 35.9, -89.1, 40.6),
    "30": (-116.1, 44.4, -104.0, 49.0), "31": (-104.1, 40.0, -95.3, 43.0),
    "32": (-120.0, 35.0, -114.0, 42.0), "33": (-72.6, 42.7, -70.6, 45.3),
    "34": (-75.6, 38.9, -73.9, 41.4), "35": (-109.1, 31.3, -103.0, 37.0),
    "36": (-79.8, 40.5, -71.9, 45.0), "37": (-84.4, 33.8, -75.4, 36.6),
    "38": (-104.1, 45.9, -96.6, 49.0), "39": (-84.8, 38.4, -80.5, 42.3),
    "40": (-103.0, 33.6, -94.4, 37.0), "41": (-124.6, 41.9, -116.5, 46.3),
    "42": (-80.5, 39.7, -74.7, 42.3), "44": (-71.9, 41.1, -71.1, 42.0),
    "45": (-83.4, 32.0, -78.5, 35.2), "46": (-104.1, 42.5, -96.4, 45.9),
    "47": (-90.3, 35.0, -81.7, 36.7), "48": (-106.7, 25.8, -93.5, 36.5),
    "49": (-114.1, 37.0, -109.0, 42.0), "50": (-73.4, 42.7, -71.5, 45.0),
    "51": (-83.7, 36.5, -75.2, 39.5), "53": (-124.8, 45.5, -116.9, 49.0),
    "54": (-82.6, 37.2, -77.7, 40.6), "55": (-92.9, 42.5, -86.8, 47.1),
    "56": (-111.1, 40.9, -104.1, 45.0), "72": (-67.3, 17.9, -65.2, 18.6),
    "78": (-65.1, 17.6, -64.5, 18.5),
}

#: Reverse: FIPS -> USPS, so a bbox-derived FIPS becomes a state_code for the
#: OpenFEMA ``state`` filter.
_FIPS_TO_STATE: dict[str, str] = {v: k for k, v in STATE_FIPS.items()}

#: OpenFEMA ``incidentType`` enumeration (the documented value set). Used to
#: validate the optional filter so a typo becomes a typed input error instead of
#: a silently-empty query.
VALID_INCIDENT_TYPES: frozenset[str] = frozenset(
    {
        "Hurricane", "Flood", "Severe Storm", "Tornado", "Fire", "Snowstorm",
        "Severe Ice Storm", "Coastal Storm", "Tropical Storm", "Earthquake",
        "Drought", "Mud/Landslide", "Typhoon", "Dam/Levee Break", "Tsunami",
        "Volcanic Eruption", "Freezing", "Winter Storm", "Biological",
        "Chemical", "Fishing Losses", "Human Cause", "Other", "Toxic Substances",
        "Terrorist", "Straight-Line Winds", "Earthquake And Aftershocks",
    }
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common: dict[str, Any] = dict(
        name="fetch_openfema_disasters",
        # Declarations are historical and change slowly (a state gains a new
        # declaration occasionally); a 7-day TTL is the right freshness/cost
        # balance and matches the admin-boundary cadence the join depends on.
        ttl_class="semi-static-7d",
        source_class="openfema_disasters",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata lacks supports_global_query; registering "
            "fetch_openfema_disasters without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator.
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    state_code: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    The overlay is one county polygon per affected county. County polygons from
    TIGERweb at full resolution are ~50 KB each. A whole state averages ~50-100
    counties (Texas has 254; small states a dozen), so a state overlay is
    ~2.5-5 MB; a metro bbox a few counties (~150-300 KB).
    """
    if state_code is not None:
        n_counties = 80  # state-level average
    elif bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # ~4 counties per 1 deg^2 in the populated CONUS.
            n_counties = max(1, int(sq_deg * 4))
        except (TypeError, ValueError):
            n_counties = 20
    else:
        n_counties = 80
    return max(0.01, n_counties * 50_000 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation + selector resolution.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Validate + coerce the bbox to a finite, ordered 4-tuple in lon/lat range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise OpenFemaInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise OpenFemaInputError(f"bbox values must be numeric: {bbox!r}") from exc
    vals = (west, south, east, north)
    if not all(math.isfinite(v) for v in vals):
        raise OpenFemaInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise OpenFemaInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise OpenFemaInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise OpenFemaInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    return (west, south, east, north)


def _validate_state_code(state_code: str) -> str:
    """Normalize + validate a 2-letter USPS state/territory code."""
    if not isinstance(state_code, str):
        raise OpenFemaInputError(
            f"state_code must be a 2-letter string; got {type(state_code).__name__}"
        )
    sc = state_code.strip().upper()
    if sc not in STATE_FIPS:
        raise OpenFemaInputError(
            f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
            f"expected e.g. 'FL', 'TX', 'CA'"
        )
    return sc


def _validate_incident_type(incident_type: str) -> str:
    """Normalize an OpenFEMA incidentType to its canonical casing, or raise."""
    if not isinstance(incident_type, str) or not incident_type.strip():
        raise OpenFemaInputError("incident_type must be a non-empty string")
    want = incident_type.strip().lower()
    for canon in VALID_INCIDENT_TYPES:
        if canon.lower() == want:
            return canon
    raise OpenFemaInputError(
        f"incident_type={incident_type!r} is not a recognized OpenFEMA incident "
        f"type. Examples: 'Hurricane', 'Flood', 'Severe Storm', 'Tornado', "
        f"'Fire', 'Tropical Storm', 'Earthquake', 'Drought'."
    )


def _resolve_states(
    state_code: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> list[str]:
    """Resolve the spatial selector to a list of 2-letter USPS state codes.

    ``state_code`` (when given) -> that one state. Else ``bbox`` -> every state
    whose envelope intersects the bbox. Raises ``OpenFemaInputError`` when
    neither is given, or when a bbox falls outside US coverage.
    """
    if state_code is not None and str(state_code).strip() != "":
        return [_validate_state_code(state_code)]
    if bbox is not None:
        west, south, east, north = bbox
        states: list[str] = []
        for fips, (s_w, s_s, s_e, s_n) in _STATE_FIPS_BBOXES.items():
            if west <= s_e and east >= s_w and south <= s_n and north >= s_s:
                usps = _FIPS_TO_STATE.get(fips)
                if usps is not None:
                    states.append(usps)
        if not states:
            raise OpenFemaInputError(
                f"bbox={bbox!r} does not intersect any US state envelope; "
                f"fetch_openfema_disasters covers US states + territories only "
                f"(supports_global_query=False)."
            )
        return sorted(states)
    raise OpenFemaInputError(
        "fetch_openfema_disasters requires a spatial selector: pass state_code "
        "(2-letter USPS, e.g. 'FL') or bbox=(west, south, east, north)."
    )


def _validate_year(year: int | None, *, label: str) -> int | None:
    if year is None:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError) as exc:
        raise OpenFemaInputError(f"{label} must be an integer year; got {year!r}") from exc
    cur = _dt.date.today().year
    if not (1953 <= y <= cur + 1):  # FEMA declarations begin 1953
        raise OpenFemaInputError(
            f"{label}={y} out of range; expected 1953..{cur + 1}"
        )
    return y


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``OpenFemaUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise OpenFemaUpstreamError(
            f"Upstream returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise OpenFemaUpstreamError(f"Network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OpenFemaUpstreamError(f"Timed out after {timeout}s fetching {url}") from exc


# ---------------------------------------------------------------------------
# OpenFEMA query.
# ---------------------------------------------------------------------------


def _build_openfema_filter(
    state_code: str,
    incident_type: str | None,
    start_fy: int | None,
) -> str:
    """Build the OData ``$filter`` clause for one state."""
    clauses = [f"state eq '{state_code}'"]
    if incident_type is not None:
        # Escape any single quote in the (validated) incident type.
        safe = incident_type.replace("'", "''")
        clauses.append(f"incidentType eq '{safe}'")
    if start_fy is not None:
        clauses.append(f"fyDeclared ge {int(start_fy)}")
    return " and ".join(clauses)


def _build_openfema_url(odata_filter: str, *, skip: int) -> str:
    params = [
        ("$filter", odata_filter),
        ("$orderby", "declarationDate desc"),
        ("$top", str(_PAGE_SIZE)),
        ("$skip", str(skip)),
        ("$format", "json"),
    ]
    return OPENFEMA_URL + "?" + urllib.parse.urlencode(params)


def _parse_declarations(raw: bytes) -> list[dict[str, Any]]:
    """Parse one OpenFEMA page body -> the list of declaration records."""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenFemaUpstreamError(f"OpenFEMA response is not valid JSON: {exc}") from exc
    recs = obj.get("DisasterDeclarationsSummaries")
    if recs is None:
        raise OpenFemaUpstreamError(
            f"OpenFEMA body missing 'DisasterDeclarationsSummaries' key; "
            f"got keys {list(obj.keys())[:8]}"
        )
    return list(recs)


def _fetch_state_declarations(
    state_code: str,
    incident_type: str | None,
    start_fy: int | None,
) -> list[dict[str, Any]]:
    """Page the OpenFEMA query for one state until a short page (or the cap)."""
    odata_filter = _build_openfema_filter(state_code, incident_type, start_fy)
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        url = _build_openfema_url(odata_filter, skip=skip)
        logger.info("fetch_openfema_disasters: OpenFEMA GET %s", url)
        page = _parse_declarations(_http_get(url))
        out.extend(page)
        if len(page) < _PAGE_SIZE or len(out) >= _MAX_ROWS:
            break
        skip += _PAGE_SIZE
    return out


def _aggregate_by_county(
    records: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    """Group declaration records -> per 5-digit-county-FIPS aggregate.

    Returns ``(by_fips, n_statewide_skipped)`` where ``by_fips`` maps the
    5-digit county FIPS to its aggregate, and ``n_statewide_skipped`` counts
    rows whose ``fipsCountyCode`` was ``"000"`` (statewide / non-county
    designations, which cannot be joined to a county polygon).
    """
    by_fips: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n_declarations": 0,
            "disaster_numbers": set(),
            "incident_types": set(),
            "declaration_types": set(),
            "latest_declaration": None,
            "area_name": None,
            "ia_program": False,
            "pa_program": False,
        }
    )
    n_statewide = 0
    for rec in records:
        sc = str(rec.get("fipsStateCode") or "").strip()
        cc = str(rec.get("fipsCountyCode") or "").strip()
        if not sc or not cc or cc.zfill(3) == "000":
            n_statewide += 1
            continue
        fips = f"{sc.zfill(2)}{cc.zfill(3)}"
        b = by_fips[fips]
        b["n_declarations"] += 1
        dn = rec.get("disasterNumber")
        if dn is not None:
            b["disaster_numbers"].add(str(dn))
        it = rec.get("incidentType")
        if it:
            b["incident_types"].add(str(it))
        dt = rec.get("declarationType")
        if dt:
            b["declaration_types"].add(str(dt))
        dd = rec.get("declarationDate")
        if dd and (b["latest_declaration"] is None or dd > b["latest_declaration"]):
            b["latest_declaration"] = dd
            b["area_name"] = rec.get("designatedArea")
        if rec.get("iaProgramDeclared"):
            b["ia_program"] = True
        if rec.get("paProgramDeclared"):
            b["pa_program"] = True
    return dict(by_fips), n_statewide


# ---------------------------------------------------------------------------
# TIGERweb county geometry.
# ---------------------------------------------------------------------------


def _build_tiger_url(state_fips: str) -> str:
    params = [
        ("where", f"STATE='{state_fips}'"),
        ("outFields", "GEOID,NAME,STATE,COUNTY"),
        ("returnGeometry", "true"),
        ("outSR", "4326"),
        ("f", "geojson"),
    ]
    return TIGER_COUNTY_URL + "?" + urllib.parse.urlencode(params)


def _fetch_county_geometry(state_fips: str) -> dict[str, dict[str, Any]]:
    """Fetch ALL county polygons for one state FIPS -> {GEOID: geojson feature}."""
    url = _build_tiger_url(state_fips)
    logger.info("fetch_openfema_disasters: TIGERweb GET %s", url)
    raw = _http_get(url)
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenFemaUpstreamError(f"TIGERweb response is not valid JSON: {exc}") from exc
    if obj.get("error"):
        raise OpenFemaUpstreamError(f"TIGERweb error for STATE={state_fips}: {obj['error']}")
    out: dict[str, dict[str, Any]] = {}
    for feat in obj.get("features") or []:
        props = feat.get("properties") or {}
        geoid = str(props.get("GEOID") or "").strip()
        if geoid and feat.get("geometry"):
            out[geoid] = feat
    return out


# ---------------------------------------------------------------------------
# FlatGeobuf builder + extent.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(
    by_fips: dict[str, dict[str, Any]],
    geom_by_fips: dict[str, dict[str, Any]],
    *,
    clip_bbox: tuple[float, float, float, float] | None = None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """Join the per-county aggregate to TIGERweb county polygons -> FlatGeobuf.

    One Polygon feature per affected county that has a TIGERweb geometry. When
    ``clip_bbox`` is given (the bbox-selector path), counties whose polygon does
    NOT intersect the bbox are dropped. Raises ``OpenFemaNoDeclarationsError``
    when the join yields zero features.

    Returns ``(fgb_bytes, extent_bbox)``.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import box, shape  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OpenFemaUpstreamError(f"geopandas / shapely not available: {exc}") from exc

    clip_geom = box(*clip_bbox) if clip_bbox is not None else None

    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    for fips, agg in by_fips.items():
        feat = geom_by_fips.get(fips)
        if feat is None:
            continue
        try:
            geom = shape(feat["geometry"])
        except (KeyError, TypeError, ValueError):
            continue
        if clip_geom is not None and not geom.intersects(clip_geom):
            continue
        props = feat.get("properties") or {}
        rows.append(
            {
                "county_fips": fips,
                "county_name": str(props.get("NAME") or agg.get("area_name") or ""),
                "state_fips": fips[:2],
                "n_declarations": int(agg["n_declarations"]),
                "disaster_numbers": ",".join(sorted(agg["disaster_numbers"], key=lambda s: int(s) if s.isdigit() else 0)),
                "incident_types": ",".join(sorted(agg["incident_types"])),
                "declaration_types": ",".join(sorted(agg["declaration_types"])),
                "latest_declaration": str(agg["latest_declaration"] or ""),
                "ia_program": bool(agg["ia_program"]),
                "pa_program": bool(agg["pa_program"]),
            }
        )
        geoms.append(geom)

    if not rows:
        raise OpenFemaNoDeclarationsError(
            "No FEMA-declared counties could be joined to a county polygon for "
            "the requested scope (either no declarations join to a county, or "
            "none of the affected counties fall inside the bbox)."
        )

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    b = gdf.total_bounds
    extent = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_openfema_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read(), extent
    except Exception as exc:
        raise OpenFemaUpstreamError(
            f"FlatGeobuf write failed for {len(rows)} counties: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_openfema_disasters_bytes(
    *,
    states: list[str],
    incident_type: str | None,
    start_fy: int | None,
    clip_bbox: tuple[float, float, float, float] | None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end: OpenFEMA per state -> aggregate -> TIGERweb join -> FGB bytes.

    Raises ``OpenFemaNoDeclarationsError`` when no county-keyed declarations are
    found across all states in scope.
    """
    by_fips: dict[str, dict[str, Any]] = {}
    geom_by_fips: dict[str, dict[str, Any]] = {}
    n_statewide_total = 0

    for usps in states:
        recs = _fetch_state_declarations(usps, incident_type, start_fy)
        state_agg, n_statewide = _aggregate_by_county(recs)
        n_statewide_total += n_statewide
        # Merge state aggregates (FIPS are state-disjoint, so no key collision).
        by_fips.update(state_agg)
        if state_agg:
            state_fips = STATE_FIPS[usps]
            geom_by_fips.update(_fetch_county_geometry(state_fips))

    if not by_fips:
        scope = (
            f"states={states}" + (f", incident_type={incident_type!r}" if incident_type else "")
            + (f", since FY{start_fy}" if start_fy else "")
        )
        extra = (
            f" ({n_statewide_total} statewide/non-county declarations exist but "
            f"cannot be mapped to a county polygon)"
            if n_statewide_total
            else ""
        )
        raise OpenFemaNoDeclarationsError(
            f"No county-level FEMA disaster declarations found for {scope}.{extra} "
            f"Try dropping the incident_type filter, extending the year window, "
            f"or widening the area."
        )

    return _build_flatgeobuf(by_fips, geom_by_fips, clip_bbox=clip_bbox)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    open_world_hint=True,
)
def fetch_openfema_disasters(
    state_code: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    incident_type: str | None = None,
    start_year: int | None = None,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch FEMA disaster declarations as a county-polygon FlatGeobuf overlay.

    Retrieves real US federal disaster declarations (Major Disaster ``DR``,
    Emergency ``EM``, Fire-Management ``FM``) from the FEMA OpenFEMA API,
    aggregates them to ONE record per affected county, and joins each county to
    its Census TIGERweb polygon. The result is a county overlay carrying the
    declaration count, the distinct incident types, the disaster numbers, the
    latest declaration date, and the Individual / Public Assistance flags — the
    canonical **historical hazard-declaration context** layer.

    When to use:
        - The user asks where disasters have been federally declared
          ("which counties in Florida have had a disaster declared", "show me
          the FEMA hurricane declarations in Texas since 2017", "where have
          flood disasters been declared near here").
        - You want the historical record of FEMA declarations as a map overlay
          to frame a hazard study, or to count how many times a county has been
          declared.

    When NOT to use:
        - LIVE / active hazards right now — use the live feeds: NWS active
          warnings, FIRMS / GOES active fire, MRMS precip. Declarations are the
          AFTER-THE-FACT federal record, not a real-time hazard.
        - MODELED hazard footprints (flood depth, plume, surge) — use the engine
          tools (SFINCS / MODFLOW / etc.). This is a declaration record, not a
          physical hazard extent.
        - Flood-zone regulatory boundaries — use ``fetch_fema_nfhl_zones`` (the
          NFHL flood-insurance-rate-map zones). That is a different FEMA product.
        - Non-US disasters — this tool is US states + territories only
          (supports_global_query=False).

    Spatial selector (pass EXACTLY ONE):
        state_code: Optional 2-letter USPS state/territory code (e.g. ``"FL"``,
            ``"TX"``). PREFERRED for state-level asks — one OpenFEMA query plus
            one TIGERweb county fetch for that state, all counties joined.
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326. The tool
            derives every state whose envelope intersects the bbox, queries
            OpenFEMA per state, joins all counties, and CLIPS the overlay to the
            counties whose polygon intersects the bbox (a metro bbox yields a
            handful of counties). When both are given, ``state_code`` wins; when
            neither, ``OpenFemaInputError`` is raised.

    Optional filters:
        incident_type: Optional OpenFEMA incident type (e.g. ``"Hurricane"``,
            ``"Flood"``, ``"Severe Storm"``, ``"Tornado"``, ``"Fire"``,
            ``"Tropical Storm"``, ``"Earthquake"``, ``"Drought"``). Validated
            against the documented OpenFEMA enumeration; an unrecognized value
            raises ``OpenFemaInputError`` rather than silently returning empty.
        start_year: Optional lower-bound federal fiscal year (``fyDeclared``).
            E.g. ``start_year=2017`` restricts to declarations from FY2017 on.
            Omit for the full declaration history (FEMA records begin 1953).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
        (``s3://.../cache/semi-static-7d/openfema_disasters/<key>.fgb``):
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="fema_disaster_declarations"``.
        - Geometry: one county Polygon per affected county, EPSG:4326.
        - ``bbox`` is the joined counties' extent so the camera auto-zooms.
        - Properties per county: ``county_fips`` (5-digit), ``county_name``,
          ``state_fips``, ``n_declarations``, ``disaster_numbers`` (comma-list),
          ``incident_types`` (comma-list), ``declaration_types`` (DR/EM/FM
          comma-list), ``latest_declaration`` (ISO-Z date), ``ia_program`` /
          ``pa_program`` (Individual / Public Assistance ever declared).

    Fallback behaviour (data-source fallback norm): OpenFEMA is the sole
    declaration source. If no county-level declarations are found in scope, or
    none join to a county polygon, ``OpenFemaNoDeclarationsError`` is raised —
    never an empty success-shaped layer. Statewide / non-county-specific
    declarations (``fipsCountyCode == "000"``) are excluded from the county
    overlay and reported in the error message when nothing else joins.

    Cache: ``ttl_class="semi-static-7d"``, ``source_class="openfema_disasters"``.
    Cache key is SHA-256 of the resolved selector (states + incident_type +
    start_year + clip bbox), so identical-scope calls within the week reuse it.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (place name -> bbox / state before this call),
          ``fetch_administrative_boundaries`` (county framing),
          ``compute_zonal_statistics`` (declaration count as a county attribute).
        - Distinct from: ``fetch_fema_nfhl_zones`` (NFHL flood zones — a
          regulatory boundary, not a declaration record); the live-hazard feeds
          (NWS warnings, FIRMS / GOES fire, MRMS) — declarations are historical.
        - Upstream sources: FEMA OpenFEMA DisasterDeclarationsSummaries +
          Census TIGERweb State_County county polygons.

    Errors (FR-AS-11 typed-error surface):
        - ``OpenFemaInputError``: no selector / bad bbox / bad state code / bad
          incident type / bad year (retryable=False).
        - ``OpenFemaUpstreamError``: OpenFEMA or TIGERweb network / HTTP 5xx /
          bad body (retryable=True).
        - ``OpenFemaNoDeclarationsError``: no county-level declarations in scope
          (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (FEMA federal declaration record). Claims from
    OpenFEMA should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=False`` (US + territories).
    """
    # 1. Resolve spatial selector -> states + optional clip bbox.
    resolved_bbox: tuple[float, float, float, float] | None = None
    if (state_code is None or str(state_code).strip() == "") and bbox is not None:
        resolved_bbox = _validate_bbox(bbox)
    states = _resolve_states(state_code, resolved_bbox if bbox is not None else None)

    # 2. Validate optional filters.
    resolved_incident = (
        _validate_incident_type(incident_type)
        if incident_type is not None and str(incident_type).strip() != ""
        else None
    )
    resolved_start_fy = _validate_year(start_year, label="start_year")

    # 3. Cache-key params.
    params: dict[str, Any] = {
        "states": states,
        "incident_type": resolved_incident,
        "start_year": resolved_start_fy,
        "clip_bbox": (
            [round(v, 6) for v in resolved_bbox] if resolved_bbox is not None else None
        ),
    }

    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_openfema_disasters_bytes(
            states=states,
            incident_type=resolved_incident,
            start_fy=resolved_start_fy,
            clip_bbox=resolved_bbox,
        )
        captured["extent"] = extent
        return fgb

    # 4. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_openfema_disasters is cacheable; uri must be set by read_through"
    )

    # 5. Resolve the camera extent (cache HIT -> captured empty -> fall back to
    # the clip bbox; a state-level query has no clip bbox so leave it None and
    # let the inline-vector path fit to the rendered counties).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 6. Descriptive name + stable id.
    scope_tag = ",".join(states) if len(states) <= 3 else f"{len(states)} states"
    parts = [scope_tag]
    if resolved_incident:
        parts.append(resolved_incident)
    if resolved_start_fy:
        parts.append(f"since FY{resolved_start_fy}")
    name = "FEMA disaster declarations - " + " ".join(parts)
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"openfema-disasters-{seed}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="fema_disaster_declarations",
        role="primary",
        units="declaration count",
        bbox=extent_bbox,
    )
