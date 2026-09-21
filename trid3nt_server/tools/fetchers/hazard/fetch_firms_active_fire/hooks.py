"""firms_active_fire hooks: the key a FIRMS area query is addressed by.

The one irreducible step is the request: FIRMS carries the MAP_KEY IN THE URL PATH, so
the plan resolves it and raises a credential-shaped missing-key error pre-network when
none does."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error

__all__ = ["build_request"]

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
_KEY_ENV = "TRID3NT_FIRMS_MAP_KEY"


def _resolve_map_key(sc: str, params: dict[str, Any]) -> str:
    """Resolve the MAP_KEY -- kwarg, then str secret_ref, then env -- raising a
    credential-shaped missing-key error BEFORE any network call when none does. The key
    never enters the cache key: the detections do not vary by it."""
    import os

    map_key = params.get("map_key")
    if map_key:
        return str(map_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        return secret_ref
    env_key = os.environ.get(_KEY_ENV)
    if env_key:
        return env_key
    raise router_input_error(
        sc,
        "no FIRMS MAP_KEY available. Add the key under Settings > Keys. Register a "
        "free key at https://firms.modaps.eosdis.nasa.gov/api/map_key/.",
        "MISSING_KEY",
    )


@_hooks.register_hook("firms_active_fire.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the key and build the AREA-endpoint CSV URL, which carries the key in its
    path, as one GET. A ``date`` names a single historical acquisition day and forces
    the day range to 1; omitted, the URL is the rolling window."""
    sc = spec.error_code_prefix
    key = _resolve_map_key(sc, params)  # raises MISSING_KEY pre-network when absent
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
