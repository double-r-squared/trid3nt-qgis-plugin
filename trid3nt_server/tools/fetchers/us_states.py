"""US state identity: the codes, the FIPS numbers and the state envelopes.

One home for the state facts every US row asks for. Free-form state text resolves to
the 2-letter codes ``api.weather.gov`` accepts; a code resolves to its FIPS number;
and a bbox resolves to the states it touches, for a source queried one state at a time."""

from __future__ import annotations

import re

__all__ = [
    "NWS_AREA_CODES",
    "STATE_CODE_TO_FIPS",
    "FIPS_TO_STATE_CODE",
    "STATE_FIPS_BBOXES",
    "states_intersecting_bbox",
    "STATE_NAME_TO_CODE",
    "STATE_CODE_TO_NAME",
    "resolve_state_code",
    "state_display_name",
]



#: 2-letter USPS code -> 2-digit FIPS state code (50 states + DC + 5 territories).
STATE_CODE_TO_FIPS: dict[str, str] = {
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
#: derivation (50 states + DC + PR + VI; generous ~10km border buffers). It is a
#: SELECTOR, never a boundary: the real polygons come from the census row.
STATE_FIPS_BBOXES: dict[str, tuple[float, float, float, float]] = {
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

FIPS_TO_STATE_CODE: dict[str, str] = {v: k for k, v in STATE_CODE_TO_FIPS.items()}


def states_intersecting_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """The 2-letter codes whose envelope the bbox touches, sorted. Empty means the box
    is outside the US, which a US-only row reports as the refusal it is."""
    west, south, east, north = (float(v) for v in bbox)
    codes = [
        FIPS_TO_STATE_CODE[fips]
        for fips, (s_w, s_s, s_e, s_n) in STATE_FIPS_BBOXES.items()
        if west <= s_e and east >= s_w and south <= s_n and north >= s_s
        and fips in FIPS_TO_STATE_CODE
    ]
    return sorted(codes)

#: Full set of 2-letter area codes accepted by api.weather.gov/alerts/active
#: (?area=): 50 states + DC + 5 territories + marine zones.
NWS_AREA_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
    # Territories
    "AS", "GU", "MP", "PR", "VI",
    # Marine zones
    "PZ", "PK", "PH", "PS", "PM", "AN", "AM", "GM", "LS", "LM", "LH", "LC",
    "LE", "LO",
})


#: Full state/territory names (lowercase, single-spaced) -> 2-letter code.
STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # Territories
    "american samoa": "AS", "guam": "GU",
    "northern mariana islands": "MP", "puerto rico": "PR",
    "virgin islands": "VI", "us virgin islands": "VI",
    "u.s. virgin islands": "VI", "washington dc": "DC",
    "washington d.c.": "DC",
}

#: Reverse mapping for display labels (codes with multiple names keep the
#: first/canonical entry; marine zones have no entry).
STATE_CODE_TO_NAME: dict[str, str] = {}
for _name, _code in STATE_NAME_TO_CODE.items():
    STATE_CODE_TO_NAME.setdefault(_code, _name.title())
STATE_CODE_TO_NAME["DC"] = "District of Columbia"


_LEADING_NOISE = re.compile(r"^(?:the\s+)?(?:state\s+of\s+)?", re.IGNORECASE)


def resolve_state_code(text: str) -> str | None:
    """Resolve free-form state text to a 2-letter NWS area code, or ``None``.
    Case-insensitive, whitespace-tolerant and tolerant of a leading "state of ";
    anything unrecognized returns ``None`` rather than raising."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    # 2-letter code fast path.
    if len(s) == 2 and s.upper() in NWS_AREA_CODES:
        return s.upper()
    # Full-name path: strip noise prefix, collapse whitespace, lowercase.
    s = _LEADING_NOISE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return STATE_NAME_TO_CODE.get(s)


def state_display_name(code: str) -> str:
    """Human-readable label for a 2-letter area code ("TX" -> "Texas").

    Marine-zone codes have no name mapping and echo the code itself."""
    return STATE_CODE_TO_NAME.get(code.upper(), code.upper())
