"""nws_event hooks: the area a US alert query is asked by.

The one irreducible step is the request: the ``area`` a person names -- a state name,
a 2-letter state or marine-zone code, or a 5-digit county FIPS -- canonicalizes to the
one value the query accepts, and the event-type filter repeats a query key."""

# ``area`` is a string only: the tool schema collapses a union to str, so a bbox tuple
# could never reach here anyway.

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error
from ...us_states import NWS_AREA_CODES, resolve_state_code

__all__ = ["build_request"]

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

#: 5-digit county FIPS pattern (NWS ``?area=`` accepts a FIPS the same as a
#: state code via zone lookup).
_FIPS_PATTERN = re.compile(r"^\d{5}$")


def _canonicalize_area(sc: str, sfx: str, area: Any) -> str:
    """Reduce the ``area`` string to the query's own value: a 2-letter state or
    marine-zone code, a 5-digit county FIPS, or a full state name resolved to its code.
    An unrecognized value is a source-stamped input error."""
    s = str(area).strip().upper()
    if _FIPS_PATTERN.match(s):
        return s
    if s in NWS_AREA_CODES:
        return s
    name_code = resolve_state_code(str(area))
    if name_code is not None:
        return name_code
    raise router_input_error(
        sc,
        f"area={area!r} is not a recognized US state name, 2-letter state code, "
        f"or 5-digit county FIPS",
        sfx,
    )


@_hooks.register_hook("nws_event.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Canonicalize the area and build the /alerts/active query URL (single GET)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    area_value = _canonicalize_area(sc, sfx, params.get("area"))
    status = params.get("status") or "actual"
    message_type = params.get("message_type") or "alert"
    event_types = params.get("event_types") or []

    query: list[tuple[str, str]] = [
        ("area", area_value),
        ("status", status),
        ("message_type", message_type),
    ]
    for et in event_types:
        query.append(("event", et))
    url = NWS_ALERTS_URL + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return [
        _hooks.RequestPlan(
            url=url,
            headers={"User-Agent": spec.auth.user_agent, "Accept": "application/geo+json"},
        )
    ]
