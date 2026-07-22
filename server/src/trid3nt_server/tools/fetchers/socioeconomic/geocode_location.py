"""Nominatim forward geocoder (``geocode_location``) with the US-state snap
fallback and POI/AOI bbox shaping.

Carved out of the original multi-tool ``data_fetch`` module (job-0033) in the
tools/ reorg; behavior and the registered tool surface are unchanged. The
shared typed-error hierarchy + bbox helpers live in
``trid3nt_server.tools.fetchers._fetch_common``.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _DEFAULT_USER_AGENT,
)

__all__ = [
    "geocode_location",
    "GeocodeNoMatchError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.geocode_location")


class GeocodeNoMatchError(UpstreamAPIError):
    """Forward-geocoding found no match for the query (zero/malformed result).

    This is an HONEST, NOT-retryable failure: re-running the SAME query string
    will not suddenly resolve, so ``retryable`` is False and the agent must ask
    the user to refine the place name (add a state/country, fix spelling, name a
    nearby larger place, or supply coordinates) rather than retry.

    It subclasses ``UpstreamAPIError`` so the existing ``except UpstreamAPIError``
    state-snap fallback in ``geocode_location`` STILL fires when a US state is
    recognized in the query (e.g. "south Florida"); when no state is detected,
    the distinct ``error_code`` / non-retryable flag propagate to the surface.
    """

    error_code = "GEOCODE_NO_MATCH"
    retryable = False

# ---------------------------------------------------------------------------
# geocode_location — Nominatim REST
#
# State-snap fallback (NATE directive 2026-06-17): a vague/regional query like
# "south Florida" geocodes via Nominatim with no country/region constraint and
# no sanity check, so an arbitrary first-ranked OSM feature comes back — observed
# resolving to a random house, or to KANSAS for a Florida query — and the agent
# loops re-issuing the same query. The fix: detect a US state in the query and,
# on a wrong-state / failed primary result, snap the bbox to the full state so
# "our bounding box is closer to right than wrong on second attempt". The
# north/south sub-region math is explicitly v2 — NOT now.
# ---------------------------------------------------------------------------

# Directional / qualifier words stripped from the FRONT of a query before the
# state match. "south Florida" -> "florida"; "greater metro Los Angeles, CA"
# leaves the ", CA" abbreviation intact for the abbreviation matcher. Order does
# not matter — we strip leading run of these tokens iteratively.
_STATE_QUALIFIER_PREFIXES: frozenset[str] = frozenset({
    "north", "south", "east", "west", "central",
    "northern", "southern", "eastern", "western",
    "northeast", "northwest", "southeast", "southwest",
    "northeastern", "northwestern", "southeastern", "southwestern",
    "upper", "lower", "upstate", "downstate", "midstate",
    "coastal", "inland", "interior", "rural", "urban",
    "the", "greater", "metro", "metropolitan", "downtown",
    "in", "of", "near",
})

# 2-letter USPS abbreviations the abbreviation matcher accepts. Sourced from the
# shared us_states.STATE_CODE_TO_NAME so the two surfaces never drift. We
# DELIBERATELY exclude marine zones / territories that have no offline bbox row
# below (the _US_STATE_BBOX table is 50 states + DC).
#: Built lazily at module load from us_states (imported inside the helper to
#: avoid a hard import cycle at decoration time).


def _strip_state_qualifiers(text: str) -> str:
    """Remove a leading run of directional / qualifier words from ``text``.

    "south florida" -> "florida"; "the greater los angeles" -> "los angeles";
    "central texas" -> "texas". Stops at the first token that is not a
    qualifier so a real place name is never eaten ("west virginia" is handled
    by the full-name matcher BEFORE this strips "west", see _extract_us_state).
    """
    tokens = text.split()
    while tokens and tokens[0] in _STATE_QUALIFIER_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)

def _extract_us_state(query: str) -> str | None:
    """Detect a US state in a free-text ``query`` and return its canonical name.

    Returns the canonical full state name (e.g. ``"Florida"``,
    ``"District of Columbia"``) or ``None`` if no state is detected.

    Matching strategy (all case/punctuation-insensitive):

    1. Try the WHOLE normalized query as a full state name FIRST — this lets
       "west virginia", "new mexico", "north carolina" win before the leading
       directional word is stripped.
    2. Strip a leading run of directional / qualifier words ("south",
       "greater", "the", ...) and retry the full-name match — this resolves
       "south florida" -> Florida, "central texas" -> Texas.
    3. Scan tokens for an explicit ``, FL`` / ``FL`` 2-letter USPS abbreviation
       with word boundaries. Guarded so the common word "in" is NOT matched as
       Indiana and "or" not as Oregon: a bare 2-letter token only counts when
       it is the LAST token or immediately follows a comma (the "City, ST"
       idiom), and "IN"/"OR"/"OK"/"HI"/"ME" require the comma form.

    Never raises; returns ``None`` for non-string / empty / non-state input.
    """
    if not isinstance(query, str):
        return None
    raw = query.strip()
    if not raw:
        return None

    # Lazy import to dodge any import-cycle at module decoration time.
    from trid3nt_server.tools.fetchers.us_states import STATE_CODE_TO_NAME, STATE_NAME_TO_CODE

    # Normalize: lowercase, drop most punctuation but KEEP commas (the "City, ST"
    # idiom relies on them), collapse whitespace.
    lowered = raw.lower()
    # Preserve commas; turn other punctuation into spaces.
    cleaned = re.sub(r"[^a-z0-9,\s]", " ", lowered)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # A comma-free form for full-name matching.
    no_comma = cleaned.replace(",", " ")
    no_comma = re.sub(r"\s+", " ", no_comma).strip()

    def _canonical(name_lc: str) -> str | None:
        code = STATE_NAME_TO_CODE.get(name_lc)
        if code is None:
            return None
        # Only the 50 states + DC have an offline bbox row; ignore territories.
        canonical = STATE_CODE_TO_NAME.get(code)
        if canonical is None or code not in _US_STATE_BBOX_CODES:
            return None
        return canonical

    # (1) whole normalized query as a full state name.
    hit = _canonical(no_comma)
    if hit is not None:
        return hit

    # (2) strip leading directional / qualifier run, retry full-name match.
    stripped = _strip_state_qualifiers(no_comma)
    if stripped and stripped != no_comma:
        hit = _canonical(stripped)
        if hit is not None:
            return hit

    # (2b) a multi-word query whose TAIL is a full state name
    # ("protected areas in south florida" -> tokens end with "florida";
    # "wildfires near los angeles california" -> ends "california"). Try the
    # last 1-3 tokens (handles "new mexico", "north carolina", "rhode island").
    tail_tokens = stripped.split() if stripped else no_comma.split()
    for n in (3, 2, 1):
        if len(tail_tokens) >= n:
            candidate = " ".join(tail_tokens[-n:])
            hit = _canonical(candidate)
            if hit is not None:
                return hit

    # NOTE: an earlier F71 attempt added a "(2c)" step that scanned for a full
    # state NAME at ANY interior position (to catch "the Florida Panhandle").
    # It was REVERTED — the any-position scan turned the wrong-state sanity
    # guard into a source of WRONG answers: "Kansas City, MO" -> Kansas,
    # "the Washington Monument" -> Washington (snapping a DC AOI to WA state),
    # "the Mississippi River delta near New Orleans" -> Mississippi. The named
    # vernacular cases ("South Florida", "Southern California", "Central Texas")
    # already resolve via (2)/(2b) tail-matching, so the interior scan added
    # real risk for negligible gain. A constrained interior match (head/tail
    # only, feature-noun exclusion, yielding to the City,ST idiom) can be a
    # future safe enhancement; the bare retry-loop steer in adapter.py is the
    # other half of the F71 fix.

    # (3) explicit 2-letter USPS abbreviation with word-boundary guards.
    # The dangerous bare words: in (IN), or (OR), ok (OK), hi (HI), me (ME),
    # de (DE)? "de" rare. We require these to appear in the comma idiom.
    comma_guarded = {"in", "or", "ok", "hi", "me", "de", "co", "id", "la",
                     "pa", "ma", "md", "mo", "mt", "ne", "oh", "wa", "wi"}
    # Build token list preserving comma adjacency markers.
    # Replace ", xx" with a sentinel so we know it followed a comma.
    parts = [p.strip() for p in cleaned.split(",")]
    abbr_to_code = {c.lower(): c for c in _US_STATE_BBOX_CODES}
    for idx, part in enumerate(parts):
        toks = part.split()
        if not toks:
            continue
        # A 2-letter token immediately AFTER a comma (idx>0 and it's the first
        # token of this part) is the "City, ST" idiom — always trust it.
        first = toks[0]
        if idx > 0 and first in abbr_to_code:
            return STATE_CODE_TO_NAME[abbr_to_code[first]]
        # Otherwise only trust an abbreviation that is NOT a dangerous English
        # word, and only when it's the final token of the whole query.
    final_tok = cleaned.replace(",", " ").split()
    if final_tok:
        last = final_tok[-1]
        if last in abbr_to_code and last not in comma_guarded:
            return STATE_CODE_TO_NAME[abbr_to_code[last]]

    # (4) last resort: hand the whole stripped string to the shared
    # us_states.resolve_state_code, which also handles "Washington D.C." and
    # "state of X" idioms. Gate the result to the 50+DC table so territories /
    # marine zones (which have no offline bbox) never leak through.
    #
    # GUARD: resolve_state_code has an UNCONDITIONAL 2-letter fast path that
    # uppercases any 2-char string and matches it as a USPS code — so a bare
    # dangerous English word ("in"->IN, "or"->OR), or a query that strips to
    # one ("the or"->"or"), would false-match a state, bypassing the
    # comma_guarded set built above. Comma-positioned abbreviations were already
    # trusted in step (3); a BARE comma_guarded token reaching here is not a
    # state reference, so skip the fallback for it.
    fallback_query = stripped or no_comma
    fallback_tokens = fallback_query.split()
    if len(fallback_tokens) == 1 and fallback_tokens[0] in comma_guarded:
        return None

    from trid3nt_server.tools.fetchers.us_states import resolve_state_code

    code = resolve_state_code(fallback_query)
    if code is not None and code in _US_STATE_BBOX_CODES:
        return STATE_CODE_TO_NAME[code]
    return None

# Census cartographic state extents (EPSG:4326), [min_lon, min_lat, max_lon,
# max_lat]. Vetted OFFLINE last-resort backstop — _resolve_state_bbox prefers
# the live OSM admin boundingbox and only uses these on failure. Values are the
# Census TIGER state bounding extents rounded outward to ~0.1 deg so the snap
# fully covers the state (closer to right than wrong). Alaska is clamped to the
# main landmass east of the antimeridian; the Aleutian tail crossing 180 is
# intentionally NOT split here (v2).
_US_STATE_BBOX: dict[str, list[float]] = {
    "Alabama": [-88.5, 30.1, -84.9, 35.1],
    "Alaska": [-179.2, 51.2, -129.9, 71.5],
    "Arizona": [-114.9, 31.3, -109.0, 37.1],
    "Arkansas": [-94.7, 33.0, -89.6, 36.6],
    "California": [-124.5, 32.5, -114.1, 42.1],
    "Colorado": [-109.1, 36.9, -102.0, 41.1],
    "Connecticut": [-73.8, 40.9, -71.7, 42.1],
    "Delaware": [-75.8, 38.4, -75.0, 39.9],
    "District of Columbia": [-77.2, 38.7, -76.9, 39.0],
    "Florida": [-87.7, 24.4, -79.9, 31.1],
    "Georgia": [-85.7, 30.3, -80.8, 35.1],
    "Hawaii": [-160.3, 18.8, -154.7, 22.3],
    "Idaho": [-117.3, 41.9, -110.9, 49.1],
    "Illinois": [-91.6, 36.9, -87.4, 42.6],
    "Indiana": [-88.1, 37.7, -84.7, 41.8],
    "Iowa": [-96.7, 40.3, -90.1, 43.6],
    "Kansas": [-102.1, 36.9, -94.5, 40.1],
    "Kentucky": [-89.6, 36.4, -81.9, 39.2],
    "Louisiana": [-94.1, 28.9, -88.7, 33.1],
    "Maine": [-71.2, 42.9, -66.8, 47.6],
    "Maryland": [-79.5, 37.8, -75.0, 39.8],
    "Massachusetts": [-73.6, 41.1, -69.8, 42.9],
    "Michigan": [-90.5, 41.6, -82.3, 48.4],
    "Minnesota": [-97.3, 43.4, -89.4, 49.5],
    "Mississippi": [-91.7, 30.1, -88.0, 35.1],
    "Missouri": [-95.8, 35.9, -89.0, 40.7],
    "Montana": [-116.1, 44.3, -104.0, 49.1],
    "Nebraska": [-104.1, 39.9, -95.2, 43.1],
    "Nevada": [-120.1, 35.0, -114.0, 42.1],
    "New Hampshire": [-72.6, 42.6, -70.5, 45.4],
    "New Jersey": [-75.6, 38.8, -73.8, 41.4],
    "New Mexico": [-109.1, 31.3, -102.9, 37.1],
    "New York": [-79.8, 40.4, -71.8, 45.1],
    "North Carolina": [-84.4, 33.8, -75.4, 36.7],
    "North Dakota": [-104.1, 45.9, -96.5, 49.1],
    "Ohio": [-84.9, 38.3, -80.5, 42.4],
    "Oklahoma": [-103.1, 33.6, -94.4, 37.1],
    "Oregon": [-124.6, 41.9, -116.4, 46.4],
    "Pennsylvania": [-80.6, 39.7, -74.6, 42.4],
    "Rhode Island": [-71.9, 41.1, -71.1, 42.1],
    "South Carolina": [-83.4, 32.0, -78.5, 35.3],
    "South Dakota": [-104.1, 42.4, -96.4, 46.0],
    "Tennessee": [-90.4, 34.9, -81.6, 36.7],
    "Texas": [-106.7, 25.8, -93.5, 36.6],
    "Utah": [-114.1, 36.9, -109.0, 42.1],
    "Vermont": [-73.5, 42.7, -71.5, 45.1],
    "Virginia": [-83.7, 36.5, -75.2, 39.5],
    "Washington": [-124.8, 45.5, -116.9, 49.1],
    "West Virginia": [-82.7, 37.2, -77.7, 40.7],
    "Wisconsin": [-92.9, 42.4, -86.8, 47.4],
    "Wyoming": [-111.1, 40.9, -104.0, 45.1],
}

#: USPS codes covered by the offline bbox table (50 states + DC). Used by the
#: abbreviation matcher to ignore territories / marine zones that have no row.
_US_STATE_BBOX_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
})

def _resolve_state_bbox(state_name: str) -> tuple[list[float], float, float, str]:
    """Resolve a canonical state name to ``(bbox, lat, lon, source)``.

    PREFERS the live OSM admin boundary: a Nominatim ``featuretype=state``
    lookup constrained to ``countrycodes=us`` returns the REAL state polygon's
    bounding box (more accurate than the offline table, and reflects OSM edits).
    Falls back to the vetted ``_US_STATE_BBOX`` table on ANY failure / empty
    result so this helper NEVER raises.

    ``bbox`` is ``[min_lon, min_lat, max_lon, max_lat]`` (project canonical).
    ``lat`` / ``lon`` is the bbox centroid. ``source`` is ``"nominatim-state"``
    when the live lookup succeeded, else ``"offline-state-table"``.
    """
    fallback = _US_STATE_BBOX.get(state_name)
    if fallback is None:
        # Should not happen — _extract_us_state only returns table-backed names.
        raise BboxInvalidError(
            f"no offline bbox for state {state_name!r}"
        )

    def _centroid(bb: list[float]) -> tuple[float, float]:
        return ((bb[1] + bb[3]) / 2.0, (bb[0] + bb[2]) / 2.0)

    user_agent = os.environ.get(
        "TRID3NT_NOMINATIM_USER_AGENT", _DEFAULT_USER_AGENT
    )
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": f"{state_name}, United States",
        "countrycodes": "us",
        "featuretype": "state",
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
        "polygon_geojson": 0,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body:
            top = body[0]
            bb = top.get("boundingbox", [])
            if len(bb) == 4:
                south, north, west, east = (float(v) for v in bb)
                live_bbox = [west, south, east, north]
                # Only trust a well-ordered, non-degenerate live bbox. A
                # degenerate / inverted OSM response (e.g. [0,0,0,0]) fails
                # these comparisons (NaN also fails) and falls through to the
                # vetted offline table rather than shipping a bad extent.
                if west < east and south < north:
                    lat = float(top.get("lat", _centroid(live_bbox)[0]))
                    lon = float(top.get("lon", _centroid(live_bbox)[1]))
                    return live_bbox, lat, lon, "nominatim-state"
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.info(
            "state-bbox live lookup failed for %r (%s); using offline table",
            state_name,
            exc,
        )

    lat, lon = _centroid(fallback)
    return list(fallback), lat, lon, "offline-state-table"

def _centroid_in_bbox(
    lat: float, lon: float, bbox: list[float], margin: float = 1.0
) -> bool:
    """True if ``(lat, lon)`` falls inside ``bbox`` widened by ``margin`` deg.

    ``bbox`` is ``[min_lon, min_lat, max_lon, max_lat]``. The margin (default
    1 degree, ~110 km) tolerates a precise match whose centroid sits just
    outside the coarse offline/admin extent (e.g. a coastal city) without
    admitting a wrong-STATE match — a Kansas-for-Florida result is hundreds of
    km out and still fails this check.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        (min_lon - margin) <= lon <= (max_lon + margin)
        and (min_lat - margin) <= lat <= (max_lat + margin)
    )

