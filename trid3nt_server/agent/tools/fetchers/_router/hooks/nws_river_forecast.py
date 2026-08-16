"""nws_river_forecast hooks (chained-resolution mode): NWS/NWPS river
forecast gauges + bounded per-gauge threshold / stageflow enrichment.

The main fetch is a gauges-by-bbox list GET (or a single-gauge detail GET when
``gauge_id`` is given). ``parse_response`` decodes the gauges into Point features.
Two bounded, best-effort per-gauge enrichments then run: ``include_thresholds``
chases each gauge's flood-category threshold stages (bbox mode; free in gauge_id
mode), ``include_series`` chases each gauge's ``/stageflow`` observed+forecast
series + forecast crest. A per-gauge detail that fails keeps its gauge with None
detail (never fabricated, never silently dropped). All I/O stays router-owned.
"""

from __future__ import annotations

import json
import math
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

GAUGES_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
GAUGE_DETAIL_URL = "https://api.water.noaa.gov/nwps/v1/gauges/"

_MAX_BBOX_SQ_DEG = 2000.0
_MAX_THRESHOLD_GAUGES = 60
_MAX_SERIES_GAUGES = 12
_MAX_OBS_SERIES_POINTS = 96

_FLOOD_CATEGORY_MAP = {"no_flooding": "no_flood"}

_FGB_FLOAT_COLS = [
    "obs_stage_ft", "obs_flow_kcfs", "fcst_stage_ft", "fcst_flow_kcfs",
    "action_stage_ft", "minor_stage_ft", "moderate_stage_ft", "major_stage_ft",
    "fcst_crest_stage_ft",
]
_FGB_STR_COLS = [
    "lid", "usgs_id", "name", "rfc", "wfo", "state", "flood_category",
    "fcst_flood_category", "obs_valid_time", "fcst_valid_time", "fcst_crest_time",
    "obs_series_json", "fcst_series_json",
]


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= -999.0:
        return None
    return f


