"""ebird_observations hooks (keyed http_json multi-URL + classify_status/0071):
Cornell Lab eBird recent-observations, keyed.

eBird exposes only radius queries around a ``(lat,lng)`` point (no bbox), so ``build_request``
tiles the bbox into overlapping 50 km circles and emits ONE GET per tile center (the http_json
default multi-GET fan-out); ``parse_response`` dedups by ``subId`` across all tiles, re-clips to
the exact bbox (the geographic-correctness gate), and emits the 7-column point schema (empty ->
an honest header-only FGB, never a typed EMPTY). The key resolves kwarg -> str secret_ref ->
``TRID3NT_EBIRD_API_KEY`` env, raising a credential-shaped EBIRD_MISSING_KEY pre-network when
absent. ``classify_status`` splits the HTTP status the transport would otherwise collapse:
401/403 -> credential-shaped EBIRD_AUTH_ERROR, 4xx (incl. 404 unknown species) -> EBIRD_INPUT_ERROR,
5xx -> the default retryable upstream. The key is NEVER registered; the parity surface is the
key-absent + input + status-split typed errors.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import RouterError, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "classify_status"]

_URL_TMPL = "https://api.ebird.org/v2/data/obs/geo/recent/{species_code}"
_TILE_RADIUS_KM = 50.0
_MAX_TILES_HARD_CAP = 200
_KEY_ENV = "TRID3NT_EBIRD_API_KEY"
_COLUMNS = ("subId", "obsDt", "locName", "howMany", "comName", "sciName", "speciesCode")


def _resolve_key(sc: str, params: dict[str, Any]) -> str:
    api_key = params.get("api_key")
    if api_key:
        return str(api_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        return secret_ref
    env_key = os.environ.get(_KEY_ENV)
    if env_key:
        return env_key
    raise router_input_error(
        sc, "no eBird API key available: pass api_key=..., secret_ref=..., or set the "
            "TRID3NT_EBIRD_API_KEY env var. Register at https://ebird.org/api/keygen.",
        "MISSING_KEY")


def _validate_species(sc: str, species_code: Any) -> str:
    if not isinstance(species_code, str):
        raise router_input_error(sc, f"species_code must be a str; got {type(species_code).__name__}")
    s = species_code.strip()
    if not s:
        raise router_input_error(sc, "species_code must be non-empty")
    if len(s) > 16:
        raise router_input_error(sc, f"species_code too long (max 16 chars); got {s!r}")
    if not all(c.isalnum() for c in s):
        raise router_input_error(sc, f"species_code must be alphanumeric; got {s!r}")
    return s.lower()


def _tile_centers(sc: str, bbox: list[float]) -> list[tuple[float, float]]:
    west, south, east, north = (float(v) for v in bbox)
    center_lat = 0.5 * (south + north)
    step_lat = _TILE_RADIUS_KM * (1.0 / 110.574)
    step_lon = _TILE_RADIUS_KM * (1.0 / (111.320 * max(0.01, math.cos(math.radians(center_lat)))))
    n_rows = max(1, math.ceil((north - south) / step_lat))
    n_cols = max(1, math.ceil((east - west) / step_lon))
    if n_rows * n_cols > _MAX_TILES_HARD_CAP:
        raise router_input_error(
            sc, f"bbox tile cover would require {n_rows * n_cols} tiles "
                f"(max {_MAX_TILES_HARD_CAP}); request a smaller bbox")
    centers: list[tuple[float, float]] = []
    for r in range(n_rows):
        lat = 0.5 * (south + north) if n_rows == 1 else south + (r + 0.5) * (north - south) / n_rows
        for c in range(n_cols):
            lng = 0.5 * (west + east) if n_cols == 1 else west + (c + 0.5) * (east - west) / n_cols
            centers.append((lat, lng))
    return centers


@_hooks.register_hook("ebird_observations.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate species/key, tile the bbox, and emit one GET per 50 km circle center."""
    sc = spec.error_code_prefix
    species = _validate_species(sc, params.get("species_code"))
    days_back = int(params.get("days_back", 30))
    api_key = _resolve_key(sc, params)  # raises MISSING_KEY pre-network when absent
    url = _URL_TMPL.format(species_code=species)
    headers = {"X-eBirdApiToken": api_key, "User-Agent": spec.auth.user_agent,
               "Accept": "application/json"}
    plans: list["_hooks.RequestPlan"] = []
    for lat, lng in _tile_centers(sc, params["bbox"]):
        q = {"lat": f"{lat:.6f}", "lng": f"{lng:.6f}", "dist": int(_TILE_RADIUS_KM),
             "back": days_back, "fmt": "json"}
        plans.append(_hooks.RequestPlan(url=url, params=q, headers=headers))
    return plans


@_hooks.register_hook("ebird_observations.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Dedup by subId across tiles, re-clip to the bbox, emit the 7-column point schema."""
    sc = spec.error_code_prefix
    species = str(params.get("species_code") or "").strip().lower()
    west, south, east, north = (float(v) for v in params["bbox"])
    seen: set[str] = set()
    feats: list[dict[str, Any]] = []
    for raw in bodies:
        if not raw:
            payload: Any = []
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise router_upstream_error(sc, f"eBird returned non-JSON: {exc}")
        if not isinstance(payload, list):
            raise router_upstream_error(sc, f"eBird payload is not a list; got {type(payload).__name__}")
        for rec in payload:
            if not isinstance(rec, dict):
                continue
            sub_id = rec.get("subId")
            if not sub_id or not isinstance(sub_id, str):
                sub_id = f"_anon::{rec.get('lat')}::{rec.get('lng')}::{rec.get('obsDt')}"
            if sub_id in seen:
                continue
            seen.add(sub_id)
            lng, lat = rec.get("lng"), rec.get("lat")
            if lng is None or lat is None:
                continue
            try:
                lng_f, lat_f = float(lng), float(lat)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lng_f) and math.isfinite(lat_f)):
                continue
            if not (west <= lng_f <= east and south <= lat_f <= north):
                continue
            how_raw = rec.get("howMany")
            try:
                how = int(how_raw) if how_raw is not None else None
            except (TypeError, ValueError):
                how = None
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng_f, lat_f]},
                "properties": {
                    "subId": rec.get("subId") or "", "obsDt": rec.get("obsDt") or "",
                    "locName": rec.get("locName") or "", "howMany": how,
                    "comName": rec.get("comName") or "", "sciName": rec.get("sciName") or "",
                    "speciesCode": rec.get("speciesCode") or species,
                },
            })
    return feats


@_hooks.register_hook("ebird_observations.classify_status")
def classify_status(spec: SourceSpec, status: int | None, body: str | None) -> RouterError | None:
    """401/403 -> credential-shaped AUTH_ERROR; 4xx (incl. 404 unknown species) -> INPUT_ERROR; else default upstream."""
    sc = spec.error_code_prefix
    if status in (401, 403):
        return router_input_error(sc, f"eBird API rejected the key (status {status}): {(body or '')[:200]}", "AUTH_ERROR")
    if status is not None and 400 <= status < 500:
        return router_input_error(sc, f"eBird API returned {status}: {(body or '')[:200]}", "INPUT_ERROR")
    return None