_GEOCODE_LOCATION_METADATA = AtomicToolMetadata(
    name="geocode_location",
    ttl_class="dynamic-1h",
    source_class="geocode",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# OPEN-10 (NATE-reported): "downtown Tampa" and similar sub-locality phrasings
# resolved to a SINGLE BUILDING/POI footprint (bbox tens of meters across), so
# every layer fetched against the case AOI came back empty -- all of NATE's
# old Tampa cases were invisible for this reason. Live-confirmed root cause
# (2026-07-11): Nominatim's ONLY match for "downtown Tampa" is a
# ``category=railway, type=tram_stop`` node literally named "Downtown Tampa"
# (a streetcar stop), bbox ~11m x 11m -- there is no competing neighbourhood
# entity in OSM's Tampa data (compare "downtown Miami" / "midtown Atlanta",
# which resolve cleanly to ``category=place, type=neighbourhood`` with a
# proper ~2 km bbox). Two-part fix below:
#
#   (a) RESULT-CLASS PREFERENCE: with ``limit=1`` the old code could not even
#       SEE an alternate candidate. Widen the query and, when the top hit is
#       NOT itself a place/administrative-boundary result, scan the remaining
#       candidates for the first one that is (city/town/village/hamlet/
#       suburb/neighbourhood/quarter, or an admin boundary) and promote it.
#       This is deliberately broad -- rather than an allowlist of "POI
#       classes" (building/amenity/shop/office, ...), it demotes ANYTHING
#       that isn't place-class, because the live Tampa failure is a railway
#       node, not a building. Skipped entirely for queries that clearly name
#       a POI (street address, named landmark) so a genuine point lookup is
#       never redirected to the surrounding place.
#   (b) MINIMUM AOI FLOOR: whichever candidate wins, a bbox smaller than
#       ~1 km on its long axis is still unusable as a case AOI (this is what
#       actually fixes "downtown Tampa" itself -- Nominatim has no better
#       candidate to promote to). Expand it to a 2 km square centered on the
#       point and attach an honest ``expansion_note`` so the model narrates
#       the widening instead of silently handing back an invisible-layers AOI.
# ---------------------------------------------------------------------------

#: Nominatim ``category`` values that represent an area/place (as opposed to
#: a point-scale POI). Matches the taxonomy observed live in jsonv2 responses
#: (``category`` is the jsonv2 field name -- there is no ``class`` key).
_PLACE_CATEGORIES: frozenset[str] = frozenset({"place"})

#: Nominatim ``type`` values under ``category="place"`` (or the closest OSM
#: place-node types) that make a usable area AOI. Excludes point-scale place
#: types such as ``"isolated_dwelling"``.
_PLACE_TYPES: frozenset[str] = frozenset({
    "city", "town", "village", "hamlet", "suburb", "neighbourhood",
    "quarter", "borough", "municipality", "county", "state", "region",
    "district", "city_block", "island",
})

def _is_place_class(candidate: dict[str, Any]) -> bool:
    """True if ``candidate`` (a raw Nominatim jsonv2 result) is an area/place.

    A place-class result is ``category="place"`` with an area-scale ``type``
    (city/town/suburb/neighbourhood/...), OR ``category="boundary"`` with
    ``type="administrative"`` (counties, states, admin areas at any level).
    """
    category = candidate.get("category")
    if category in _PLACE_CATEGORIES:
        return candidate.get("type") in _PLACE_TYPES
    if category == "boundary" and candidate.get("type") == "administrative":
        return True
    return False

#: Query substrings that clearly name a point-of-interest rather than an
#: area -- the class-preference reorder in ``_fetch_nominatim_geocode_bytes``
#: MUST NOT touch these queries, or e.g. "Tampa International Airport" would
#: get redirected to the surrounding city boundary instead of the airport.
_POI_INTENT_KEYWORDS: tuple[str, ...] = (
    "airport", "station", "stadium", "arena", "hospital", "university",
    "college", "courthouse", "terminal", "port authority", "mall",
    "museum", "library", "cemetery", "monument", "memorial",
)

#: A leading house number ("123 Main St, Tampa, FL") is a street address --
#: always a precise point lookup, never an area-intent query.
_STREET_ADDRESS_RE = re.compile(r"^\s*\d+[\d-]*\s+\S")

def _looks_like_poi_query(query: str) -> bool:
    """True if ``query`` clearly names a point-of-interest, not an area.

    Governs the OPEN-10 class-preference reorder: point-intent queries
    (street addresses, named landmarks like an airport or a stadium) pass
    through with Nominatim's own top-ranked result, unchanged.
    """
    if _STREET_ADDRESS_RE.match(query):
        return True
    lowered = query.lower()
    return any(keyword in lowered for keyword in _POI_INTENT_KEYWORDS)

#: Kilometers per degree of latitude (also used as the per-degree-of-longitude
#: figure at the equator; longitude shrinks by cos(latitude) elsewhere). An
#: equirectangular approximation is intentional here -- OPEN-10 only needs to
#: tell "building footprint" from "usable AOI" apart, not survey-grade
#: distance.
_KM_PER_DEGREE = 111.32

#: Below this long-axis size (km) a bbox reads as a point-scale footprint
#: (building, POI node, tram stop, ...) rather than a usable case AOI.
_MIN_AOI_AXIS_KM = 1.0

#: Side length (km) of the square AOI a point-scale geocode result is
#: expanded to.
_EXPANDED_AOI_SIDE_KM = 2.0

def _bbox_long_axis_km(
    west: float, south: float, east: float, north: float, lat: float
) -> float:
    """Approximate the longer side of a WGS84 bbox in kilometers."""
    height_km = abs(north - south) * _KM_PER_DEGREE
    width_km = abs(east - west) * _KM_PER_DEGREE * math.cos(math.radians(lat))
    return max(height_km, width_km)

def _square_km_bbox(
    lat: float, lon: float, side_km: float
) -> tuple[float, float, float, float]:
    """Return ``(west, south, east, north)`` for a ``side_km`` square centered
    on ``(lat, lon)`` (same equirectangular approximation as
    ``_bbox_long_axis_km``).
    """
    half_deg_lat = (side_km / 2.0) / _KM_PER_DEGREE
    # Guard near the poles so this never divides by ~0; irrelevant in
    # practice (case AOIs are not polar), but keeps the helper total.
    cos_lat = max(abs(math.cos(math.radians(lat))), 0.01)
    half_deg_lon = (side_km / 2.0) / (_KM_PER_DEGREE * cos_lat)
    return (
        lon - half_deg_lon,
        lat - half_deg_lat,
        lon + half_deg_lon,
        lat + half_deg_lat,
    )

def _fetch_nominatim_geocode_bytes(query: str) -> bytes:
    """Forward-geocode ``query`` via OpenStreetMap Nominatim and return JSON bytes.

    Honors Nominatim usage policy:
    - descriptive User-Agent identifying the app + contact;
    - ``format=jsonv2`` for stable JSON shape;
    - ``limit=5`` so a same-locality place-class alternate is visible to the
      OPEN-10 class-preference reorder below (was ``limit=1``, which could
      not see past a single point-scale top hit -- see the module comment
      above this function);
    - ``polygon_geojson=0`` (we just want bbox + lat/lon);
    - one request per cache-bucket window (the ``dynamic-1h`` class naturally
      throttles repeat queries — see ``read_through``).

    Area-intent semantics (OPEN-10): when the top-ranked hit is a point-scale
    POI (not itself a place/administrative-boundary result) and the query
    does not clearly name a POI, the first place-class candidate among the
    remaining results is promoted instead. Whichever candidate wins, if its
    bbox is still smaller than ~1 km on its long axis, it is expanded to a
    2 km square centered on the point and the returned dict carries an
    additive ``expansion_note`` key the agent narrates truthfully.

    Returns the JSON-encoded structured result the tool body further
    massages into a ``GeocodedLocation``-shaped dict.
    """
    if not query or not query.strip():
        raise BboxInvalidError("geocode_location requires a non-empty query")

    user_agent = os.environ.get("TRID3NT_NOMINATIM_USER_AGENT", _DEFAULT_USER_AGENT)
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query.strip(),
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 0,
        "polygon_geojson": 0,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"Nominatim search failed for query={query!r}: {exc}"
        ) from exc
    except ValueError as exc:
        raise UpstreamAPIError(
            f"Nominatim returned non-JSON for query={query!r}: {exc}"
        ) from exc

    if not body:
        raise GeocodeNoMatchError(
            f"Could not locate {query!r}. Try refining the place name "
            f"(add City, ST or a country, or check the spelling)."
        )

    top = body[0]

    # OPEN-10 part (a): promote a place-class candidate over a point-scale
    # top hit, unless the query clearly names a POI (street address, named
    # landmark) -- see the module comment above this function for the live
    # "downtown Tampa" vs. "downtown Miami" evidence behind this heuristic.
    if not _is_place_class(top) and not _looks_like_poi_query(query):
        for candidate in body[1:]:
            if _is_place_class(candidate):
                logger.info(
                    "geocode_location query=%r top hit category=%r/type=%r "
                    "(point-scale); promoting place-class candidate %r",
                    query,
                    top.get("category"),
                    top.get("type"),
                    candidate.get("display_name"),
                )
                top = candidate
                break

    # Nominatim returns boundingbox as [south, north, west, east] strings.
    bb = top.get("boundingbox", [])
    if len(bb) != 4:
        raise GeocodeNoMatchError(
            f"Could not locate {query!r} (no valid bounding box returned). Try "
            f"refining the place name (add City, ST or a country, or check the "
            f"spelling)."
        )
    try:
        south, north, west, east = [float(v) for v in bb]
    except (TypeError, ValueError) as exc:
        raise UpstreamAPIError(
            f"Nominatim boundingbox non-numeric: {bb!r}"
        ) from exc

    lat = float(top.get("lat", (south + north) / 2.0))
    lon = float(top.get("lon", (west + east) / 2.0))

    # OPEN-10 part (b): MINIMUM AOI FLOOR. Whatever candidate won above, a
    # bbox smaller than ~1 km on its long axis is not a usable case AOI --
    # expand it to a 2 km square and carry an honest note so the model
    # narrates the widening instead of silently returning an
    # invisible-everything AOI.
    expansion_note: str | None = None
    long_axis_km = _bbox_long_axis_km(west, south, east, north, lat)
    if long_axis_km < _MIN_AOI_AXIS_KM:
        west, south, east, north = _square_km_bbox(lat, lon, _EXPANDED_AOI_SIDE_KM)
        expansion_note = (
            f"Geocoder returned a building-scale footprint "
            f"(~{long_axis_km * 1000.0:.0f} m across) for {query.strip()!r}; "
            f"expanded to a {_EXPANDED_AOI_SIDE_KM:.0f} km area of interest. "
            f"Draw an AOI for precise control."
        )

    structured = {
        "name": top.get("display_name", query),
        "latitude": lat,
        "longitude": lon,
        # Normalize to (min_lon, min_lat, max_lon, max_lat) — the project
        # canonical bbox shape (matches LayerURI / Census / py3dep).
        "bbox": [west, south, east, north],
        "source": "nominatim",
        "query": query,
        "osm_type": top.get("osm_type"),
        "osm_id": top.get("osm_id"),
        "place_id": top.get("place_id"),
    }
    if expansion_note is not None:
        structured["expansion_note"] = expansion_note
    return json.dumps(structured).encode("utf-8")

