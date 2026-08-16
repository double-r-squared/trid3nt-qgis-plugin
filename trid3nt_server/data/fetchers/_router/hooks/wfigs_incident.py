"""wfigs_incident record hooks: NIFC/WFIGS named-incident lookup.

The proof-by-migration for the record-return output shape. The source resolves a
NAMED wildland-fire incident to an authoritative point + padded AOI bbox + discovery
record -- a bare structured JSON dict, NOT a renderable map layer. The router owns the
transport + cache; these PURE hooks own the bespoke resolution the declarative surface
cannot carry:

- ``build_request`` -- the token-OR ``UPPER(IncidentName) LIKE`` query builder + the
  ordered 2-endpoint plan set (the live "Current" active feed first, then the
  "YearToDate" all-incidents sibling that also carries recently-contained fires) +
  the bespoke state-code + pad-degree input validation.
- ``record`` -- best-feature-by-size selection over ONE feed's features (returning
  None to signal "no usable feature in this feed, try the next endpoint" -- the
  record executor walks the plans in order and stops at the first non-None dict,
  reproducing the twin's Current->YearToDate short-circuit) + the authoritative
  point + bbox-from-point + epoch->ISO discovery record.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from . import RequestPlan, register_hook

logger = logging.getLogger(
    "trid3nt_server.data.fetchers._router.hooks.wfigs_incident"
)

_OUT_FIELDS = (
    "IncidentName", "FireDiscoveryDateTime", "InitialLatitude", "InitialLongitude",
    "IncidentSize", "PercentContained", "POOState", "POOCounty", "IrwinID",
    "UniqueFireIdentifier",
)

#: NIFC asks automated clients to identify themselves.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Noise tokens dropped before the loose token-OR match; short fragments too.
_NAME_STOP_TOKENS = frozenset({"FIRE", "THE", "OF", "AND", "COMPLEX"})
_MIN_TOKEN_LEN = 3
_DEFAULT_BBOX_PAD_DEG = 0.25


def _normalize_state(sc: str, state: str | None) -> str | None:
    """Normalize a state arg to WFIGS ``US-XX``; raise typed INPUT on a malformed code."""
    if not state or not str(state).strip():
        return None
    s = str(state).strip().upper().replace("_", "-")
    body = s[3:] if s.startswith("US-") else s
    if len(body) != 2 or not body.isalpha():
        raise router_input_error(
            sc, f"state={state!r} is not a 2-letter US state code (e.g. 'UT' or 'US-UT')",
            "INPUT_INVALID",
        )
    return f"US-{body}"


def _significant_name_tokens(name: str) -> list[str]:
    """UPPER significant tokens for the loose token-OR match (drops noise + fragments)."""
    toks = []
    for raw in (name or "").upper().replace("/", " ").split():
        t = raw.strip("'\"().,;:")
        if len(t) >= _MIN_TOKEN_LEN and t not in _NAME_STOP_TOKENS:
            toks.append(t)
    return toks


def _build_wfigs_query(incident_name: str, state_norm: str | None) -> dict[str, str]:
    """Build the WFIGS FeatureServer query params (case-insensitive token-OR LIKE)."""
    name = (incident_name or "").strip()
    if name.upper().endswith(" FIRE"):
        name = name[: -len(" FIRE")].strip()
    safe = name.upper().replace("'", "''")
    whole = f"UPPER(IncidentName) LIKE '%{safe}%'"
    tokens = _significant_name_tokens(name)
    if len(tokens) >= 2:
        clauses = [whole]
        for tok in tokens:
            clauses.append(f"UPPER(IncidentName) LIKE '%{tok.replace(chr(39), chr(39) * 2)}%'")
        where = "(" + " OR ".join(clauses) + ")"
    else:
        where = whole
    if state_norm:
        where = f"({where}) AND POOState = '{state_norm}'"
    return {
        "where": where,
        "outFields": ",".join(_OUT_FIELDS),
        "outSR": "4326",
        "returnGeometry": "true",
        "f": "json",
    }


def _is_finite_lonlat(lon: float, lat: float) -> bool:
    return (
        math.isfinite(lon) and math.isfinite(lat)
        and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0
        and not (lon == 0.0 and lat == 0.0)
    )


def _feature_point(feature: dict[str, Any]) -> tuple[float, float] | None:
    """(lon, lat) preferring the InitialLongitude/Latitude fields, else the geometry."""
    attrs = feature.get("attributes") or {}
    for lon, lat in (
        (attrs.get("InitialLongitude"), attrs.get("InitialLatitude")),
        ((feature.get("geometry") or {}).get("x"), (feature.get("geometry") or {}).get("y")),
    ):
        try:
            if lon is not None and lat is not None:
                flon, flat = float(lon), float(lat)
                if _is_finite_lonlat(flon, flat):
                    return flon, flat
        except (TypeError, ValueError):
            pass
    return None


def _select_best_feature(features: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Largest IncidentSize (acres), tie-break most-recent discovery; None if no point."""
    usable = [f for f in features if _feature_point(f) is not None]
    if not usable:
        return None

    def _num(f: dict[str, Any], key: str) -> float:
        v = (f.get("attributes") or {}).get(key)
        try:
            return float(v) if v is not None else -1.0
        except (TypeError, ValueError):
            return -1.0

    usable.sort(key=lambda f: (_num(f, "IncidentSize"), _num(f, "FireDiscoveryDateTime")), reverse=True)
    return usable[0]


