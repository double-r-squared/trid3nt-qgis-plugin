"""gbif_occurrences hooks (chained-resolution mode, ADR 0063): GBIF species points.

Two irreducible steps the declarative surface cannot carry:
1. RESOLVE (name -> taxonKey): a ``species/match`` GET whose EXACT-match gate the
   router runs BEFORE the cache key so a name query and its taxonKey query collapse.
   A numeric ``species_key`` is a taxonKey already -> the resolve hook returns ``[]``
   (skip the round trip).
2. The occurrence search: offset paging to ``endOfRecords`` / ``max_records`` (the
   ``next_page`` hook) + the point decode with the bbox-clip correctness gate.

All I/O (both round trips, the paging loop, retry, cache, FGB serialize, LayerURI)
stays router-owned; these hooks only compute.
"""

from __future__ import annotations

import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["resolve_build", "resolve_parse", "build_request", "next_page", "parse_response"]

_GBIF_SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
_GBIF_SPECIES_MATCH_URL = "https://api.gbif.org/v1/species/match"
_PAGE_SIZE = 300

#: Only an EXACT species/match is safe to drive a search off a name string (FUZZY
#: swaps a near-spelling taxon, HIGHERRANK widens to a parent) -- the twin's gate.
_ACCEPTED_MATCH_TYPES = frozenset({"EXACT"})


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _resolved_taxon_key(params: dict[str, Any]) -> int:
    """The taxonKey the main fetch drives off: the resolved key else the numeric species_key."""
    tk = params.get("taxon_key")
    if tk is not None:
        return int(tk)
    return int(str(params["species_key"]).strip())


@_hooks.register_hook("gbif_occurrences.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """species/match plan for a name; ``[]`` (skip) for a numeric taxonKey."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    raw = params.get("species_key")
    s = str(raw).strip() if raw is not None else ""
    if not s:
        raise router_input_error(sc, "species_key str must be a non-empty species name", sfx)
    if s.lstrip("-").isdigit():
        # A numeric species_key IS a taxonKey (fast path) -- must be positive.
        if int(s) <= 0:
            raise router_input_error(sc, f"taxonKey must be a positive int; got {s}", sfx)
        return []
    return [_hooks.RequestPlan(url=_GBIF_SPECIES_MATCH_URL, params={"name": s}, headers=_headers(spec))]


@_hooks.register_hook("gbif_occurrences.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """EXACT-match gate on species/match -> ``{"taxon_key": usageKey}`` (the twin's gate)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    name = str(params.get("species_key"))
    try:
        payload = json.loads(bodies[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise router_upstream_error(sc, f"GBIF species/match returned non-JSON for name={name!r}: {exc}")

    usage_key = payload.get("usageKey")
    match_type = payload.get("matchType")
    if usage_key is None:
        raise router_input_error(
            sc,
            f"GBIF could not resolve species name {name!r} "
            f"(matchType={match_type if match_type is not None else '?'!r})",
            sfx,
        )
    matched_name = payload.get("scientificName") or payload.get("canonicalName") or "?"
    matched_rank = payload.get("rank", "?")
    confidence = payload.get("confidence")
    if match_type not in _ACCEPTED_MATCH_TYPES:
        accepted = ", ".join(sorted(_ACCEPTED_MATCH_TYPES))
        raise router_input_error(
            sc,
            f"GBIF resolved species name {name!r} only via a "
            f"matchType={match_type!r} (confidence={confidence}) match to "
            f"{matched_name!r} (rank={matched_rank!r}, taxonKey={usage_key}); "
            f"refusing to drive an occurrence search off an ambiguous match. "
            f"Did you mean {matched_name!r}? Re-issue with the exact "
            f"scientific name (accepted matchType: {accepted}) or pass the "
            f"numeric GBIF taxonKey directly for a deliberate higher-taxon query.",
            sfx,
        )
    try:
        return {"taxon_key": int(usage_key)}
    except (TypeError, ValueError):
        raise router_upstream_error(sc, f"GBIF species/match usageKey is not an int: {usage_key!r}")


def _search_plan(spec: SourceSpec, params: dict[str, Any], offset: int) -> "_hooks.RequestPlan":
    west, south, east, north = params["bbox"]
    q: dict[str, Any] = {
        "taxonKey": _resolved_taxon_key(params),
        "decimalLongitude": f"{west},{east}",
        "decimalLatitude": f"{south},{north}",
        "hasCoordinate": "true",
        "limit": _PAGE_SIZE,
        "offset": offset,
    }
    yr = params.get("year_range")
    if yr:
        q["year"] = f"{yr[0]},{yr[1]}"
    return _hooks.RequestPlan(url=_GBIF_SEARCH_URL, params=q, headers=_headers(spec))


@_hooks.register_hook("gbif_occurrences.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """The occurrence/search page-1 request (offset 0)."""
    return [_search_plan(spec, params, 0)]


@_hooks.register_hook("gbif_occurrences.next_page")
def next_page(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> "_hooks.RequestPlan | None":
    """Offset paging: next page until endOfRecords / max_records / an empty page."""
    sc = spec.error_code_prefix
    try:
        last = json.loads(bodies[-1].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"GBIF occurrence/search returned non-JSON: {exc}")
    results = last.get("results") or []
    if not isinstance(results, list):
        raise router_upstream_error(sc, f"GBIF occurrence/search 'results' is not a list: {type(results).__name__}")
    end_of_records = bool(last.get("endOfRecords", True))
    max_records = int(params.get("max_records", 5000))
    total_so_far = (len(bodies) - 1) * _PAGE_SIZE + len(results)
    if end_of_records or total_so_far >= max_records or not results:
        return None
    return _search_plan(spec, params, len(bodies) * _PAGE_SIZE)


@_hooks.register_hook("gbif_occurrences.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Concatenate the pages, trim to max_records, coordinate-validate + bbox-clip -> points."""
    sc = spec.error_code_prefix
    max_records = int(params.get("max_records", 5000))
    west, south, east, north = params["bbox"]

    records: list[dict[str, Any]] = []
    for raw in bodies:
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"GBIF occurrence/search returned non-JSON: {exc}")
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise router_upstream_error(sc, f"GBIF occurrence/search 'results' is not a list: {type(results).__name__}")
        records.extend(results)
    if len(records) > max_records:
        records = records[:max_records]

    out: list[dict[str, Any]] = []
    for rec in records:
        lon = rec.get("decimalLongitude")
        lat = rec.get("decimalLatitude")
        if lon is None or lat is None:
            continue
        try:
            lon_f, lat_f = float(lon), float(lat)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
            continue
        # Geographic-correctness gate: every emitted point falls in the bbox.
        if not (west <= lon_f <= east and south <= lat_f <= north):
            continue
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
            "properties": {
                "gbifID": rec.get("gbifID"),
                "species": rec.get("species") or rec.get("scientificName") or "",
                "eventDate": rec.get("eventDate") or "",
                "coordinateUncertaintyInMeters": rec.get("coordinateUncertaintyInMeters"),
                "basisOfRecord": rec.get("basisOfRecord") or "",
            },
        })
    return out
