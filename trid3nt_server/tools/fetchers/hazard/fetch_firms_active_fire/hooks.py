"""firms_active_fire hooks: NASA FIRMS active-fire detections, keyed by MAP_KEY.

FIRMS carries the key IN THE URL PATH, so ``build_request`` resolves it and raises a
credential-shaped missing-key error pre-network when none does. An empty header-only
CSV body is an honest 0-feature FGB, never a typed error."""

# FIRMS signals a bad or rate-limited key with a 200-WITH-ERROR-BODY as often as with
# a non-2xx, so the auth split lives in BOTH hooks: ``parse_response`` checks the
# 200-body envelope, and ``classify_status`` checks the same markers on a transport
# error's body.

# FIRMS signals a bad or rate-limited key with a 200-WITH-ERROR-BODY as often as with
# a non-2xx, so the auth split lives in BOTH hooks: ``parse_response`` checks the
# 200-body envelope, and ``classify_status`` checks the same markers on a transport
# error's body.

from __future__ import annotations

import csv
import io
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import RouterError, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "classify_status"]

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
_KEY_ENV = "TRID3NT_FIRMS_MAP_KEY"

#: Columns emitted in the FGB properties -- drops latitude/longitude (they ARE the
#: geometry). Only columns PRESENT in a given source's CSV are populated per-row
#: (VIIRS names its brightness bands bright_ti4/ti5, not brightness/bright_t31, so
#: those read null for a VIIRS source).
_RETAINED_COLUMNS = (
    "brightness", "scan", "track", "acq_date", "acq_time", "satellite",
    "instrument", "confidence", "version", "bright_t31", "frp", "daynight",
)


def _is_auth_body(body: str) -> str | None:
    """The auth-failure reason when the body signals a bad or rate-limited key, else
    None. The plain-text error body arrives under HTTP 200 as often as a 4xx: an
    unknown key names the key, a limited one names the exceeded transaction."""
    low = body.strip().lower()
    if "invalid map_key" in low:
        return (
            "FIRMS rejected the MAP_KEY. Set TRID3NT_FIRMS_MAP_KEY to a valid key "
            "from https://firms.modaps.eosdis.nasa.gov/api/map_key/."
        )
    if "exceeded your transaction" in low or ("rate" in low and "limit" in low):
        return f"FIRMS reports rate-limit exhaustion for the MAP_KEY: {body[:200]}"
    return None


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
        "no FIRMS MAP_KEY available: pass map_key=..., secret_ref=..., or set the "
        "TRID3NT_FIRMS_MAP_KEY env var. Register a free key at "
        "https://firms.modaps.eosdis.nasa.gov/api/map_key/.",
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


@_hooks.register_hook("firms_active_fire.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode the CSV into Point features, checking the 200 body for an auth failure
    first. A blank body, or one missing the coordinate columns, is an upstream defect;
    a header-only body is a valid 0-feature result."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"FIRMS body not UTF-8: {exc}")

    auth_reason = _is_auth_body(body)
    if auth_reason is not None:
        raise router_input_error(sc, auth_reason, "AUTH_ERROR")
    if not body.strip():
        raise router_upstream_error(sc, "FIRMS returned an empty response body")

    reader = csv.DictReader(io.StringIO(body))
    cols = set(reader.fieldnames or [])
    missing = {"latitude", "longitude"} - cols
    if missing:
        raise router_upstream_error(
            sc,
            f"FIRMS CSV missing required columns {sorted(missing)}; "
            f"got columns={sorted(cols)}",
        )

    feats: list[dict[str, Any]] = []
    for row in reader:
        lat_raw, lon_raw = row.get("latitude"), row.get("longitude")
        if lat_raw in (None, "") or lon_raw in (None, ""):
            continue
        try:
            lat, lon = float(lat_raw), float(lon_raw)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        props = {c: row.get(c) for c in _RETAINED_COLUMNS if c in row}
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return feats


@_hooks.register_hook("firms_active_fire.classify_status")
def classify_status(
    spec: SourceSpec, status: int | None, body: str | None
) -> RouterError | None:
    """A bad-key body on a non-2xx raises the auth error; anything else keeps the default
    retryable upstream mapping. The same body markers appear under a 400 as under a
    200, so the split runs on a transport failure too."""
    sc = spec.error_code_prefix
    auth_reason = _is_auth_body(body or "")
    if auth_reason is not None:
        return router_input_error(sc, auth_reason, "AUTH_ERROR")
    return None