def _normalize_flood_category(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    return _FLOOD_CATEGORY_MAP.get(s, s)


def _detail_url(lid: str) -> str:
    return GAUGE_DETAIL_URL + urllib.parse.quote(str(lid).strip(), safe="")


def _stageflow_url(lid: str) -> str:
    return _detail_url(lid) + "/stageflow"


# --------------------------------------------------------------------------- #
# build_request: bbox list OR single-gauge detail, with the bespoke gates.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("nws_river_forecast.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the spatial selector (bbox OR gauge_id) and build the gauges request."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    gauge_id = params.get("gauge_id")
    if gauge_id:
        lid = str(gauge_id).strip().upper()
        if not lid or not lid.isalnum():
            raise router_input_error(sc, f"gauge_id must be an alphanumeric NWS lid (e.g. 'CIDI4'); got {gauge_id!r}", sfx)
        return [_hooks.RequestPlan(url=_detail_url(lid), headers=_headers(spec))]

    bbox = params.get("bbox")
    if not bbox:
        raise router_input_error(
            sc,
            "fetch_nws_river_forecast requires bbox=(west, south, east, north) in "
            "EPSG:4326 (or a gauge_id lid for a single gauge).",
            sfx,
        )
    west, south, east, north = bbox
    area = max(0.0, east - west) * max(0.0, north - south)
    if area > _MAX_BBOX_SQ_DEG:
        raise router_input_error(
            sc,
            f"bbox area {area:.0f} deg^2 exceeds the {_MAX_BBOX_SQ_DEG:.0f} deg^2 "
            f"limit; the NWS river-gauge set spans the US, so an unbounded bbox "
            f"would pull thousands of points. Re-issue with a basin / metro / "
            f"state-sized bbox.",
            "BBOX_TOO_LARGE",
        )
    q = {
        "bbox.xmin": f"{west}", "bbox.ymin": f"{south}",
        "bbox.xmax": f"{east}", "bbox.ymax": f"{north}",
        "srid": "EPSG_4326",
    }
    return [_hooks.RequestPlan(url=GAUGES_URL, params=q, headers=_headers(spec))]


# --------------------------------------------------------------------------- #
# parse_response: gauges-list OR single-detail -> gauge records -> point features.
# --------------------------------------------------------------------------- #


def _parse_gauge_records(obj: dict[str, Any]) -> list[dict[str, Any]]:
    gauges = obj.get("gauges") or []
    records: list[dict[str, Any]] = []
    for g in gauges:
        lat = _coerce_float(g.get("latitude"))
        lon = _coerce_float(g.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        status = g.get("status") or {}
        obs = status.get("observed") or {}
        fc = status.get("forecast") or {}
        rec: dict[str, Any] = {
            "lid": str(g.get("lid") or "").strip(),
            "usgs_id": str(g.get("usgsId") or "").strip(),
            "name": str(g.get("name") or "").strip(),
            "lon": lon, "lat": lat,
            "rfc": str((g.get("rfc") or {}).get("abbreviation") or "").strip(),
            "wfo": str((g.get("wfo") or {}).get("abbreviation") or "").strip(),
            "state": str((g.get("state") or {}).get("abbreviation") or "").strip(),
            "flood_category": _normalize_flood_category(obs.get("floodCategory")),
            "obs_stage_ft": _coerce_float(obs.get("primary")),
            "obs_flow_kcfs": _coerce_float(obs.get("secondary")),
            "obs_valid_time": str(obs.get("validTime") or "").strip() or None,
            "fcst_flood_category": _normalize_flood_category(fc.get("floodCategory")),
            "fcst_stage_ft": _coerce_float(fc.get("primary")),
            "fcst_flow_kcfs": _coerce_float(fc.get("secondary")),
            "fcst_valid_time": str(fc.get("validTime") or "").strip() or None,
            "action_stage_ft": None, "minor_stage_ft": None,
            "moderate_stage_ft": None, "major_stage_ft": None,
            "fcst_crest_stage_ft": None, "fcst_crest_time": None,
            "obs_series_json": None, "fcst_series_json": None,
        }
        if not rec["lid"]:
            continue
        records.append(rec)
    return records


def _thresholds_from_detail(obj: dict[str, Any]) -> dict[str, float | None]:
    cats = ((obj.get("flood") or {}).get("categories")) or {}
    out: dict[str, float | None] = {}
    for cat in ("action", "minor", "moderate", "major"):
        out[f"{cat}_stage_ft"] = _coerce_float((cats.get(cat) or {}).get("stage"))
    return out


def _record_to_feature(rec: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for col in _FGB_STR_COLS:
        props[col] = str(rec.get(col) or "")
    for col in _FGB_FLOAT_COLS:
        props[col] = None if rec.get(col) is None else float(rec[col])
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
        "properties": props,
    }


@_hooks.register_hook("nws_river_forecast.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decode the gauges (list or single detail) -> point features; honest no-gauges error."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    gauge_id = params.get("gauge_id")

    if gauge_id:
        lid = str(gauge_id).strip().upper()
        if not raw:
            raise _no_gauges_gauge(sc, lid)
        try:
            detail = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"NWPS gauge detail for {lid!r} is not valid JSON: {exc}")
        if not isinstance(detail, dict):
            raise _no_gauges_gauge(sc, lid)
        records = _parse_gauge_records({"gauges": [detail]})
        if not records:
            raise _no_gauges_gauge(sc, lid)
        thr = _thresholds_from_detail(detail)  # thresholds ride along free in gauge_id mode
        for rec in records:
            rec.update(thr)
        return [_record_to_feature(r) for r in records]

    if not raw:
        raise _no_gauges_bbox(sc, params.get("bbox"))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NWPS gauges response is not valid JSON: {exc}")
    records = _parse_gauge_records(obj)
    if not records:
        raise _no_gauges_bbox(sc, params.get("bbox"))
    return [_record_to_feature(r) for r in records]


def _no_gauges_gauge(sc: str, lid: str):
    return router_input_error(
        sc,
        f"NWPS has no river-forecast gauge with lid={lid!r}. Gauge ids are NWS "
        f"location ids (e.g. 'CIDI4'), not USGS site numbers; find one via a bbox "
        f"query first.",
        "NO_GAUGES",
    )


def _no_gauges_bbox(sc: str, bbox: Any):
    return router_input_error(
        sc,
        f"No NWS river/forecast gauges (AHPS/NWPS) found inside bbox={bbox!r}. The "
        f"NWPS gauges-by-bbox service returned zero forecast points. Either the area "
        f"has no forecast river reach or the bbox misses the river; try a larger bbox "
        f"or an area on a known forecast river.",
        "NO_GAUGES",
    )


# --------------------------------------------------------------------------- #
# Enrichment: bounded per-gauge threshold (bbox mode) + stageflow series.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("nws_river_forecast.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """threshold refs (bbox mode, first 60) + stageflow-series refs (first 12)."""
    gauge_mode = bool(params.get("gauge_id"))
    include_thresholds = bool(params.get("include_thresholds"))
    include_series = bool(params.get("include_series"))
    plans: list[tuple[str, "_hooks.RequestPlan"]] = []
    if include_thresholds and not gauge_mode:
        for feat in features[:_MAX_THRESHOLD_GAUGES]:
            lid = (feat.get("properties") or {}).get("lid")
            if lid:
                plans.append((f"thr:{lid}", _hooks.RequestPlan(url=_detail_url(lid), headers=_headers(spec))))
    if include_series:
        for feat in features[:_MAX_SERIES_GAUGES]:
            lid = (feat.get("properties") or {}).get("lid")
            if lid:
                plans.append((f"ser:{lid}", _hooks.RequestPlan(url=_stageflow_url(lid), headers=_headers(spec))))
    return plans


def _parse_stageflow(body: bytes | None) -> dict[str, Any]:
    empty: dict[str, Any] = {"observed": [], "forecast": [], "fcst_crest_stage_ft": None, "fcst_crest_time": None}
    if not body:
        return empty
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return empty
    if not isinstance(obj, dict):
        return empty

    def _series(key: str) -> list[tuple[str, float | None, float | None]]:
        block = obj.get(key) or {}
        pts: list[tuple[str, float | None, float | None]] = []
        for p in block.get("data") or []:
            if not isinstance(p, dict):
                continue
            t = str(p.get("validTime") or "").strip()
            if not t:
                continue
            pts.append((t, _coerce_float(p.get("primary")), _coerce_float(p.get("secondary"))))
        return pts

    observed = _series("observed")
    forecast = _series("forecast")
    crest_stage: float | None = None
    crest_time: str | None = None
    for t, stage, _flow in forecast:
        if stage is not None and (crest_stage is None or stage > crest_stage):
            crest_stage, crest_time = stage, t
    return {"observed": observed, "forecast": forecast, "fcst_crest_stage_ft": crest_stage, "fcst_crest_time": crest_time}


def _series_to_json(points: list[tuple[str, float | None, float | None]]) -> str:
    return json.dumps(
        {"t": [p[0] for p in points], "stage_ft": [p[1] for p in points], "flow_kcfs": [p[2] for p in points]},
        separators=(",", ":"),
    )


@_hooks.register_hook("nws_river_forecast.enrich_merge")
def enrich_merge(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fold the fetched threshold + stageflow detail onto each gauge; keep every gauge."""
    for feat in features:
        props = feat.get("properties") or {}
        lid = props.get("lid")
        if not lid:
            continue
        thr_res = results.get(f"thr:{lid}")
        thr_body = getattr(thr_res, "body", None) if thr_res is not None else None
        if thr_body:
            try:
                detail = json.loads(thr_body.decode("utf-8"))
                if isinstance(detail, dict):
                    for k, v in _thresholds_from_detail(detail).items():
                        props[k] = None if v is None else float(v)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        ser_res = results.get(f"ser:{lid}")
        if ser_res is not None:
            series = _parse_stageflow(getattr(ser_res, "body", None))
            props["fcst_crest_stage_ft"] = None if series["fcst_crest_stage_ft"] is None else float(series["fcst_crest_stage_ft"])
            props["fcst_crest_time"] = str(series["fcst_crest_time"] or "")
            props["obs_series_json"] = _series_to_json(series["observed"][-_MAX_OBS_SERIES_POINTS:])
            props["fcst_series_json"] = _series_to_json(series["forecast"])
    return features
