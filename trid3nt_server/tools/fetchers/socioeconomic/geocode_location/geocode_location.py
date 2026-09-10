"""Nominatim forward geocoder with the US-state snap fallback and AOI bbox shaping."""

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

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.geocode_location.geocode_location")


class GeocodeNoMatchError(UpstreamAPIError):
    """Forward-geocoding found no match. An HONEST, NOT-retryable failure: the SAME
    query string will not resolve on a retry, so the caller must refine the place name
    -- add a state, fix a spelling, name a larger place -- rather than re-ask."""

    error_code = "GEOCODE_NO_MATCH"
    retryable = False

# ---------------------------------------------------------------------------
# THE STATE-SNAP FALLBACK. A vague or regional query like "south Florida" geocodes
# with no country or region constraint and no sanity check, so an arbitrary
# first-ranked feature comes back -- observed resolving to a random house, and to
# KANSAS for a Florida query -- and the caller then loops re-issuing the same query.
# So a US state named in the query is detected, and a wrong-state or failed primary
# result snaps the bbox to the full state: closer to right than wrong on the second
# attempt. Sub-region math within a state is deliberately not attempted.
# ---------------------------------------------------------------------------

# Directional / qualifier words stripped from the FRONT of a query before the
# state match. "south Florida" -> "florida"; "greater metro Los Angeles, CA"
# leaves the ", CA" abbreviation intact for the abbreviation matcher. Order does
# not matter -- we strip leading run of these tokens iteratively.
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
    """Remove a LEADING run of directional or qualifier words, stopping at the first
    token that is not one, so a real place name is never eaten. A state whose own name
    starts with such a word is matched in full before this runs."""
    tokens = text.split()
    while tokens and tokens[0] in _STATE_QUALIFIER_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)

def _extract_us_state(query: str) -> str | None:
    """Detect a US state in a free-text query and return its canonical full name, or
    ``None``. Never raises, and never returns a state for empty or non-state input."""

    # The order matters, and every step is case- and punctuation-insensitive. The WHOLE
    # normalized query is tried as a full state name FIRST, so "west virginia" and
    # "north carolina" win before their leading word is stripped. Then a leading run of
    # directional or qualifier words is stripped and the full-name match retried. Then
    # tokens are scanned for a 2-letter USPS abbreviation, guarded so the common words
    # "in" and "or" are NOT read as Indiana and Oregon: a bare 2-letter token counts
    # only as the LAST token, and the dangerous ones require the "City, ST" comma form.
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

    # There is deliberately NO scan for a state NAME at an interior position. An
    # any-position scan turns the wrong-state sanity guard into a source of WRONG
    # answers: "Kansas City, MO" reads as Kansas, "the Washington Monument" snaps a DC
    # AOI to Washington state, "the Mississippi River delta near New Orleans" reads as
    # Mississippi. The vernacular cases already resolve by tail-matching above, so the
    # interior scan buys negligible coverage for real risk.

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
        # token of this part) is the "City, ST" idiom -- always trust it.
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
    # uppercases any 2-char string and matches it as a USPS code -- so a bare
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
# max_lat]. The vetted OFFLINE last-resort backstop, used only when the live admin
# lookup fails. Values are the published state bounding extents rounded OUTWARD to
# ~0.1 deg, so the snap fully covers the state: closer to right than wrong. Alaska is
# clamped to the main landmass east of the antimeridian, and its Aleutian tail crossing
# 180 is deliberately not split here.
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
    """Resolve a canonical state name to ``(bbox, lat, lon, source)``, PREFERRING the
    live admin boundary and falling back to the vetted offline table on ANY failure or
    empty result, so this NEVER raises. ``source`` names which one answered."""
    fallback = _US_STATE_BBOX.get(state_name)
    if fallback is None:
        # Should not happen -- _extract_us_state only returns table-backed names.
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
    """True if ``(lat, lon)`` falls inside ``bbox`` widened by ``margin`` degrees. The
    default margin tolerates a precise coastal match whose centroid sits just outside a
    coarse extent, while a wrong-STATE result is hundreds of km out and still fails."""
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
# Sub-locality phrasings can resolve to a single building/POI footprint when
# OSM has no competing neighbourhood entity for that name, producing a bbox
# too small to be a usable case AOI. Two-part mitigation:
#
#   (a) RESULT-CLASS PREFERENCE: widen the query and, when the top hit is NOT
#       itself a place/administrative-boundary result, scan the remaining
#       candidates for the first one that is (city/town/village/hamlet/
#       suburb/neighbourhood/quarter, or an admin boundary) and promote it.
#       Deliberately broad -- demotes ANYTHING non-place-class rather than
#       allowlisting POI classes (building/amenity/shop/office, ...), since
#       the failure mode is any non-place node, not just buildings. Skipped
#       for queries that clearly name a POI (street address, named landmark)
#       so a genuine point lookup is never redirected to the surrounding
#       place.
#   (b) MINIMUM AOI FLOOR: whichever candidate wins, a bbox smaller than
#       ~1 km on its long axis is still unusable as a case AOI. Expand it to
#       a 2 km square centered on the point and attach an honest
#       ``expansion_note`` so the model narrates the widening instead of
#       silently handing back an invisible-layers AOI.
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
    """True if ``candidate`` is an area or place: a ``place`` category with an area-scale
    type, or a ``boundary`` category with an administrative type, which covers counties,
    states and admin areas at any level."""
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
    """True if ``query`` clearly names a point of interest rather than an area. A
    point-intent query -- a street address, a named landmark -- passes through with the
    geocoder's own top-ranked result, unchanged by the class-preference reorder."""
    if _STREET_ADDRESS_RE.match(query):
        return True
    lowered = query.lower()
    return any(keyword in lowered for keyword in _POI_INTENT_KEYWORDS)

