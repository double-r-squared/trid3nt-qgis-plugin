"""inaturalist_observations hooks (chained-resolution mode, ADR 0063): iNat points.

Same shape as gbif: RESOLVE a taxon name -> id via ``/v1/taxa`` (pre-cache, so name
and id collapse; a digit ``taxon_id`` skips the round trip), then the ``/v1/observations``
page-number fetch to ``total_results`` / ``max_records`` (the ``next_page`` hook) and
the observation-point decode. All I/O stays router-owned.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["resolve_build", "resolve_parse", "build_request", "next_page", "parse_response"]

_INAT_BASE = "https://api.inaturalist.org/v1"
_OBSERVATIONS_URL = f"{_INAT_BASE}/observations"
_TAXA_URL = f"{_INAT_BASE}/taxa"
_PER_PAGE = 200


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _resolved_taxon_id(params: dict[str, Any]) -> int:
    tid = params.get("taxon_id_int")
    if tid is not None:
        return int(tid)
    return int(str(params["taxon_id"]).strip())


@_hooks.register_hook("inaturalist_observations.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """/v1/taxa plan for a name; ``[]`` (skip) for a digit taxon_id."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    raw = params.get("taxon_id")
    s = str(raw).strip() if raw is not None else ""
    if not s:
        raise router_input_error(sc, "taxon_id string must be non-empty", sfx)
    if s.isdigit():
        if int(s) <= 0:
            raise router_input_error(sc, f"taxon_id integer must be positive; got {s!r}", sfx)
        return []
    return [_hooks.RequestPlan(url=_TAXA_URL, params={"q": s, "per_page": 1}, headers=_headers(spec))]


@_hooks.register_hook("inaturalist_observations.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """Top-hit id from /v1/taxa -> ``{"taxon_id_int": id}`` (the twin's contract)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    name = str(params.get("taxon_id"))
    try:
        payload = json.loads(bodies[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise router_upstream_error(sc, f"iNat taxa lookup returned non-JSON for name={name!r}: {exc}")
    results = payload.get("results") or []
    if not results:
        raise router_input_error(sc, f"iNat taxa lookup returned no results for name={name!r}", sfx)
    try:
        return {"taxon_id_int": int(results[0]["id"])}
    except (KeyError, TypeError, ValueError) as exc:
        raise router_upstream_error(sc, f"iNat taxa top-hit missing/invalid id for name={name!r}: {exc}")


def _obs_plan(spec: SourceSpec, params: dict[str, Any], page: int) -> "_hooks.RequestPlan":
    min_lon, min_lat, max_lon, max_lat = params["bbox"]
    q: dict[str, Any] = {
        "taxon_id": _resolved_taxon_id(params),
        "swlat": min_lat,
        "swlng": min_lon,
        "nelat": max_lat,
        "nelng": max_lon,
        "quality_grade": params.get("quality_grade", "research"),
        "per_page": _PER_PAGE,
        "page": page,
        "geo": "true",
    }
    days_back = params.get("days_back")
    if days_back is not None:
        q["d1"] = (datetime.now(timezone.utc) - timedelta(days=int(days_back))).strftime("%Y-%m-%d")
    return _hooks.RequestPlan(url=_OBSERVATIONS_URL, params=q, headers=_headers(spec))


def _valid_records(raw: bytes, sc: str) -> tuple[list[dict[str, Any]], Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"iNat observations non-JSON response: {exc}")
    results = payload.get("results") or []
    if not isinstance(results, list):
        raise router_upstream_error(sc, f"iNat observations 'results' is not a list: {type(results).__name__}")
    recs: list[dict[str, Any]] = []
    for obs in results:
        if not isinstance(obs, dict):
            continue
        rec = _extract_observation_record(obs)
        if rec is not None:
            recs.append(rec)
    return recs, payload.get("total_results")


def _extract_observation_record(obs: dict[str, Any]) -> dict[str, Any] | None:
    geojson = obs.get("geojson") or {}
    coords = geojson.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2 or coords[0] is None or coords[1] is None:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    photo_url: str | None = None
    photos = obs.get("photos") or []
    if isinstance(photos, list) and photos:
        first = photos[0] or {}
        if isinstance(first, dict):
            photo_url = first.get("url") or first.get("medium_url") or None
    user_login: str | None = None
    user = obs.get("user") or {}
    if isinstance(user, dict):
        user_login = user.get("login") or user.get("login_exact") or None
    return {
        "id": obs.get("id"),
        "observed_on": obs.get("observed_on"),
        "user_login": user_login,
        "photo_url": photo_url,
        "species_guess": obs.get("species_guess"),
        "place_guess": obs.get("place_guess"),
        "lon": lon,
        "lat": lat,
    }


@_hooks.register_hook("inaturalist_observations.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """The /v1/observations page-1 request."""
    return [_obs_plan(spec, params, 1)]


@_hooks.register_hook("inaturalist_observations.next_page")
def next_page(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> "_hooks.RequestPlan | None":
    """Page-number paging: next page until max_records / total_results / an empty page."""
    sc = spec.error_code_prefix
    max_records = int(params.get("max_records", 5000))
    records_so_far = 0
    last_results: list[dict[str, Any]] = []
    total_results: Any = None
    for i, raw in enumerate(bodies):
        recs, tr = _valid_records(raw, sc)
        records_so_far += len(recs)
        last_results = recs
        if i == 0:
            total_results = int(tr) if isinstance(tr, int) else None
    page_count = len(bodies)
    if records_so_far >= max_records or not last_results:
        return None
    if total_results is not None and (page_count * _PER_PAGE) >= total_results:
        return None
    return _obs_plan(spec, params, page_count + 1)


@_hooks.register_hook("inaturalist_observations.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Concatenate the pages' valid observations, trim to max_records -> points."""
    sc = spec.error_code_prefix
    max_records = int(params.get("max_records", 5000))
    records: list[dict[str, Any]] = []
    for raw in bodies:
        recs, _tr = _valid_records(raw, sc)
        records.extend(recs)
        if len(records) >= max_records:
            break
    records = records[:max_records]
    return [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "id": r["id"],
                "observed_on": r.get("observed_on"),
                "user_login": r.get("user_login"),
                "photo_url": r.get("photo_url"),
                "species_guess": r.get("species_guess"),
                "place_guess": r.get("place_guess"),
            },
        }
        for r in records
    ]
