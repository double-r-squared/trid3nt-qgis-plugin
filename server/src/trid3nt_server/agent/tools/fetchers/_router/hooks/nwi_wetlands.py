"""nwi_wetlands hooks (tier-3 chained-resolution mode, ADR 0063/0066): USFWS National
Wetlands Inventory polygons, offset-paged.

The wave-11 deferral (ADR 0059) was three bespoke steps: the WAF-required browser
header trio, the table-prefix-strip / first-wins property normalizer, and a same-URL
geojson->esri-json format fallback. All three fold onto the EXISTING hooks with ZERO
new machinery: ``build_request`` sets the WAF headers on the RequestPlan and builds the
geojson query, ``next_page`` does offset paging (stop on a short page /
exceededTransferLimit), ``parse_response`` runs the prefix-strip first-wins normalizer.
The same-URL esri fallback is NOT reproduced (the live host serves geojson with the WAF
headers, and the twin's fallback ring decode was never byte-parity -- ADR 0066 divergence).
All I/O stays router-owned.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_upstream_error

__all__ = ["build_request", "next_page", "parse_response"]

_NWI_URL = (
    "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/"
    "rest/services/Wetlands/MapServer/0/query"
)

#: Browser-like header trio required to get JSON past the host WAF.
_NWI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36 trid3nt/0.1 (Hazard Modeling Agent; agent@trid3nt.dev)"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fws.gov/program/national-wetlands-inventory",
}

_PAGE_SIZE = 1000
_OUT_COLUMNS: tuple[str, ...] = ("attribute", "wetland_type", "acres")


def _page_plan(spec: SourceSpec, params: dict[str, Any], offset: int) -> "_hooks.RequestPlan":
    b = params["bbox"]
    q = {
        "where": "1=1",
        "geometry": f"{b[0]},{b[1]},{b[2]},{b[3]}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "*",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    return _hooks.RequestPlan(url=_NWI_URL, params=q, headers=dict(_NWI_HEADERS))


@_hooks.register_hook("nwi_wetlands.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Build page 1 of the geojson query (offset 0) with the WAF header trio."""
    return [_page_plan(spec, params, 0)]


def _page(sc: str, body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NWI returned non-JSON body (WAF/HTML?): {exc}")
    if not isinstance(obj, dict):
        raise router_upstream_error(sc, "NWI response is not a JSON object")
    if "error" in obj:
        raise router_upstream_error(sc, f"NWI query returned error envelope: {obj['error']}")
    if obj.get("type") != "FeatureCollection":
        raise router_upstream_error(
            sc, f"NWI geojson response is not a FeatureCollection: type={obj.get('type')!r}"
        )
    return obj


@_hooks.register_hook("nwi_wetlands.next_page")
def next_page(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> "_hooks.RequestPlan | None":
    """Offset paging: next offset by the page's feature count; stop on a short page."""
    sc = spec.error_code_prefix
    obj = _page(sc, bodies[-1])
    feats = obj.get("features", []) or []
    exceeded = bool(
        obj.get("exceededTransferLimit")
        or (obj.get("properties") or {}).get("exceededTransferLimit")
    )
    if (len(feats) < _PAGE_SIZE and not exceeded) or len(feats) == 0:
        return None
    total = sum(len(_page(sc, b).get("features", []) or []) for b in bodies)
    return _page_plan(spec, params, total)


def _normalize_props(props: dict[str, Any]) -> dict[str, Any]:
    """Strip the ``Wetlands.`` / ``NWI_Wetland_Codes.`` table prefix; first-wins; keep 3 cols."""
    flat: dict[str, Any] = {}
    for key, val in (props or {}).items():
        base = key.rsplit(".", 1)[-1].strip().lower()
        if base in _OUT_COLUMNS and base not in flat:
            flat[base] = val
    return {c: flat.get(c) for c in _OUT_COLUMNS}


@_hooks.register_hook("nwi_wetlands.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode all pages; normalize props (prefix-strip, first-wins) to the 3 NWI columns."""
    sc = spec.error_code_prefix
    out: list[dict[str, Any]] = []
    for body in bodies:
        for feat in _page(sc, body).get("features", []) or []:
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if geom is None:
                continue
            out.append({
                "type": "Feature",
                "geometry": geom,
                "properties": _normalize_props(feat.get("properties") or {}),
            })
    return out