#: Kilometers per degree of latitude (also used as the per-degree-of-longitude
#: figure at the equator; longitude shrinks by cos(latitude) elsewhere). An
#: equirectangular approximation is intentional here: the AOI floor only needs to
#: tell "building footprint" from "usable AOI" apart, not survey-grade distance.
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
    """``(west, south, east, north)`` for a ``side_km`` square centred on ``(lat, lon)``,
    under the same equirectangular approximation the long-axis measure uses."""
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
    """Forward-geocode ``query`` and return the JSON bytes the tool body shapes into its
    result dict."""

    # The request honours the service's usage policy: a descriptive User-Agent naming
    # the application and a contact, the stable jsonv2 format, no polygon geometry since
    # only a bbox and centroid are wanted, and one request per cache-bucket window,
    # which the hourly cache class throttles. The result limit is above one so a
    # same-locality place-class alternate is VISIBLE to the class-preference reorder: at
    # one, nothing could be seen past a point-scale top hit.
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

    # RESULT-CLASS PREFERENCE: promote a place-class candidate over a point-scale
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

    # MINIMUM AOI FLOOR. Whatever candidate won above, a
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
        # Normalize to (min_lon, min_lat, max_lon, max_lat) -- the project
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
    """Translate a free-text place name into a bounding box and canonical name.

    Forward-geocodes to a WGS84 bbox, a centroid and the canonical name. The bbox
    is ALWAYS at least about 2 km on its long axis, so it is a usable AOI and
    never a bare building footprint.

    Use this when: a request names a place and a downstream tool needs a bbox.

    Do NOT use this for: reverse geocoding, routing or distance, or parcel-level
    address resolution. The bbox is the full administrative boundary of the named
    place, so a county or a state comes back very large -- narrow it before
    handing it to a heavy download.

    Params: ``query``, a non-empty free-text place name.

    Returns the canonical ``name``, a ``[min_lon, min_lat, max_lon, max_lat]``
    ``bbox``, a centroid and the source. ``fallback_reason`` appears when a vague
    query snapped to a whole state and ``expansion_note`` when a building-scale
    result was widened to the AOI floor - both are notes to narrate, not hide.
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
    # -- Tier separation (invariant 5): no gs:// URIs leak into model text.
    payload = json.loads(result.data.decode("utf-8"))

    # Sanity-check: if a state was detected but the primary result's centroid
    # lands OUTSIDE that state (with a tolerance margin), the match is wrong --
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
                "(centroid=%s,%s) -- snapping to state bbox",
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
    """Build the geocode dict for a state-snap: the SAME keys as the primary path, plus
    the additive ``fallback_reason`` note. The bbox prefers the live state admin
    boundary and falls back to the offline extent, never raising."""
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
