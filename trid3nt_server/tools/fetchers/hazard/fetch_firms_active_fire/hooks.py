"""firms_active_fire hooks: the key a FIRMS area query is addressed by.

The one irreducible step is the request: FIRMS carries the MAP_KEY IN THE URL PATH, so
the plan reads it from the row's credential and asks for it pre-network when none is stored."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.model.credentials.resolver import credential_value

from ..._router import hooks as _hooks

__all__ = ["build_request"]

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


@_hooks.register_hook("firms_active_fire.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Read the key and build the AREA-endpoint CSV URL, which carries the key in its
    path, as one GET. A ``date`` names a single historical acquisition day and forces
    the day range to 1; omitted, the URL is the rolling window."""
    key = credential_value(spec)
    source = params["source"]
    bbox = params["bbox"]
    date = params.get("date")
    days_back = 1 if date else int(params.get("days_back", 1))
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    url = f"{_FIRMS_BASE}/{key}/{source}/{bbox_str}/{days_back}"
    if date:
        url = f"{url}/{date}"
    headers = {"User-Agent": spec.auth.user_agent}
    return [_hooks.RequestPlan(url=url, headers=headers)]
