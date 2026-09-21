"""USGS NWIS stream-gauge hooks: the mode switch, then the dataretrieval read.

``pre_resolve`` derives a ``_mode`` param pre-cache-key, so the declarative surface can
pin a per-mode column schema and units; ``read`` calls ``dataretrieval.nwis`` (IV then
the expanded Site record) and joins them, so every reading carries the gauge's own
zero. IV empty degrades to station locations from the Site call; both empty is the
source's honest no-stations refusal."""

from __future__ import annotations

import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook

__all__ = ["resolve", "read"]

_PARAM_DISCHARGE = "00060"
_PARAM_GAGE_HEIGHT = "00065"
_PARAM_TEMPERATURE = "00010"
_PARAMETER_CD = f"{_PARAM_DISCHARGE},{_PARAM_GAGE_HEIGHT}"

#: The value and window columns each NWIS parameter code is read into. A code
#: with no columns here is a measurement this source publishes nowhere, so the
#: ask refuses it rather than fetching a series that lands in no column.
_COLUMNS: dict[str, tuple[str, str]] = {
    _PARAM_DISCHARGE: ("discharge_cfs", "time_series_csv"),
    _PARAM_GAGE_HEIGHT: ("gage_height_ft", "stage_series_csv"),
    _PARAM_TEMPERATURE: ("water_temp_c", "temp_series_csv"),
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


def _map_http_error(spec: SourceSpec, exc: Exception) -> None:
    prefix = spec.error_code_prefix
    status = getattr(exc, "status_code", None)
    if status == 400:
        raise router_input_error(prefix, f"upstream rejected the request (HTTP 400): {exc}", spec.input_error_suffix)
    raise router_upstream_error(prefix, f"{type(exc).__name__}: {exc}")


def _number(raw: Any) -> float | None:
    """A site field as a float, or None. A frame column with any missing cell holds
    NaN where the record published nothing, and NaN is not a datum."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _text(raw: Any) -> str | None:
    """A site field as text, or None. NaN reads as the string "nan" unless caught."""
    if raw is None or (isinstance(raw, float) and not math.isfinite(raw)):
        return None
    return str(raw).strip() or None


def _feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def _readings_by_site(iv_df: Any, parameter_cd: str, mode: str) -> dict[str, dict[str, Any]]:
    """Group ``get_iv``'s long DataFrame (one row per timestep) by ``site_no`` into
    one record per site, latest-value columns for ``instantaneous`` and full inline
    series + rollups for ``hydrograph``."""
    by_site: dict[str, dict[str, Any]] = {}
    if iv_df is None or len(iv_df) == 0 or "site_no" not in iv_df.columns:
        return by_site
    codes = [c for c in parameter_cd.split(",") if c in _COLUMNS]
    for site_no, sub in iv_df.groupby("site_no"):
        site_no = str(site_no)
        rec = by_site.setdefault(site_no, {
            "site_no": site_no, "discharge_cfs": None, "gage_height_ft": None,
            "water_temp_c": None, "reading_dt": None,
        })
        if mode == "hydrograph":
            rec.setdefault("time_series_csv", "")
            rec.setdefault("stage_series_csv", "")
            rec.setdefault("temp_series_csv", "")
            rec.setdefault("time_start", None)
            rec.setdefault("time_end", None)
            rec.setdefault("n_timesteps", 0)
            rec.setdefault("discharge_min_cfs", None)
            rec.setdefault("discharge_max_cfs", None)
            rec.setdefault("discharge_mean_cfs", None)
        for code in codes:
            if code not in sub.columns:
                continue
            value_column, series_column = _COLUMNS[code]
            col = sub[code].dropna()
            col = col[col > -999990.0]
            if col.empty:
                continue
            if mode == "instantaneous":
                idx = col.index.max()
                rec[value_column] = float(col.loc[idx])
                dt_str = idx.strftime("%Y-%m-%dT%H:%M:%S%z") if hasattr(idx, "strftime") else str(idx)
                if not rec["reading_dt"]:
                    rec["reading_dt"] = dt_str
            else:
                pairs = sorted((idx.strftime("%Y-%m-%dT%H:%M:%S%z") if hasattr(idx, "strftime") else str(idx), float(v))
                                for idx, v in col.items())
                rec[series_column] = "\n".join(f"{t},{v:.6f}" for t, v in pairs) + "\n"
                rec[value_column] = pairs[-1][1]
                if code == _PARAM_DISCHARGE:
                    vals = [v for _t, v in pairs]
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
    join on ``site_no``: the readings carry each site's own zero. An empty IV
    degrades to the station locations; both empty is the honest no-stations
    refusal."""
    import dataretrieval.nwis as nwis
    from dataretrieval.exceptions import DataRetrievalError

    sc = spec.error_code_prefix
    sel = _selector_kwargs(params.get("state_code"), params.get("bbox"))
    parameter_cd = str(params.get("parameter") or _PARAMETER_CD)
    mode = params.get("_mode", "instantaneous")
    window = params.get("window")
    window_kwargs: dict[str, str] = {}
    if isinstance(window, str):
        window_kwargs["period"] = window
    elif isinstance(window, (list, tuple)):
        window_kwargs["start"] = str(window[0])
        window_kwargs["end"] = str(window[1])

    try:
        iv_df, _md = nwis.get_iv(
            parameterCd=parameter_cd, siteStatus="active", multi_index=False,
            **sel, **window_kwargs,
        )
    except DataRetrievalError as exc:
        _map_http_error(spec, exc)
    try:
        site_df, _md2 = nwis.get_info(
            parameterCd=parameter_cd, siteStatus="active", hasDataTypeCd="iv",
            siteOutput="expanded", **sel,
        )
    except DataRetrievalError as exc:
        _map_http_error(spec, exc)

    readings = _readings_by_site(iv_df, parameter_cd, mode)

    sites: dict[str, dict[str, Any]] = {}
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
            sites[site_no] = {
                "site_name": _text(rd.get("station_nm")) or "",
                "lon": lon, "lat": lat,
                "gauge_datum_ft": _number(rd.get("alt_va")),
                "vertical_datum": _text(rd.get("alt_datum_cd")),
            }

    if not readings:
        if not sites:
            raise router_empty_error(
                sc, "no active gauges in scope from the readings service or the "
                    "site service", spec.empty_error_suffix)
        # Fallback: station LOCATIONS only, every reading column null.
        cols = _COLUMNS_BY_MODE[mode]
        feats = []
        for site_no, loc in sites.items():
            props = {c: None for c in cols}
            props["site_no"] = site_no
            props["site_name"] = loc["site_name"]
            props["gauge_datum_ft"] = loc["gauge_datum_ft"]
            props["vertical_datum"] = loc["vertical_datum"]
            feats.append(_feature(loc["lon"], loc["lat"], props))
        return feats

    cols = _COLUMNS_BY_MODE[mode]
    feats = []
    for site_no, rec in readings.items():
        loc = sites.get(site_no)
        if loc is None:
            continue  # a reading with no site location has nowhere to be a Point
        rec["site_name"] = loc["site_name"]
        rec["gauge_datum_ft"] = loc["gauge_datum_ft"]
        rec["vertical_datum"] = loc["vertical_datum"]
        props = {c: rec.get(c) for c in cols}
        feats.append(_feature(loc["lon"], loc["lat"], props))
    if not feats:
        raise router_empty_error(
            sc, "no active gauges in scope from the readings service or the "
                "site service", spec.empty_error_suffix)
    return feats


_COLUMNS_BY_MODE: dict[str, tuple[str, ...]] = {
    "instantaneous": (
        "site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
        "reading_dt", "gauge_datum_ft", "vertical_datum",
    ),
    "hydrograph": (
        "site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
        "reading_dt", "time_series_csv", "stage_series_csv", "temp_series_csv",
        "time_start", "time_end", "n_timesteps", "discharge_min_cfs",
        "discharge_max_cfs", "discharge_mean_cfs", "gauge_datum_ft", "vertical_datum",
    ),
}
