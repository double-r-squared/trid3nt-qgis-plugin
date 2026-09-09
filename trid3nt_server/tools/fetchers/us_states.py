"""US state / NWS area-code resolution for the NWS alert tools.

Free-form state text resolves to the 2-letter codes ``api.weather.gov`` accepts:
50 states, DC and 5 territories, by name or code. A marine-zone code passes through
unmapped; anything else resolves to ``None`` for the caller to classify."""

from __future__ import annotations

import re

__all__ = [
    "NWS_AREA_CODES",
    "STATE_NAME_TO_CODE",
    "STATE_CODE_TO_NAME",
    "resolve_state_code",
    "state_display_name",
]


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


#: Full state/territory names (lowercase, single-spaced) → 2-letter code.
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