@register_tool(
    _GEOCODE_LOCATION_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (OSM Nominatim API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def geocode_location(query: str, **_extra_ignored: Any) -> dict[str, Any]:
    """Translate a free-text place name into a bbox and canonical name via OpenStreetMap Nominatim.

    **What it does:** Forward-geocodes a human-readable location string to a
    WGS84 bounding box, centroid latitude/longitude, and canonical place name
    using the OpenStreetMap Nominatim REST API. The result is cached for one
    hour (``dynamic-1h``), so repeated references to the same place within a
    session are free.

    **When to use:**
    - User asks to "model flooding in Fort Myers, FL" or "show wildfires near
      Los Angeles" — convert the place name to a bbox before calling spatial
      fetch tools.
    - The agent needs to translate a textual event location from the Hazard
      Event Pipeline (``EventMetadata.location_name``) into a usable bbox.
    - Any workflow step that starts from a city, county, neighborhood, or
      named geographic feature rather than coordinates.

    **When NOT to use:**
    - Reverse geocoding (coordinates → place name) — Nominatim has a separate
      ``/reverse`` endpoint; use ``web_fetch`` or a future dedicated tool.
    - Routing or turn-by-turn distance queries — Nominatim does not support
      them; use a routing API.
    - High-precision parcel-level address resolution — Nominatim is
      street-address level at best; use a dedicated geocoding provider for
      sub-parcel accuracy.
    - Queries where bbox coverage matters: the returned bbox reflects OSM's
      administrative boundary for the named place, which can be very large for
      counties or states; narrow it before passing to ``fetch_dem`` or similar
      large-download tools.

    **Parameters:**
    - ``query`` (str): Free-text place name or description.
      Examples: ``"Fort Myers, FL"``, ``"Lee County Florida"``,
      ``"Gulf of Mexico"``. Must be non-empty.

    **Returns:**
    A plain dict with keys:
    - ``name`` (str): canonical OSM display name.
    - ``bbox`` (list[float]): ``[min_lon, min_lat, max_lon, max_lat]`` in
      EPSG:4326 — feeds directly into ``fetch_dem``, ``fetch_buildings``,
      ``fetch_population``, ``fetch_landcover``, etc. Always at least ~2 km
      on its long axis (see the AOI floor below) — this bbox is always a
      usable case AOI, never a bare point/building footprint.
    - ``latitude`` / ``longitude`` (float): centroid of the matched feature.
    - ``source`` (str): ``"nominatim"`` on a precise match, or
      ``"state-bbox-fallback"`` when the state-snap fired (see below).
    - ``osm_type``, ``osm_id``, ``place_id`` (str / int): OSM provenance fields
      (``None`` on a state-snap, where there is no single OSM feature).
    - ``fallback_reason`` (str, ADDITIVE — present ONLY on a state-snap): an
      honest human-readable explanation the agent narrates truthfully, e.g.
      *"No precise match for 'south Florida'; snapped to the full state of
      Florida. Refine the prompt for a smaller area."*
    - ``expansion_note`` (str, ADDITIVE — present ONLY when the AOI floor
      fired, see below): an honest note the agent narrates truthfully, e.g.
      *"Geocoder returned a building-scale footprint (~11 m across) for
      'downtown Tampa'; expanded to a 2 km area of interest. Draw an AOI for
      precise control."*

    **State-snap fallback (NATE directive):** vague/regional queries
    ("south Florida", "protected areas in south Florida") used to geocode to an
    arbitrary first-ranked OSM feature (observed: a random house, or KANSAS for
    a Florida query). Now, if a US state is detected in the query, the primary
    result's centroid is sanity-checked against that state's bounding box; a
    wrong-state result (or a "no results" / upstream failure) snaps the bbox to
    the full state (live OSM state admin boundary, with a vetted offline Census
    extent as last resort) and records an honest ``fallback_reason``. A PRECISE
    in-state query ("Fort Myers, FL", "Lee County Florida") passes the
    sanity-check and is returned UNCHANGED — it is never widened. When NO state
    is detected and the primary geocode fails, the typed error still raises
    (genuine failures are never swallowed).

    **Area-intent semantics + AOI floor (OPEN-10, NATE-reported):**
    sub-locality phrasings like "downtown Tampa" used to resolve to a SINGLE
    BUILDING/POI footprint — e.g. the live top (and only) Nominatim match for
    "downtown Tampa" is a railway tram-stop node named "Downtown Tampa", bbox
    ~11 m across — so every layer fetched against the resulting AOI came back
    empty. Two fixes now run inside the fetch:
    (a) *result-class preference* — when the top-ranked hit is a point-scale
    POI (not itself a place or administrative-boundary result) and the query
    does not clearly name a POI (no street-address house number, no landmark
    keyword like "airport" or "stadium"), the first place-class candidate
    among the next few results (city/town/village/suburb/neighbourhood/
    quarter/admin boundary) is promoted instead — e.g. "downtown Miami" and
    "midtown Atlanta" already resolve straight to a neighbourhood polygon and
    are untouched by this rule;
    (b) *minimum AOI floor* — whichever candidate wins, if its bbox is still
    smaller than ~1 km on its long axis (the Tampa case: there is no
    neighbourhood entity to promote to), it is expanded to a 2 km square
    centered on the point and the ``expansion_note`` key is set. Bboxes for
    genuine POI queries and ordinary city/county/state matches are returned
    exactly as Nominatim reports them — this only ever widens a
    building-scale result, never a real area.

    **Cross-tool dependencies:**
    - Upstream of: ``fetch_dem``, ``fetch_buildings``, ``fetch_population``,
      ``fetch_landcover``, ``fetch_river_geometry``,
      ``fetch_administrative_boundaries``, ``fetch_nws_event``,
      ``fetch_firms_active_fire``, and most other bbox-based fetchers.
    - Called internally by ``model_flood_scenario`` workflow to resolve a
      user-supplied location string before fetching DEM/landcover.

    FR-CE-8: The fetch is routed through ``read_through`` so two identical
    queries within the same hourly window reuse the cached response. The
    cache class is ``"dynamic-1h"`` per FR-DC-2 active-state-ish (geocoding
    answers DO change as Nominatim's OSM index updates, but on a slower
    cadence than hourly).

    Side effect: per FR-TA-2 §"Location-resolved emission" / FR-AS-7, the
    agent surface emits a ``location-resolved`` WebSocket message when this
    tool returns so the client auto-snaps the map. The emission seam is
    in the agent's server.py M1 module — surfaced as
    OQ-33-LOCATION-RESOLVED-EMISSION-SEAM for the agent job that owns
    envelope emission this sprint (job-0035) to wire up.

    Nominatim usage policy: User-Agent is sent on every request; the
    ``dynamic-1h`` cache class naturally throttles repeat queries (one
    fetch per hour-bucket per distinct query).
    """
    if not isinstance(query, str) or not query.strip():
        raise BboxInvalidError("geocode_location requires a non-empty string query")

    # Detect a US state up front so we know whether the state-snap fallback is
    # eligible for either failure mode (wrong-state result OR no-result error).
    detected_state = _extract_us_state(query)

    params = {"query": query.strip()}
    try:
        result = read_through(
            metadata=_GEOCODE_LOCATION_METADATA,
            params=params,
            ext="json",
            fetch_fn=lambda: _fetch_nominatim_geocode_bytes(query),
        )
    except UpstreamAPIError:
        # No precise match / upstream failure. If we recognized a state, snap to
        # it instead of dead-ending (fallback norm: primary -> fallback ->
        # honest, never silent). This branch ALSO catches GeocodeNoMatchError
        # (a subclass of UpstreamAPIError) so a no-match query like "south
        # Florida" still snaps to the state. Otherwise the genuine failure
        # propagates -- for GEOCODE_NO_MATCH that means a non-retryable error
        # the agent surfaces as a clarify-the-place request, not a retry.
        if detected_state is not None:
            return _state_snap_payload(
                query,
                detected_state,
                reason=(
                    f"No precise match for {query.strip()!r}; snapped to the "
                    f"full state of {detected_state}. Refine the prompt for a "
                    f"smaller area."
                ),
            )
        raise

    # The fetched (or cached) payload is JSON bytes; decode and return as a
    # structured dict. The cache URI is intentionally NOT returned to the LLM
    # — Tier separation (invariant 5): no gs:// URIs leak into model text.
    payload = json.loads(result.data.decode("utf-8"))

    # Sanity-check: if a state was detected but the primary result's centroid
    # lands OUTSIDE that state (with a tolerance margin), the match is wrong —
    # e.g. a "south Florida" query that resolved to Kansas. Snap to the state.
    if detected_state is not None:
        state_bbox = _US_STATE_BBOX.get(detected_state)
        try:
            lat = float(payload.get("latitude"))
            lon = float(payload.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None  # type: ignore[assignment]
        if state_bbox is not None and (
            lat is None
            or lon is None
            or not _centroid_in_bbox(lat, lon, state_bbox)
        ):
            logger.info(
                "geocode_location query=%r resolved OUTSIDE detected state %r "
                "(centroid=%s,%s) — snapping to state bbox",
                query,
                detected_state,
                lat,
                lon,
            )
            return _state_snap_payload(
                query,
                detected_state,
                reason=(
                    f"No precise match for {query.strip()!r}; snapped to the "
                    f"full state of {detected_state}. Refine the prompt for a "
                    f"smaller area."
                ),
            )

    logger.info(
        "geocode_location query=%r resolved name=%r cache_hit=%s",
        query,
        payload.get("name"),
        result.hit,
    )
    return payload

def _state_snap_payload(
    query: str, state_name: str, *, reason: str
) -> dict[str, Any]:
    """Build the backward-compatible geocode dict for a state-snap fallback.

    Same keys as the primary path (``name``, ``bbox``, ``latitude``,
    ``longitude``, ``source``, ``query``, ``osm_type``, ``osm_id``,
    ``place_id``) plus the ADDITIVE ``fallback_reason`` honest note. Prefers
    the live OSM state admin boundary, falling back to the offline Census
    extent (``_resolve_state_bbox`` handles that and never raises).
    """
    bbox, lat, lon, state_source = _resolve_state_bbox(state_name)
    logger.info(
        "geocode_location state-snap query=%r state=%r bbox=%s source=%s",
        query,
        state_name,
        bbox,
        state_source,
    )
    return {
        "name": f"{state_name}, United States",
        "bbox": bbox,
        "latitude": lat,
        "longitude": lon,
        "source": "state-bbox-fallback",
        "query": query,
        "osm_type": None,
        "osm_id": None,
        "place_id": None,
        # Additive, honest narration hook (fallback norm).
        "fallback_reason": reason,
        # Provenance of the snap bbox itself (live OSM vs offline table).
        "state_bbox_source": state_source,
    }
