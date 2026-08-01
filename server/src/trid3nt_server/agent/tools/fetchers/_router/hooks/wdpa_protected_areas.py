"""wdpa_protected_areas hooks (tier-3 chained-resolution mode, ADR 0063/0066): WDPA
protected-area polygons, offset-paged.

The wave-11 deferral (ADR 0059) was two bespoke steps the declarative param surface
could not carry: a designation alias-normalizer that RAISES on an unknown token (vs
ParamSpec.aliases which passes through), and a POST-fetch fail-loud when the filter
emptied a non-empty result (the "goes18 vs goes-18" silent-mismatch guard). Both are
PURE compute at a hook edge, so they fold with ZERO new machinery: ``build_request``
resolves + validates designation_filter (raises WDPA_DESIGNATION_INVALID on an unknown
spelling) and builds the geojson query; ``next_page`` does offset paging;
``parse_response`` applies the client-side casefold filter and raises the fail-loud typed
error when a non-empty bbox is emptied by the filter. All I/O stays router-owned.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "next_page", "parse_response"]

_WDPA_URL = (
    "https://services5.arcgis.com/Mj0hjvkNtV7NRhA7/ArcGIS/rest/services/"
    "WDPA_v0/FeatureServer/1/query"
)
_WDPA_OUT_FIELDS = "name_eng,desig_eng,iucn_cat,status,status_yr,site_id"
_OUT_COLUMNS = ("name_eng", "desig_eng", "iucn_cat", "status", "status_yr", "site_id")
_DESIG_FIELD = "desig_eng"
_PAGE_SIZE = 2000
_DESIG_SUFFIX = "DESIGNATION_INVALID"

_CANONICAL_DESIGNATIONS: tuple[str, ...] = (
    "National Park", "National Wildlife Refuge", "National Preserve",
    "National Monument", "National Forest", "National Recreation Area",
    "National Seashore", "National Lakeshore", "National Conservation Area",
    "National Marine Sanctuary", "National Estuarine Research Reserve",
    "Wilderness Area", "State Park", "State Forest",
    "State Wildlife Management Area", "State Wildlife Area",
    "Wildlife Management Area", "Wildlife Sanctuary", "Nature Reserve",
    "Marine Protected Area", "Habitat/Species Management Area",
    "Protected Landscape/Seascape",
    "Ramsar Site, Wetland of International Importance",
    "World Heritage Site (natural or mixed)", "UNESCO-MAB Biosphere Reserve",
    "Area of Outstanding Natural Beauty", "Site of Special Scientific Interest",
    "Special Area of Conservation (Habitats Directive)",
    "Special Protection Area (Birds Directive)", "Conservation Area",
    "Game Reserve", "Forest Reserve",
)
_DESIG_CANONICAL_BY_FOLD: dict[str, str] = {d.casefold(): d for d in _CANONICAL_DESIGNATIONS}
_DESIG_ALIASES: dict[str, str] = {
    "np": "National Park", "nps": "National Park", "nwr": "National Wildlife Refuge",
    "nm": "National Monument", "nf": "National Forest", "nra": "National Recreation Area",
    "nms": "National Marine Sanctuary", "nerr": "National Estuarine Research Reserve",
    "wma": "Wildlife Management Area", "mpa": "Marine Protected Area",
    "sssi": "Site of Special Scientific Interest",
    "aonb": "Area of Outstanding Natural Beauty",
    "sac": "Special Area of Conservation (Habitats Directive)",
    "spa": "Special Protection Area (Birds Directive)",
    "national parks": "National Park", "national wildlife refuges": "National Wildlife Refuge",
    "national monuments": "National Monument", "national forests": "National Forest",
    "national preserves": "National Preserve", "wilderness areas": "Wilderness Area",
    "state parks": "State Park", "nature reserves": "Nature Reserve",
    "marine protected areas": "Marine Protected Area",
    "wildlife management areas": "Wildlife Management Area",
    "biosphere reserve": "UNESCO-MAB Biosphere Reserve",
    "ramsar site": "Ramsar Site, Wetland of International Importance",
    "ramsar": "Ramsar Site, Wetland of International Importance",
    "world heritage site": "World Heritage Site (natural or mixed)",
    "world heritage": "World Heritage Site (natural or mixed)",
}


def _fold(value: str) -> str:
    return " ".join(value.replace(".", " ").split()).casefold()


def _normalize_one(sc: str, value: Any) -> str:
    if not isinstance(value, str):
        raise router_input_error(sc, f"designation_filter entries must be str; got {type(value).__name__}", _DESIG_SUFFIX)
    stripped = value.strip()
    if not stripped:
        raise router_input_error(sc, "designation_filter entries must be non-empty strings; got an empty/whitespace value", _DESIG_SUFFIX)
    fold = _fold(stripped)
    if fold in _DESIG_CANONICAL_BY_FOLD:
        return _DESIG_CANONICAL_BY_FOLD[fold]
    if fold in _DESIG_ALIASES:
        return _DESIG_ALIASES[fold]
    fold_nospace = fold.replace(" ", "")
    if fold_nospace in _DESIG_ALIASES:
        return _DESIG_ALIASES[fold_nospace]
    if fold.endswith("s") and fold[:-1] in _DESIG_CANONICAL_BY_FOLD:
        return _DESIG_CANONICAL_BY_FOLD[fold[:-1]]
    accepted = ", ".join(_CANONICAL_DESIGNATIONS)
    raise router_input_error(
        sc,
        f"designation_filter entry {value!r} is not a known WDPA designation or alias. "
        f"Accepted designations (case/plural/abbreviation insensitive): {accepted}. "
        f"Accepted aliases include: {', '.join(sorted(_DESIG_ALIASES))}. "
        "Pass designation_filter=None to keep all designations.",
        _DESIG_SUFFIX,
    )


def _normalize_filter(sc: str, raw: Any) -> list[str] | None:
    if not raw:
        return None
    if not isinstance(raw, (list, tuple)):
        raise router_input_error(sc, f"designation_filter must be a list[str] or None; got {type(raw).__name__}", _DESIG_SUFFIX)
    return sorted({_normalize_one(sc, d) for d in raw})


def _page_plan(spec: SourceSpec, params: dict[str, Any], offset: int) -> "_hooks.RequestPlan":
    b = params["bbox"]
    q = {
        "where": "1=1",
        "geometry": f"{b[0]},{b[1]},{b[2]},{b[3]}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": _WDPA_OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    return _hooks.RequestPlan(url=_WDPA_URL, params=q, headers={"User-Agent": spec.auth.user_agent})


@_hooks.register_hook("wdpa_protected_areas.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate designation_filter (raise-on-unknown alias), build page 1."""
    _normalize_filter(spec.error_code_prefix, params.get("designation_filter"))
    return [_page_plan(spec, params, 0)]


