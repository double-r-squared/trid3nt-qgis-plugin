"""USGS NWIS stream-gauge hooks: the mode switch, then the dataretrieval read.

``pre_resolve`` derives a ``_mode`` param pre-cache-key; ``read`` calls ``dataretrieval.nwis`` (IV then
the expanded Site record) and joins them: a feature is the site record under NWIS's own field names, and
what the read produced (the latest reading and its qualifier, the window's rows) rides beside it. IV empty
degrades to station locations from the Site call; both empty is the source's honest no-stations refusal."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error
from ..._router.hooks import register_hook

__all__ = ["resolve", "read"]

_PARAM_DISCHARGE = "00060"
_PARAM_GAGE_HEIGHT = "00065"
_PARAM_TEMPERATURE = "00010"
_PARAMETER_CD = f"{_PARAM_DISCHARGE},{_PARAM_GAGE_HEIGHT}"

#: The value, window and qualifier columns each NWIS parameter code is read into. A code
#: with no columns here is a measurement this source publishes nowhere, so the
#: ask refuses it rather than fetching a series that lands in no column.
_COLUMNS: dict[str, tuple[str, str, str]] = {
    _PARAM_DISCHARGE: ("discharge_cfs", "discharge_series", "discharge_cd"),
    _PARAM_GAGE_HEIGHT: ("gage_height_ft", "stage_series", "gage_height_cd"),
    _PARAM_TEMPERATURE: ("water_temp_c", "temp_series", "water_temp_cd"),
}
_MAX_BBOX_SQ_DEG = 24.5
_MAX_WINDOW_DAYS = 120

_VALID_STATE_CODES: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC", "PR", "VI", "GU", "AS", "MP",
    }
)


def _resolve_window(sc: str, sfx: str, start_date: Any, end_date: Any, period: Any):
    """Resolve the window: None, an ISO period string, or a (start, end) date pair."""
    if period is not None and str(period).strip() != "":
        p = str(period).strip().upper()
        if not re.fullmatch(r"P(?:\d+[YMWD])*(?:T(?:\d+[HMS])+)?", p) or p == "P":
            raise router_input_error(
                sc, f"period={period!r} is not a valid ISO-8601 duration (e.g. 'P7D', 'P1M', 'PT6H')", sfx)
        return p
    if start_date is None and end_date is None:
        return None
    if start_date is None or end_date is None:
        raise router_input_error(
            sc, "a hydrograph window requires BOTH start_date and end_date (ISO YYYY-MM-DD), "
                f"or a single relative period (e.g. period='P7D'); got start_date={start_date!r}, end_date={end_date!r}", sfx)
    import datetime as _dt
    try:
        d0 = _dt.date.fromisoformat(str(start_date))
    except ValueError as exc:
        raise router_input_error(sc, f"start_date={start_date!r} is not a valid ISO date (YYYY-MM-DD): {exc}", sfx)
    try:
        d1 = _dt.date.fromisoformat(str(end_date))
    except ValueError as exc:
        raise router_input_error(sc, f"end_date={end_date!r} is not a valid ISO date (YYYY-MM-DD): {exc}", sfx)
    if d0 > d1:
        raise router_input_error(sc, f"start_date must be <= end_date; got start={d0}, end={d1}", sfx)
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_WINDOW_DAYS:
        raise router_input_error(
            sc, f"hydrograph window {n_days} days exceeds the {_MAX_WINDOW_DAYS}-day cap; request a shorter window or call in chunks", sfx)
    return (d0.isoformat(), d1.isoformat())


def _resolve_parameter(sc: str, sfx: str, parameter: Any) -> str | None:
    """The parameter codes this call asks NWIS for, or ``None`` for the pair the
    gauge network is read by default."""
    codes = [code.strip() for code in str(parameter or "").split(",") if code.strip()]
    if not codes:
        return None
    unknown = [code for code in codes if code not in _COLUMNS]
    if unknown:
        raise router_input_error(
            sc, f"parameter={parameter!r} names {', '.join(unknown)}, which this "
                f"source reads into no column; it publishes {', '.join(sorted(_COLUMNS))} "
                "(discharge, gage height, water temperature)", sfx)
    return ",".join(codes)


@register_hook("usgs_nwis.resolve")
def resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the spatial selector, temporal window and mode, pure and pre-cache-key,
    so all three enter the cache key and the LayerURI stamps. A bad selector raises a
    typed input or bbox-too-large error pre-network."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    state_code = params.get("state_code")
    bbox = params.get("bbox")

    resolved_state: str | None = None
    resolved_bbox: list[float] | None = None
    if state_code is not None and str(state_code).strip() != "":
        s = str(state_code).strip().upper()
        if s not in _VALID_STATE_CODES:
            raise router_input_error(
                sc, f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
                    "expected one of e.g. 'WA', 'FL', 'CA' (USGS NWIS stateCd)", sfx)
        resolved_state = s
    elif bbox is not None:
        # bbox was validated + quantized (round_6dp) by validate_params. The area cap
        # only applies WITHOUT a state selector (stateCd has no area limit).
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area > _MAX_BBOX_SQ_DEG:
            raise router_input_error(
                sc, f"bbox area {area:.1f} deg^2 exceeds the USGS NWIS bBox limit (~25 deg^2); "
                    "a whole-state bbox (e.g. Washington ~28 deg^2) will 400. For a state-level "
                    "query pass state_code (e.g. state_code='WA') instead -- stateCd has no area "
                    f"limit -- or re-issue with a smaller bbox (<= ~{_MAX_BBOX_SQ_DEG:.0f} deg^2).",
                "BBOX_TOO_LARGE")
        resolved_bbox = [float(v) for v in bbox]
    else:
        raise router_input_error(
            sc, "fetch_usgs_nwis_gauges requires a spatial selector: pass state_code "
                "(2-letter USPS, e.g. 'WA') for a state-level query, or bbox=(west, south, east, north) "
                "for an area query.", sfx)

    window = _resolve_window(sc, sfx, params.get("start_date"), params.get("end_date"), params.get("period"))
    mode = "hydrograph" if window is not None else "instantaneous"
    return {
        "state_code": resolved_state,
        "bbox": resolved_bbox,
        "window": list(window) if isinstance(window, tuple) else window,
        "parameter": _resolve_parameter(sc, sfx, params.get("parameter")),
        "_mode": mode,
        # Collapse the raw temporal params into the resolved window for the cache key
        # The key carries the resolved window only, not the period-vs-dates form.
        "start_date": None,
        "end_date": None,
        "period": None,
    }


def _selector_kwargs(state_code: str | None, bbox: list[float] | None) -> dict[str, str]:
    if state_code is not None:
        return {"stateCd": state_code}
    w, s, e, n = bbox  # type: ignore[misc]
    return {"bBox": f"{w},{s},{e},{n}"}


def _number(raw: Any) -> float | None:
    """A site field as a float, or None. A frame column with any missing cell holds
    NaN where the record published nothing, and NaN is not a datum."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _stated(raw: Any) -> Any:
    """A site field as the record states it: NaN, the frame's hole, and a numpy scalar read as None and a plain value."""
    if raw is None or (isinstance(raw, float) and not math.isfinite(raw)):
        return None
    return raw.item() if hasattr(raw, "item") else raw


