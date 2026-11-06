"""US state identity: the codes, the FIPS numbers and the state envelopes.

One home for the state facts every US row asks for. Free-form state text resolves to
the 2-letter codes ``api.weather.gov`` accepts; a code resolves to its FIPS number;
and a bbox resolves to the states whose Census envelope it touches, for a source queried one state at a time."""

from __future__ import annotations

import re
from typing import Iterable

from .us_state_envelopes import STATE_ENVELOPES

__all__ = [
    "NWS_AREA_CODES",
    "STATE_CODE_TO_FIPS",
    "FIPS_TO_STATE_CODE",
    "states_intersecting_bbox",
    "STATE_NAME_TO_CODE",
    "STATE_CODE_TO_NAME",
    "resolve_state_code",
    "state_display_name",
]



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

FIPS_TO_STATE_CODE: dict[str, str] = {v: k for k, v in STATE_CODE_TO_FIPS.items()}


def states_intersecting_bbox(bbox: tuple[float, float, float, float],
                             among: "Iterable[str] | None" = None) -> list[str]:
    """The 2-letter codes whose Census envelope the bbox touches, sorted; empty means outside the US.

    ``among`` keeps only the codes a source serves. An envelope across the antimeridian wraps."""
    west, south, east, north = (float(v) for v in bbox)
    served = None if among is None else set(among)
    codes = []
    for code, (s_w, s_s, s_e, s_n) in STATE_ENVELOPES.items():
        if served is not None and code not in served:
            continue
        across = (east >= s_w or west <= s_e) if s_w > s_e else (west <= s_e and east >= s_w)
        if across and south <= s_n and north >= s_s:
            codes.append(code)
    return sorted(codes)

#: Area codes accepted by api.weather.gov alerts (?area=): states, DC, territories and marine zones.
NWS_AREA_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
    "AS", "GU", "MP", "PR", "VI",
    "PZ", "PK", "PH", "PS", "PM", "AN", "AM", "GM", "LS", "LM", "LH", "LC",
    "LE", "LO",
})


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
    "american samoa": "AS", "guam": "GU",
    "northern mariana islands": "MP", "puerto rico": "PR",
    "virgin islands": "VI", "us virgin islands": "VI",
    "u.s. virgin islands": "VI", "washington dc": "DC",
    "washington d.c.": "DC",
}

#: Reverse mapping for display labels; a code with several names keeps the first, marine zones have none.
STATE_CODE_TO_NAME: dict[str, str] = {}
for _name, _code in STATE_NAME_TO_CODE.items():
    STATE_CODE_TO_NAME.setdefault(_code, _name.title())
STATE_CODE_TO_NAME["DC"] = "District of Columbia"


_LEADING_NOISE = re.compile(r"^(?:the\s+)?(?:state\s+of\s+)?", re.IGNORECASE)


def resolve_state_code(text: str) -> str | None:
    """Resolve free-form state text to a 2-letter NWS area code, or ``None`` (case-insensitive, tolerant of "state of ")."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    if len(s) == 2 and s.upper() in NWS_AREA_CODES:
        return s.upper()
    s = _LEADING_NOISE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return STATE_NAME_TO_CODE.get(s)


def state_display_name(code: str) -> str:
    """Human-readable label for a 2-letter area code; marine-zone codes echo the code."""
    return STATE_CODE_TO_NAME.get(code.upper(), code.upper())
