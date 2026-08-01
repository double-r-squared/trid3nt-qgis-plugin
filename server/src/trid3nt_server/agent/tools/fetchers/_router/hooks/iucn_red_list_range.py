"""iucn_red_list_range hooks (keyed http_json single-GET + classify_status, ADR 0065/0071):
IUCN Red List species assessment, keyed.

``build_request`` validates the species name + region, resolves the key (kwarg -> str secret_ref
-> ``TRID3NT_IUCN_RED_LIST_API_KEY`` env), and builds the region-select GET (``/species/{name}``
for global, else ``/species/region/{name}/{region}``) with the token as a query param; a missing
key raises a credential-shaped IUCN_AUTH_ERROR pre-network (the twin uses AUTH_ERROR for both the
absent AND the rejected key -- there is no separate MISSING_KEY). ``parse_response`` builds ONE
feature (the real assessment, or a ``category="DD"`` data-deficient placeholder when IUCN returns
an empty ``result``) on a placeholder square polygon at (0,0), and raises IUCN_AUTH_ERROR on the
200-OK ``{"message": "...token..."}`` rejection envelope. ``classify_status`` splits 401/403 ->
IUCN_AUTH_ERROR, other 4xx -> IUCN_INPUT_INVALID, 5xx -> the default retryable upstream.
"""

from __future__ import annotations

import json
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import RouterError, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "classify_status"]

_IUCN_BASE = "https://apiv3.iucnredlist.org/api/v3"
_KEY_ENV = "TRID3NT_IUCN_RED_LIST_API_KEY"
_MAX_NAME_LEN = 200
_PLACEHOLDER_HALF = 1.0
_COLUMNS = (
    "taxonid", "scientific_name", "common_name", "kingdom", "phylum", "class_name",
    "order_name", "family", "category", "criteria", "population_trend", "marine_system",
    "freshwater_system", "terrestrial_system", "elevation_lower", "elevation_upper",
    "depth_lower", "depth_upper", "published_year", "assessment_date", "region",
    "is_placeholder_geometry",
)


def _resolve_key(sc: str, params: dict[str, Any]) -> str:
    api_key = params.get("api_key")
    if api_key:
        return str(api_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref.strip():
        return secret_ref.strip()
    env_key = os.environ.get(_KEY_ENV)
    if env_key and env_key.strip():
        return env_key.strip()
    raise router_input_error(
        sc, f"no IUCN Red List API key resolved; pass api_key=, secret_ref=, or set ${_KEY_ENV}",
        "AUTH_ERROR")


def _validate_name(sc: str, name: Any) -> str:
    if not isinstance(name, str):
        raise router_input_error(sc, f"species_name must be str; got {type(name).__name__}", "INPUT_INVALID")
    s = name.strip()
    if not s:
        raise router_input_error(sc, "species_name must be a non-empty string", "INPUT_INVALID")
    if len(s) > _MAX_NAME_LEN:
        raise router_input_error(sc, f"species_name exceeds maximum length {_MAX_NAME_LEN}: got {len(s)} chars", "INPUT_INVALID")
    return " ".join(s.split())


def _validate_region(sc: str, region: Any) -> str:
    if not isinstance(region, str):
        raise router_input_error(sc, f"region must be str; got {type(region).__name__}", "INPUT_INVALID")
    s = region.strip()
    if not s:
        raise router_input_error(sc, "region must be a non-empty string", "INPUT_INVALID")
    if not all(c.isalnum() or c in "-_" for c in s):
        raise router_input_error(sc, f"region contains illegal characters; expected [a-z0-9_-]+, got {s!r}", "INPUT_INVALID")
    return s.lower()


@_hooks.register_hook("iucn_red_list_range.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Region-select GET with the token as a query param; MISSING key -> IUCN_AUTH_ERROR pre-network."""
    sc = spec.error_code_prefix
    name = _validate_name(sc, params.get("species_name"))
    region = _validate_region(sc, params.get("region", "global"))
    api_key = _resolve_key(sc, params)
    path = f"/species/{name}" if region == "global" else f"/species/region/{name}/{region}"
    return [_hooks.RequestPlan(
        url=_IUCN_BASE + path, params={"token": api_key},
        headers={"User-Agent": spec.auth.user_agent})]


def _placeholder_geometry() -> dict[str, Any]:
    h = _PLACEHOLDER_HALF
    return {"type": "Polygon", "coordinates": [[[-h, -h], [h, -h], [h, h], [-h, h], [-h, -h]]]}


@_hooks.register_hook("iucn_red_list_range.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Build one assessment (or DD placeholder) feature; 200-OK token-reject envelope -> AUTH_ERROR."""
    sc = spec.error_code_prefix
    region = str(params.get("region") or "global").strip().lower()
    species_name = " ".join(str(params.get("species_name") or "").split())
    raw = bodies[0] if bodies else b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"IUCN returned non-JSON: {exc}")
    if not isinstance(payload, dict):
        raise router_upstream_error(sc, f"IUCN payload is not an object: {type(payload).__name__}")
    if "message" in payload and "result" not in payload:
        msg = str(payload.get("message", ""))
        if "token" in msg.lower():
            raise router_input_error(sc, f"IUCN signaled token rejection in body: {msg!r}", "AUTH_ERROR")
        raise router_upstream_error(sc, f"IUCN returned an unexpected message-only payload: {msg!r}")

    result = payload.get("result") or []
    if not isinstance(result, list):
        raise router_upstream_error(sc, f"IUCN 'result' is not a list: {type(result).__name__}")
    if result:
        rec = result[0] if isinstance(result[0], dict) else {}
        found = True
    else:
        rec = {}
        found = False

    def _g(k: str, default: Any = None) -> Any:
        v = rec.get(k)
        return v if v is not None else default

    def _f(k: str) -> float | None:
        v = rec.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    row = {
        "taxonid": _g("taxonid"), "scientific_name": _g("scientific_name", species_name),
        "common_name": _g("main_common_name", ""), "kingdom": _g("kingdom", ""),
        "phylum": _g("phylum", ""), "class_name": _g("class", ""),
        "order_name": _g("order_name", ""), "family": _g("family", ""),
        "category": _g("category", "DD" if not found else ""), "criteria": _g("criteria", ""),
        "population_trend": _g("population_trend", ""),
        "marine_system": bool(_g("marine_system", False)),
        "freshwater_system": bool(_g("freshwater_system", False)),
        "terrestrial_system": bool(_g("terrestrial_system", False)),
        "elevation_lower": _f("elevation_lower"), "elevation_upper": _f("elevation_upper"),
        "depth_lower": _f("depth_lower"), "depth_upper": _f("depth_upper"),
        "published_year": _g("published_year"), "assessment_date": _g("assessment_date", ""),
        "region": region, "is_placeholder_geometry": True,
    }
    return [{"type": "Feature", "geometry": _placeholder_geometry(),
             "properties": {c: row.get(c) for c in _COLUMNS}}]


@_hooks.register_hook("iucn_red_list_range.classify_status")
def classify_status(spec: SourceSpec, status: int | None, body: str | None) -> RouterError | None:
    """401/403 -> credential-shaped IUCN_AUTH_ERROR; other 4xx -> IUCN_INPUT_INVALID; else default upstream."""
    sc = spec.error_code_prefix
    if status in (401, 403):
        return router_input_error(sc, f"IUCN Red List API rejected the key (HTTP {status})", "AUTH_ERROR")
    if status is not None and 400 <= status < 500:
        return router_input_error(sc, f"IUCN returned {status}: {(body or '')[:200]}", "INPUT_INVALID")
    return None
