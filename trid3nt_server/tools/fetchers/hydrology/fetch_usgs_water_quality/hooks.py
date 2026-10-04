"""Water Quality Portal: monitoring stations, each with its latest numeric result.

Station locations (``wqp.what_sites``) LEFT-joined with the latest numeric Result per
site (``wqp.get_results``, resultPhysChem profile, latest by ActivityStartDate). Zero
stations is the typed empty, never an empty layer."""

from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.dataretrieval import retrieve

__all__ = ["validate", "features"]

logger = logging.getLogger(__name__)


def _bbox_str(bbox: list[float]) -> str:
    return ",".join(str(v) for v in bbox)


def _str_or_none(v: Any) -> str | None:
    """Trimmed str, or None for pandas NaN / None / empty."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def _latest_results_by_site(res_df: Any) -> dict[str, dict[str, Any]]:
    """Latest NUMERIC result per MonitoringLocationIdentifier: a non-numeric
    ResultMeasureValue is skipped, and a row wins only when its ActivityStartDate is
    strictly LATER than the current best, so first-seen wins on an equal date."""
    latest: dict[str, dict[str, Any]] = {}
    cols = set(res_df.columns)
    if "MonitoringLocationIdentifier" not in cols:
        return latest
    # Read as RECORDS, not tuples: a WQP column whose name carries a slash -
    # ``ResultMeasure/MeasureUnitCode``, the unit every reading is in - is not an
    # identifier, and a named tuple renames it to its position.
    for rd in res_df.to_dict(orient="records"):
        site_id = (_str_or_none(rd.get("MonitoringLocationIdentifier")) or "")
        if not site_id:
            continue
        raw_val = _str_or_none(rd.get("ResultMeasureValue"))
        if raw_val is None:
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        date = _str_or_none(rd.get("ActivityStartDate")) or ""
        cur = latest.get(site_id)
        if cur is not None and date <= cur["date"]:
            continue
        latest[site_id] = {
            "value": value,
            "unit": _str_or_none(rd.get("ResultMeasure/MeasureUnitCode")) or "",
            "date": date,
            "fraction": _str_or_none(rd.get("ResultSampleFractionText")) or "",
            "characteristic": _str_or_none(rd.get("CharacteristicName")) or "",
        }
    return latest


def _wqp_date(valid_time: Any) -> dict[str, str]:
    """The instant a run asks about, as the WQP's own upper date bound.

    The portal reads MM-DD-YYYY; anything unparseable states no bound at all,
    which is the whole archive and the same answer this fetch gave before."""
    import datetime as _dt

    if not valid_time:
        return {}
    try:
        read = _dt.datetime.fromisoformat(str(valid_time).replace("Z", "+00:00"))
    except ValueError:
        return {}
    return {"startDateHi": read.strftime("%m-%d-%Y")}


@register_hook("wqp.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """A bbox and a characteristic, before the cache and the network."""
    if params.get("bbox") is None:
        raise router_input_error(
            spec.error_code_prefix,
            "fetch_usgs_water_quality requires bbox=(west, south, east, north) in "
            "EPSG:4326 for the area of interest (a watershed/sub-basin).",
            spec.input_error_suffix,
        )
    char = params.get("characteristic")
    if not char or not str(char).strip():
        raise router_input_error(
            spec.error_code_prefix,
            "characteristic is required (e.g. 'nitrate', 'lead', 'arsenic', 'pH', "
            "'dissolved_oxygen', 'specific_conductance')",
            spec.input_error_suffix,
        )


@register_hook("wqp.features")
def features(spec: SourceSpec, params: dict[str, Any], *,
             timeout_s: float) -> list[dict[str, Any]]:
    """The WQP point features: each station left-joined with its latest result."""
    import dataretrieval.wqp as wqp

    prefix = spec.error_code_prefix
    bbox = params.get("bbox")
    characteristic = params.get("characteristic")
    bbstr = _bbox_str(bbox)
    # The run asks what the water carried at ITS OWN moment, so the window CLOSES
    # at that instant and the latest sample on or before it is the reading. No
    # lower bound is stated: a measurement stands until the next one replaces it,
    # and a window nobody measured would be a number this code invented.
    window = _wqp_date(params.get("valid_time"))

    # 1. Station service -- the authoritative monitoring-location locations.
    sites_df, _ = retrieve(spec, wqp.what_sites, bBox=bbstr,
                           characteristicName=characteristic, **window)

    stations: dict[str, dict[str, Any]] = {}
    if sites_df is not None and len(sites_df):
        for row in sites_df.itertuples(index=False):
            rd = row._asdict()
            site_id = _str_or_none(rd.get("MonitoringLocationIdentifier"))
            if not site_id:
                continue
            try:
                lon = float(rd.get("LongitudeMeasure"))
                lat = float(rd.get("LatitudeMeasure"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            stations[site_id] = {
                "site_id": site_id,
                "site_name": _str_or_none(rd.get("MonitoringLocationName")) or "",
                "site_type": _str_or_none(rd.get("MonitoringLocationTypeName")) or "",
                "lon": lon,
                "lat": lat,
            }

    # 2. Honest typed error if no sites -- never an empty success layer.
    if not stations:
        raise router_empty_error(
            prefix,
            f"No Water Quality Portal monitoring sites found for "
            f"characteristic={characteristic!r} in bbox={bbox!r}. The WQP Station "
            f"service returned zero locations; try a different area, a different "
            f"characteristic, or a wider bbox over a monitored watershed.",
            spec.empty_error_suffix,
        )

    # 3. Result service -- latest numeric sample per site (best-effort decoration).
    res_df, _ = retrieve(spec, wqp.get_results, bBox=bbstr,
                         characteristicName=characteristic,
                         dataProfile="resultPhysChem", **window)
    results = _latest_results_by_site(res_df)

    # 4. Left-join: one record per station; latest result decorates (or nulls).
    feats: list[dict[str, Any]] = []
    for site_id, loc in stations.items():
        res = results.get(site_id) or {}
        feats.append(
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [loc["lon"], loc["lat"]]},
             "properties": {
                    "site_id": site_id,
                    "site_name": loc["site_name"],
                    "site_type": loc["site_type"],
                    "characteristic": res.get("characteristic") or "",
                    "value": res.get("value"),
                    "unit": res.get("unit") or "",
                    "result_date": res.get("date") or "",
                    "fraction": res.get("fraction") or "",
             }}
        )
    logger.info(
        "wqp: %d site(s); %d carry a latest %s value",
        len(feats),
        sum(1 for f in feats if f["properties"]["value"] is not None),
        characteristic,
    )
    return feats