def _feature(lon: float, lat: float, record: dict[str, Any], produced: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": record, "produced": produced}


def _column_of(sub: Any, code: str) -> str | None:
    """The one column a site reports ``code`` under: the bare code, or the one ``<code>_<description>`` dataretrieval
    names a site-described time series; a site reporting it under several holds no one reading, and none is read."""
    held = [name for name in sub.columns
            if (name == code or (str(name).startswith(f"{code}_") and not str(name).endswith("_cd")))
            and sub[name].notna().any()]
    return held[0] if len(held) == 1 else None


def _utc_days(window: Any) -> tuple[Any, Any, dict[str, str]]:
    """A date pair's UTC span [first day 00:00Z, the day after the last) and the dates NWIS is asked.

    NWIS reads a date pair as the SITE's local days, so it is asked one day wider each side and the readings are cut
    on their own timestamps to the UTC days asked."""
    import datetime as _dt

    d0, d1 = (_dt.date.fromisoformat(str(v)) for v in window)
    day = _dt.timedelta(days=1)
    lo = _dt.datetime.combine(d0, _dt.time(), tzinfo=_dt.timezone.utc)
    hi = _dt.datetime.combine(d1 + day, _dt.time(), tzinfo=_dt.timezone.utc)
    return lo, hi, {"start": (d0 - day).isoformat(), "end": (d1 + day).isoformat()}


def _readings_by_site(iv_df: Any, parameter_cd: str, mode: str,
                      cut: tuple[Any, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Group ``get_iv``'s long DataFrame (one row per timestep) by ``site_no`` into what each site's
    reading produced: latest value and qualifier columns for ``instantaneous``, full inline
    series (JSON of ``datetime``, ``value``, ``cd`` readings) + rollups for ``hydrograph``; ``cut`` keeps the readings in [lo, hi) UTC."""
    by_site: dict[str, dict[str, Any]] = {}
    if iv_df is None or len(iv_df) == 0 or "site_no" not in iv_df.columns:
        return by_site
    if cut is not None:
        import pandas as pd

        stamps = pd.DatetimeIndex(iv_df.index)
        stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
        iv_df = iv_df[(stamps >= cut[0]) & (stamps < cut[1])]
        if len(iv_df) == 0:
            return by_site
    codes = [c for c in parameter_cd.split(",") if c in _COLUMNS]
    for site_no, sub in iv_df.groupby("site_no"):
        site_no = str(site_no)
        rec = by_site.setdefault(site_no, {
            "discharge_cfs": None, "gage_height_ft": None, "water_temp_c": None, "reading_dt": None,
            "discharge_cd": None, "gage_height_cd": None, "water_temp_cd": None,
        })
        if mode == "hydrograph":
            rec.setdefault("discharge_series", "[]")
            rec.setdefault("stage_series", "[]")
            rec.setdefault("temp_series", "[]")
            rec.setdefault("time_start", None)
            rec.setdefault("time_end", None)
            rec.setdefault("n_timesteps", 0)
            rec.setdefault("discharge_min_cfs", None)
            rec.setdefault("discharge_max_cfs", None)
            rec.setdefault("discharge_mean_cfs", None)
        for code in codes:
            named = _column_of(sub, code)
            if named is None:
                continue
            value_column, series_column, cd_column = _COLUMNS[code]
            kept = sub[sub[named].notna() & (sub[named] > -999990.0)]
            if kept.empty:
                continue
            marks = kept[f"{named}_cd"].tolist() if f"{named}_cd" in kept.columns else [None] * len(kept)
            rows = [(idx, float(v), "" if cd is None or cd != cd else str(cd).strip())
                    for (idx, v), cd in zip(kept[named].items(), marks)]
            if mode == "instantaneous":
                idx, rec[value_column], cd = max(rows, key=lambda row: row[0])
                rec[cd_column] = cd or None
                dt_str = idx.strftime("%Y-%m-%dT%H:%M:%S%z") if hasattr(idx, "strftime") else str(idx)
                if not rec["reading_dt"]:
                    rec["reading_dt"] = dt_str
            else:
                pairs = sorted((idx.strftime("%Y-%m-%dT%H:%M:%S%z") if hasattr(idx, "strftime") else str(idx), v, cd)
                               for idx, v, cd in rows)
                rec[series_column] = json.dumps([{"datetime": t, "value": v, "cd": cd} for t, v, cd in pairs])
                rec[value_column] = pairs[-1][1]
                rec[cd_column] = pairs[-1][2] or None
                if code == _PARAM_DISCHARGE:
                    vals = [v for _t, v, _cd in pairs]
                    rec["n_timesteps"] = len(vals)
                    rec["time_start"] = pairs[0][0]
                    rec["time_end"] = pairs[-1][0]
                    rec["discharge_min_cfs"] = min(vals)
                    rec["discharge_max_cfs"] = max(vals)
                    rec["discharge_mean_cfs"] = sum(vals) / len(vals)
                    rec["reading_dt"] = pairs[-1][0]
                elif rec["reading_dt"] is None:
                    rec["reading_dt"] = pairs[-1][0]
    return by_site


@register_hook("usgs_nwis.read")
def read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict[str, Any]]:
    """Fetch through ``dataretrieval.nwis`` (IV, then the expanded Site record) and
    join on ``site_no``: the site record carries each site's own zero. An empty IV
    degrades to the station locations; both empty is the honest no-stations
    refusal."""
    import dataretrieval.nwis as nwis

    from ..._router.hooks.dataretrieval import retrieve

    sc = spec.error_code_prefix
    sel = _selector_kwargs(params.get("state_code"), params.get("bbox"))
    parameter_cd = str(params.get("parameter") or _PARAMETER_CD)
    mode = params.get("_mode", "instantaneous")
    window = params.get("window")
    window_kwargs: dict[str, str] = {}
    cut = None
    if isinstance(window, str):
        window_kwargs["period"] = window
    elif isinstance(window, (list, tuple)):
        lo, hi, window_kwargs = _utc_days(window)
        cut = (lo, hi)

    iv_df, _md = retrieve(
        spec, nwis.get_iv, parameterCd=parameter_cd, siteStatus="active",
        multi_index=False, **sel, **window_kwargs,
    )
    site_df, _md2 = retrieve(
        spec, nwis.get_info, parameterCd=parameter_cd, siteStatus="active",
        hasDataTypeCd="iv", siteOutput="expanded", **sel,
    )

    readings = _readings_by_site(iv_df, parameter_cd, mode, cut)

    sites: dict[str, tuple[float, float, dict[str, Any]]] = {}
    if site_df is not None and len(site_df):
        for rd in site_df.to_dict(orient="records"):
            site_no = str(rd.get("site_no") or "").strip()
            if not site_no:
                continue
            try:
                lat = float(rd.get("dec_lat_va"))
                lon = float(rd.get("dec_long_va"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            # dataretrieval adds a ``geometry`` of its own from the two decimal coordinates the record keeps.
            sites[site_no] = (lon, lat, {str(k): _stated(v) for k, v in rd.items() if k != "geometry"})

    columns = _READING_COLUMNS[mode]
    if not readings:
        if not sites:
            raise router_empty_error(
                sc, "no active gauges in scope from the readings service or the "
                    "site service", spec.empty_error_suffix)
        # Fallback: station LOCATIONS only, every reading column null.
        return [_feature(lon, lat, record, dict.fromkeys(columns))
                for lon, lat, record in sites.values()]

    feats = []
    for site_no, rec in readings.items():
        loc = sites.get(site_no)
        if loc is None:
            continue  # a reading with no site location has nowhere to be a Point
        feats.append(_feature(loc[0], loc[1], loc[2], {c: rec.get(c) for c in columns}))
    if not feats:
        raise router_empty_error(
            sc, "no active gauges in scope from the readings service or the "
                "site service", spec.empty_error_suffix)
    return feats


#: What a reading produces beside the site record, per mode.
_READING_COLUMNS: dict[str, tuple[str, ...]] = {
    "instantaneous": (
        "discharge_cfs", "gage_height_ft", "water_temp_c", "discharge_cd", "gage_height_cd",
        "water_temp_cd", "reading_dt",
    ),
    "hydrograph": (
        "discharge_cfs", "gage_height_ft", "water_temp_c", "discharge_cd", "gage_height_cd",
        "water_temp_cd", "reading_dt", "discharge_series", "stage_series", "temp_series",
        "time_start", "time_end", "n_timesteps", "discharge_min_cfs",
        "discharge_max_cfs", "discharge_mean_cfs",
    ),
}
