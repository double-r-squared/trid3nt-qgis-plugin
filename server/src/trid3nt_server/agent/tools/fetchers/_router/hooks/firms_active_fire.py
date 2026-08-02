"""firms_active_fire hooks (quick-folds wave, keyed CSV http_json, ADR 0079):
NASA FIRMS active-fire / thermal-anomaly detections, keyed by a MAP_KEY.

FIRMS carries the key IN THE URL PATH (not a header), so ``build_request`` resolves
the key (kwarg -> str secret_ref -> ``TRID3NT_FIRMS_MAP_KEY`` env), raising a
credential-shaped FIRMS_MISSING_KEY pre-network when none resolves (the ebird
precedent), and emits ONE GET of the AREA-endpoint CSV URL. The key wrinkle: FIRMS
signals a bad/rate-limited key via a 200-WITH-ERROR-BODY (not always a non-2xx), so
the auth split lives in BOTH ``parse_response`` (the 200-body envelope check IS the
doctrine) and ``classify_status`` (the same body markers on a non-2xx TransportError).
``parse_response`` decodes the CSV into Point features carrying the retained schema;
an empty (header-only) body is an honest 0-feature FGB, never a typed error.
"""

from __future__ import annotations

import csv
import io
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import RouterError, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "classify_status"]

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
_KEY_ENV = "TRID3NT_FIRMS_MAP_KEY"

#: Columns emitted in the FGB properties -- drops latitude/longitude (they ARE the
#: geometry). Only columns PRESENT in a given source's CSV are populated per-row
#: (VIIRS names its brightness bands bright_ti4/ti5, not brightness/bright_t31, so
#: those read null for a VIIRS source -- the twin dropped them entirely).
_RETAINED_COLUMNS = (
    "brightness", "scan", "track", "acq_date", "acq_time", "satellite",
    "instrument", "confidence", "version", "bright_t31", "frp", "daynight",
)


def _is_auth_body(body: str) -> str | None:
    """Return an auth-failure reason when the FIRMS body signals a bad/limited key.

    FIRMS returns a plain-text error body (sometimes under HTTP 200, sometimes a
    4xx): an unknown key -> ``Invalid MAP_KEY.``; a rate-limited key -> wording
    containing ``exceeded your transaction`` (or rate + limit). None otherwise.
    """
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
    """Resolve the FIRMS MAP_KEY: kwarg -> str secret_ref -> env; else MISSING_KEY.

    Raises a credential-shaped FIRMS_MISSING_KEY BEFORE any network call when no key
    resolves (the ebird parity surface). The env var is the local-dev / live-drive
    path and is NOT threaded through the cache key (the detections do not vary by key).
    """
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
    """Resolve the key, build the AREA-endpoint CSV URL (key in the path), one GET.

    A ``date`` (single historical acquisition day) forces the day-range to 1 -- the
    FIRMS trailing ``/{YYYY-MM-DD}`` idiom. The rolling URL is byte-identical when
    ``date`` is omitted.
    """
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
    """Decode the FIRMS CSV into Point features (200-body auth check first).

    A 200-with-error-body (bad / rate-limited key) raises a credential-shaped
    FIRMS_AUTH_ERROR. A blank body / a body missing the latitude/longitude columns
    is an upstream defect. A header-only body is a valid 0-feature result.
    """
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
    """Bad-key body on a non-2xx -> FIRMS_AUTH_ERROR; else the default upstream.

    FIRMS returns the ``Invalid MAP_KEY.`` body under HTTP 400 as well as 200, so the
    same body-marker split runs here on a transport failure. A non-auth non-2xx keeps
    the default retryable upstream mapping (the twin's FirmsUpstreamError).
    """
    sc = spec.error_code_prefix
    auth_reason = _is_auth_body(body or "")
    if auth_reason is not None:
        return router_input_error(sc, auth_reason, "AUTH_ERROR")
    return None
