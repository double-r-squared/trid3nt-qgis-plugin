"""nws_river_forecast hooks: the selector, the one-gauge body, and the detail families.

The request template, the row walk, the geometry and the column projection are declared
in source.yaml. Three steps are not a plain row walk and stay here: the bbox-or-lid
selector, the detail endpoint answering with ONE gauge where the list endpoint answers
with many, and the two conditional detail families whose merge reduces a forecast crest
and packs two series. A detail failure keeps its gauge with null detail."""

from __future__ import annotations

import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import field_map as _field_map
from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

_MAX_THRESHOLD_GAUGES = 60
_MAX_SERIES_GAUGES = 12
_MAX_OBS_SERIES_POINTS = 96


def _detail_url(spec: SourceSpec, lid: str) -> str:
    """One gauge's detail URL, off the same declared endpoint the request block uses."""
    endpoint = spec.endpoints["detail"]
    return str(endpoint.url_template or endpoint.url or "").format(gauge_id=lid)


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


@_hooks.register_hook("nws_river_forecast.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the spatial selector, then hand the declared request block the params.
    A lid is upper-cased and required alphanumeric, which is also what lets it sit in
    the detail path unescaped; neither selector present is not a query at all."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    gauge_id = params.get("gauge_id")
    if gauge_id:
        lid = str(gauge_id).strip().upper()
        if not lid.isalnum():
            raise router_input_error(
                sc,
                f"gauge_id must be an alphanumeric NWS lid (e.g. 'CIDI4'); got {gauge_id!r}",
                sfx,
            )
        return _field_map.declared_plans(spec, {**params, "gauge_id": lid})
    if not params.get("bbox"):
        raise router_input_error(
            sc,
            "fetch_nws_river_forecast requires bbox=(west, south, east, north) in "
            "EPSG:4326 (or a gauge_id lid for a single gauge).",
            sfx,
        )
    return _field_map.declared_plans(spec, params)


@_hooks.register_hook("nws_river_forecast.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Walk the declared row list. In gauge_id mode the body IS the one gauge, so it is
    wrapped into that same row list first -- the one step the declaration cannot state."""
    if not params.get("gauge_id"):
        return _field_map.declared_features(spec, params, bodies)
    sc = spec.error_code_prefix
    lid = str(params["gauge_id"]).strip().upper()
    raw = bodies[0] if bodies else b""
    detail = None
    if raw:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(
                sc, f"NWPS gauge detail for {lid!r} is not valid JSON: {exc}")
        detail = decoded if isinstance(decoded, dict) else None
    if detail is None:
        raise router_input_error(
            sc,
            f"NWPS has no river-forecast gauge with lid={lid!r}. Gauge ids are NWS "
            f"location ids (e.g. 'CIDI4'), not USGS site numbers; find one via a bbox "
            f"query first.",
            "NO_GAUGES",
        )
    wrapped = json.dumps({"gauges": [detail]}).encode("utf-8")
    return _field_map.declared_features(spec, params, [wrapped])


@_hooks.register_hook("nws_river_forecast.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """Two detail families under their own caps: the flood-category thresholds (bbox
    mode only -- the detail body already carries them when a lid was named) and the
    stageflow observed + forecast series."""
    headers = {"User-Agent": spec.auth.user_agent}
    plans: list[tuple[str, "_hooks.RequestPlan"]] = []
    if params.get("include_thresholds") and not params.get("gauge_id"):
        for lid in _lids(features, _MAX_THRESHOLD_GAUGES):
            plans.append((f"thr:{lid}", _hooks.RequestPlan(url=_detail_url(spec, lid), headers=headers)))
    if params.get("include_series"):
        for lid in _lids(features, _MAX_SERIES_GAUGES):
            plans.append((f"ser:{lid}", _hooks.RequestPlan(url=_detail_url(spec, lid) + "/stageflow", headers=headers)))
    return plans


def _lids(features: list[dict[str, Any]], cap: int) -> list[str]:
    return [lid for lid in ((f.get("properties") or {}).get("lid") for f in features[:cap]) if lid]


def _body(result: Any) -> bytes | None:
    return getattr(result, "body", None) if result is not None else None


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
    """Fold both families onto the RAW row the column map still has to read: the
    threshold block lands at the path a gauge_id body already carries it under, so one
    declared rule serves both modes; the crest and the two series packs land under
    their own output names. Every gauge survives a failed detail."""
    for feat in features:
        props = feat.get("properties") or {}
        lid = props.get("lid")
        if not lid:
            continue
        thr_body = _body(results.get(f"thr:{lid}"))
        if thr_body:
            try:
                detail = json.loads(thr_body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = None
            if isinstance(detail, dict) and detail.get("flood"):
                props["flood"] = detail["flood"]
        ser_key = f"ser:{lid}"
        if ser_key in results:
            series = _parse_stageflow(_body(results[ser_key]))
            props["fcst_crest_stage_ft"] = series["fcst_crest_stage_ft"]
            props["fcst_crest_time"] = series["fcst_crest_time"]
            props["obs_series_json"] = _series_to_json(series["observed"][-_MAX_OBS_SERIES_POINTS:])
            props["fcst_series_json"] = _series_to_json(series["forecast"])
    return features