def _page(sc: str, body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"WDPA returned non-JSON body: {exc}")
    if isinstance(obj, dict) and obj.get("error"):
        raise router_upstream_error(sc, f"WDPA query returned error envelope: {obj['error']}")
    return obj if isinstance(obj, dict) else {}


@_hooks.register_hook("wdpa_protected_areas.next_page")
def next_page(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> "_hooks.RequestPlan | None":
    """Offset paging driven by exceededTransferLimit (twin's stop condition)."""
    sc = spec.error_code_prefix
    obj = _page(sc, bodies[-1])
    feats = obj.get("features", []) or []
    more = bool(
        obj.get("exceededTransferLimit")
        or (obj.get("properties") or {}).get("exceededTransferLimit")
    )
    if not more or len(feats) == 0:
        return None
    total = sum(len(_page(sc, b).get("features", []) or []) for b in bodies)
    return _page_plan(spec, params, total)


@_hooks.register_hook("wdpa_protected_areas.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode pages; client-side casefold designation filter; fail-loud when emptied."""
    sc = spec.error_code_prefix
    desig = _normalize_filter(sc, params.get("designation_filter"))
    all_feats: list[dict[str, Any]] = []
    for body in bodies:
        for feat in _page(sc, body).get("features", []) or []:
            if not isinstance(feat, dict) or feat.get("geometry") is None:
                continue
            # Pass the server-returned props through as-is (the twin does not
            # project) so a feature the server omits a null field for keeps the
            # twin's dynamic column set; the empty-schema header carries all 6.
            all_feats.append({
                "type": "Feature",
                "geometry": feat.get("geometry"),
                "properties": dict(feat.get("properties") or {}),
            })
    if not desig:
        return all_feats
    filter_set = {d.casefold() for d in desig}
    filtered = [
        f for f in all_feats
        if str((f.get("properties") or {}).get(_DESIG_FIELD, "")).casefold() in filter_set
    ]
    if all_feats and not filtered:
        present = sorted({
            str((f.get("properties") or {}).get(_DESIG_FIELD, "")).strip()
            for f in all_feats if (f.get("properties") or {}).get(_DESIG_FIELD)
        })
        raise router_input_error(
            sc,
            f"designation_filter {desig} matched 0 of {len(all_feats)} protected area(s) "
            f"in the bbox. Designations actually present here: {present}. Adjust "
            "designation_filter to one of these, or pass designation_filter=None to keep all.",
            _DESIG_SUFFIX,
        )
    return filtered