def _bbox_from_point(lon: float, lat: float, pad_deg: float) -> tuple[float, float, float, float]:
    """A degree bbox padded around a point; E-W widened by 1/cos(lat) (square on ground)."""
    pad = max(0.01, float(pad_deg))
    cos_lat = math.cos(math.radians(max(-89.0, min(89.0, lat))))
    ew_pad = pad / max(0.2, cos_lat)
    return (
        round(max(-180.0, lon - ew_pad), 6),
        round(max(-90.0, lat - pad), 6),
        round(min(180.0, lon + ew_pad), 6),
        round(min(90.0, lat + pad), 6),
    )


def _epoch_ms_to_iso(value: Any) -> str | None:
    """ArcGIS epoch-milliseconds -> ISO-8601 UTC; None for a missing/non-numeric value."""
    if value is None:
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


@register_hook("wfigs_incident.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """Build the ordered 2-endpoint WFIGS query plans + the bespoke state/pad validation."""
    sc = spec.error_code_prefix
    incident_name = str(params.get("incident_name") or "").strip()
    if not incident_name:
        raise router_input_error(sc, "incident_name must be a non-empty string", "INPUT_INVALID")
    try:
        pad = float(params.get("bbox_pad_deg", _DEFAULT_BBOX_PAD_DEG))
    except (TypeError, ValueError):
        raise router_input_error(sc, f"bbox_pad_deg must be a number; got {params.get('bbox_pad_deg')!r}", "INPUT_INVALID")
    if not (0.0 < pad <= 10.0):
        raise router_input_error(sc, f"bbox_pad_deg must be in (0, 10]; got {pad!r}", "INPUT_INVALID")
    state_norm = _normalize_state(sc, params.get("state"))
    query = _build_wfigs_query(incident_name, state_norm)
    endpoints = spec.endpoints
    return [
        RequestPlan(url=endpoints["current"].url, params=query, headers={"User-Agent": _USER_AGENT}),
        RequestPlan(url=endpoints["year_to_date"].url, params=query, headers={"User-Agent": _USER_AGENT}),
    ]


@register_hook("wfigs_incident.record")
def record(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any] | None:
    """Best-feature discovery record over ONE feed; None to advance to the next endpoint."""
    sc = spec.error_code_prefix
    body = bodies[0] if bodies else b""
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"WFIGS returned non-JSON: {exc}")
    if not isinstance(parsed, dict):
        raise router_upstream_error(sc, f"WFIGS response is not a JSON object: {type(parsed).__name__}")
    if "error" in parsed:
        raise router_upstream_error(sc, f"WFIGS query returned error envelope: {parsed['error']}")
    features = parsed.get("features") or []
    best = _select_best_feature(features) if isinstance(features, list) else None
    if best is None:
        return None
    point = _feature_point(best)
    assert point is not None  # _select_best_feature guarantees a usable point
    lon, lat = point
    pad = float(params.get("bbox_pad_deg", _DEFAULT_BBOX_PAD_DEG))
    attrs = best.get("attributes") or {}
    incident_name = str(params.get("incident_name") or "").strip()
    return {
        "incident_name": attrs.get("IncidentName") or incident_name,
        "lat": lat,
        "lon": lon,
        "bbox": list(_bbox_from_point(lon, lat, pad)),
        "fire_discovery_datetime": _epoch_ms_to_iso(attrs.get("FireDiscoveryDateTime")),
        "incident_size_acres": attrs.get("IncidentSize"),
        "percent_contained": attrs.get("PercentContained"),
        "poo_state": attrs.get("POOState"),
        "poo_county": attrs.get("POOCounty"),
        "irwin_id": attrs.get("IrwinID"),
        "unique_fire_identifier": attrs.get("UniqueFireIdentifier"),
    }
