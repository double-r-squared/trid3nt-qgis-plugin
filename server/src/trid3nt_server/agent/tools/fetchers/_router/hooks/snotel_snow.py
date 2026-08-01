"""snotel_snow hooks (chained-resolution mode, ADR 0063/0065): NRCS SNOTEL/SCAN
snow stations (AWDB REST).

The batched-snapshot shape folds onto the EXISTING main-fetch + enrich phases, zero
new machinery. The MAIN FETCH is the stations catalog GET (``build_request`` single
plan); ``parse_response`` parses the SNTL/SCAN catalog, bbox-filters it, and emits one
station Point feature (raising SNOTEL_NO_STATIONS when the bbox holds none -- the
spatial primary). PHASE E is ONE batched data GET for all triplets (``enrich_plan``
emits a single ``batch`` ref); ``enrich_merge`` folds the latest non-null WTEQ/SNWD
per station, null-tolerantly. The chained mode's best-effort per-ref survival IS the
data-source degrade-to-locations fallback (a failed batch keeps every station with
null readings). ``output.bbox_from_features`` stamps LayerURI.bbox = station extent.
All I/O stays router-owned; these hooks only compute.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_upstream_error

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

_AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
_STATIONS_URL = _AWDB_BASE + "/stations"
_DATA_URL = _AWDB_BASE + "/data"

_SNOW_NETWORKS = frozenset({"SNTL", "SCAN"})
_ELEM_SWE = "WTEQ"
_ELEM_DEPTH = "SNWD"
_LATEST_WINDOW_DAYS = 10
_NODATA_FLOOR = -999990.0

_COLUMNS = ("triplet", "name", "state", "network", "elevation_ft", "swe_in", "snow_depth_in", "date")


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


# --------------------------------------------------------------------------- #
# MAIN FETCH -- stations catalog (the spatial primary) -> bbox-filtered features.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("snotel_snow.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """GET the active SNTL+SCAN stations catalog (no service-side bbox param)."""
    q = urllib.parse.urlencode([("networkCds", ",".join(sorted(_SNOW_NETWORKS))), ("activeOnly", "true")])
    return [_hooks.RequestPlan(url=f"{_STATIONS_URL}?{q}", headers=_headers(spec))]


@_hooks.register_hook("snotel_snow.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Parse the catalog, keep SNTL/SCAN stations inside the bbox; NO_STATIONS if none."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        obj: Any = []
    else:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"NRCS AWDB stations response is not valid JSON: {exc}")
    west, south, east, north = (float(v) for v in params["bbox"])
    feats: list[dict[str, Any]] = []
    if isinstance(obj, list):
        for s in obj:
            if not isinstance(s, dict):
                continue
            net = str(s.get("networkCode") or "").strip().upper()
            if net not in _SNOW_NETWORKS:
                continue
            trip = str(s.get("stationTriplet") or "").strip()
            if not trip:
                continue
            try:
                lat = float(s.get("latitude")); lon = float(s.get("longitude"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            if not (west <= lon <= east and south <= lat <= north):
                continue
            try:
                elev: float | None = float(s.get("elevation"))
                if not math.isfinite(elev):
                    elev = None
            except (TypeError, ValueError):
                elev = None
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "triplet": trip, "name": str(s.get("name") or "").strip(),
                    "state": str(s.get("stateCode") or "").strip(), "network": net,
                    "elevation_ft": elev,
                    "swe_in": None, "snow_depth_in": None, "date": None,
                },
            })
    if not feats:
        raise router_empty_error(
            sc,
            f"No active NRCS SNOTEL/SCAN snow stations found inside bbox={list(params['bbox'])!r}. "
            f"SNOTEL is a western-US mountain network (Rockies, Sierra Nevada, Cascades, Wasatch, "
            f"Alaska, plus scattered eastern SCAN sites); the requested area has no automated snow "
            f"stations. Pick a mountain region or widen the bbox.",
            spec.empty_error_suffix,
        )
    return feats


# --------------------------------------------------------------------------- #
# PHASE E -- one batched data GET for all triplets; null-tolerant merge.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("snotel_snow.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """ONE batched data ref for every station triplet over the trailing window."""
    triplets = [(f.get("properties") or {}).get("triplet") for f in features]
    triplets = [t for t in triplets if t]
    if not triplets:
        return []
    today = _dt.date.today()
    begin = (today - _dt.timedelta(days=_LATEST_WINDOW_DAYS)).isoformat()
    end = today.isoformat()
    q = urllib.parse.urlencode([
        ("stationTriplets", ",".join(triplets)), ("elements", f"{_ELEM_SWE},{_ELEM_DEPTH}"),
        ("duration", "DAILY"), ("beginDate", begin), ("endDate", end), ("periodRef", "END"),
        ("returnFlags", "false"), ("returnOriginalValues", "false"), ("returnSuspectData", "false"),
    ])
    return [("batch", _hooks.RequestPlan(url=f"{_DATA_URL}?{q}", headers=_headers(spec)))]


def _parse_data(raw: bytes) -> dict[str, dict[str, Any]]:
    """{triplet: {swe_in, snow_depth_in, date}} from the AWDB data body (latest non-null)."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(obj, list):
        return {}
    readings: dict[str, dict[str, Any]] = {}
    for block in obj:
        if not isinstance(block, dict):
            continue
        trip = str(block.get("stationTriplet") or "").strip()
        if not trip:
            continue
        rec = readings.setdefault(trip, {"swe_in": None, "snow_depth_in": None, "date": None})
        for el in block.get("data") or []:
            code = str((el.get("stationElement") or {}).get("elementCode") or "").strip().upper()
            if code not in (_ELEM_SWE, _ELEM_DEPTH):
                continue
            latest_val: float | None = None
            latest_dt: str | None = None
            for sample in el.get("values") or []:
                v = sample.get("value")
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(fv) or fv <= _NODATA_FLOOR:
                    continue
                latest_val = fv
                latest_dt = str(sample.get("date") or "").strip() or latest_dt
            if latest_val is None:
                continue
            if code == _ELEM_SWE:
                rec["swe_in"] = latest_val
                if latest_dt:
                    rec["date"] = latest_dt
            else:
                rec["snow_depth_in"] = latest_val
                if rec["date"] is None and latest_dt:
                    rec["date"] = latest_dt
    return readings


@_hooks.register_hook("snotel_snow.enrich_merge")
def enrich_merge(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge the latest readings onto each station; a failed batch keeps null (degrade-to-locations)."""
    res = results.get("batch")
    body = getattr(res, "body", None) if res is not None else None
    readings = _parse_data(body) if body else {}
    out: list[dict[str, Any]] = []
    for feat in features:
        props = dict(feat.get("properties") or {})
        r = readings.get(props.get("triplet"), {})
        props["swe_in"] = r.get("swe_in")
        props["snow_depth_in"] = r.get("snow_depth_in")
        props["date"] = r.get("date")
        out.append({"type": "Feature", "geometry": feat.get("geometry"),
                    "properties": {c: props.get(c) for c in _COLUMNS}})
    return out
