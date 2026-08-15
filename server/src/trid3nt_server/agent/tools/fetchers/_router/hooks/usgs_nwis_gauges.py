"""USGS NWIS stream-gauge hooks: the last flood-seam twin fold.

fetch_usgs_nwis_gauges has two blockers the declarative surface could not carry,
both resolved here as PURE hooks over the ``http_json`` parse_fallback executor:

1. OUTPUT-SCHEMA switch by temporal-window presence. The FGB property schema is
   5-field (latest instantaneous) OR 12-field (a full discharge hydrograph with an
   inline ``time_series_csv``). ``usgs_nwis.resolve`` (pre_resolve) derives a
   ``_mode`` param (instantaneous / hydrograph) pre-cache-key; ``properties_by_param``
   pins the per-mode column schema and ``style_preset_by_param`` / ``units_by_param``
   the per-mode stamps -- one declarative switch keyed on the derived mode.

2. The IV WaterML-JSON -> Site-service RDB cross-parser FALLBACK. In instantaneous
   mode ``usgs_nwis.build_request`` emits an ORDERED [IV, Site] plan pair; the
   parse_fallback executor tries them in order, and ``usgs_nwis.parse`` self-detects
   the payload (JSON body -> IV parse; RDB text -> Site parse) so a 404/empty IV body
   degrades to the Site locations. Hydrograph mode emits [IV-window] only (the Site
   service has no readings). All-empty -> the honest NWIS_GAUGES_NO_STATIONS.

The spatial-selector cross-param gate (state_code XOR bbox, state wins, the ~25 deg^2
bbox area cap) and the temporal-window resolver (period-wins, both-or-neither dates,
120-day cap) also live in ``usgs_nwis.resolve`` -- the twin's body validation,
reproduced pre-cache / pre-network.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from . import RequestPlan, register_hook

_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"
_PARAM_DISCHARGE = "00060"
_PARAM_GAGE_HEIGHT = "00065"
_PARAMETER_CD = f"{_PARAM_DISCHARGE},{_PARAM_GAGE_HEIGHT}"
_MAX_BBOX_SQ_DEG = 24.5
_MAX_WINDOW_DAYS = 120
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

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


# --------------------------------------------------------------------------- #
# pre_resolve: spatial-selector + temporal-window resolution -> _mode.
# --------------------------------------------------------------------------- #


def _resolve_window(sc: str, sfx: str, start_date: Any, end_date: Any, period: Any):
    """The twin's ``_resolve_window``: None | ISO period str | (start, end) dates."""
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


@register_hook("usgs_nwis.resolve")
def resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the spatial selector + temporal window + ``_mode`` (pure, pre-cache-key).

    Merged into params BEFORE read_through so the resolved selector / window / mode
    enter the cache key and the executor + LayerURI stamps read them. Raises the twin's
    typed INPUT_ERROR / BBOX_TOO_LARGE on a bad selector, pre-network.
    """
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
        "_mode": mode,
        # Collapse the raw temporal params into the resolved window for the cache key
        # (the twin keys on the resolved window only, not period-vs-dates form).
        "start_date": None,
        "end_date": None,
        "period": None,
    }


# --------------------------------------------------------------------------- #
# build_request: ordered plan(s) for the parse_fallback executor.
# --------------------------------------------------------------------------- #


def _selector_params(state_code: str | None, bbox: list[float] | None) -> dict[str, str]:
    if state_code is not None:
        return {"stateCd": state_code}
    w, s, e, n = bbox  # type: ignore[misc]
    return {"bBox": f"{w},{s},{e},{n}"}


@register_hook("usgs_nwis.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """Ordered request plans. Instantaneous: [IV, Site]. Hydrograph: [IV-window]."""
    state_code = params.get("state_code")
    bbox = params.get("bbox")
    window = params.get("window")
    sel = _selector_params(state_code, bbox)
    headers = {"User-Agent": _USER_AGENT}

    iv_params: dict[str, str] = {"format": "json", "siteStatus": "active", "parameterCd": _PARAMETER_CD, **sel}
    if isinstance(window, str):
        iv_params["period"] = window
    elif isinstance(window, (list, tuple)):
        iv_params["startDT"] = str(window[0])
        iv_params["endDT"] = str(window[1])
    iv_plan = RequestPlan(url=_IV_URL, params=iv_params, headers=headers)

    if window is not None:
        # Hydrograph mode: the Site service has no readings, so a miss is an honest
        # no-stations error (no fallback plan).
        return [iv_plan]

    site_params = {"format": "rdb", "siteStatus": "active", "hasDataTypeCd": "iv", "parameterCd": _PARAMETER_CD, **sel}
    site_plan = RequestPlan(url=_SITE_URL, params=site_params, headers=headers)
    return [iv_plan, site_plan]


# --------------------------------------------------------------------------- #
# parse_response: self-detecting IV-JSON / IV-window-JSON / Site-RDB decode.
# --------------------------------------------------------------------------- #


def _feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def _parse_iv_json(sc: str, raw: bytes) -> list[dict[str, Any]]:
    """Latest-instantaneous IV WaterML-JSON -> 5-field Point features (twin parity)."""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"USGS IV response is not valid JSON: {exc}")
    series = (obj.get("value") or {}).get("timeSeries") or []
    by_site: dict[str, dict[str, Any]] = {}
    for ts in series:
        source = ts.get("sourceInfo") or {}
        site_codes = source.get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "").strip()
        if not site_no:
            continue
        site_name = str(source.get("siteName") or "").strip()
        geo = (source.get("geoLocation") or {}).get("geogLocation") or {}
        try:
            lat = float(geo.get("latitude"))
            lon = float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        var_codes = (ts.get("variable") or {}).get("variableCode") or []
        param = str(var_codes[0].get("value") or "").strip() if var_codes else ""
        latest_val: float | None = None
        latest_dt: str | None = None
        values_blocks = ts.get("values") or []
        if values_blocks:
            samples = values_blocks[0].get("value") or []
            if samples:
                last = samples[-1]
                try:
                    fv = float(last.get("value"))
                    if fv > -999990.0:
                        latest_val = fv
                        latest_dt = str(last.get("dateTime") or "") or None
                except (TypeError, ValueError):
                    latest_val = None
        rec = by_site.setdefault(site_no, {"site_no": site_no, "site_name": site_name, "lon": lon, "lat": lat,
                                           "discharge_cfs": None, "gage_height_ft": None, "reading_dt": None})
        if site_name and not rec.get("site_name"):
            rec["site_name"] = site_name
        if param == _PARAM_DISCHARGE and latest_val is not None:
            rec["discharge_cfs"] = latest_val
            if latest_dt and not rec["reading_dt"]:
                rec["reading_dt"] = latest_dt
        elif param == _PARAM_GAGE_HEIGHT and latest_val is not None:
            rec["gage_height_ft"] = latest_val
            if latest_dt and not rec["reading_dt"]:
                rec["reading_dt"] = latest_dt
    return [_feature(r["lon"], r["lat"], {k: r[k] for k in ("site_no", "site_name", "discharge_cfs", "gage_height_ft", "reading_dt")})
            for r in by_site.values()]


def _parse_iv_json_window(sc: str, raw: bytes) -> list[dict[str, Any]]:
    """Windowed IV WaterML-JSON -> 12-field hydrograph Point features (twin parity)."""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"USGS IV (window) response is not valid JSON: {exc}")
    series = (obj.get("value") or {}).get("timeSeries") or []
    by_site: dict[str, dict[str, Any]] = {}
    for ts in series:
        source = ts.get("sourceInfo") or {}
        site_codes = source.get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "").strip()
        if not site_no:
            continue
        site_name = str(source.get("siteName") or "").strip()
        geo = (source.get("geoLocation") or {}).get("geogLocation") or {}
        try:
            lat = float(geo.get("latitude"))
            lon = float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        var_codes = (ts.get("variable") or {}).get("variableCode") or []
        param = str(var_codes[0].get("value") or "").strip() if var_codes else ""
        samples: list[tuple[str, float]] = []
        values_blocks = ts.get("values") or []
        if values_blocks:
            for s in values_blocks[0].get("value") or []:
                try:
                    fv = float(s.get("value"))
                except (TypeError, ValueError):
                    continue
                if fv <= -999990.0:
                    continue
                dt_s = str(s.get("dateTime") or "").strip()
                if not dt_s:
                    continue
                samples.append((dt_s, fv))
        rec = by_site.setdefault(site_no, {"site_no": site_no, "site_name": site_name, "lon": lon, "lat": lat,
                                           "discharge_cfs": None, "gage_height_ft": None, "reading_dt": None,
                                           "time_series_csv": "", "time_start": None, "time_end": None, "n_timesteps": 0,
                                           "discharge_min_cfs": None, "discharge_max_cfs": None, "discharge_mean_cfs": None})
        if site_name and not rec.get("site_name"):
            rec["site_name"] = site_name
        if not samples:
            continue
        if param == _PARAM_DISCHARGE:
            rec["time_series_csv"] = "\n".join(f"{dt_s},{v:.6f}" for dt_s, v in samples) + "\n"
            vals = [v for _dt_s, v in samples]
            rec["n_timesteps"] = len(vals)
            rec["time_start"] = samples[0][0]
            rec["time_end"] = samples[-1][0]
            rec["discharge_min_cfs"] = min(vals)
            rec["discharge_max_cfs"] = max(vals)
            rec["discharge_mean_cfs"] = sum(vals) / len(vals)
            rec["discharge_cfs"] = samples[-1][1]
            rec["reading_dt"] = samples[-1][0]
        elif param == _PARAM_GAGE_HEIGHT:
            rec["gage_height_ft"] = samples[-1][1]
            if rec["reading_dt"] is None:
                rec["reading_dt"] = samples[-1][0]
    cols = ("site_no", "site_name", "discharge_cfs", "gage_height_ft", "reading_dt", "time_series_csv",
            "time_start", "time_end", "n_timesteps", "discharge_min_cfs", "discharge_max_cfs", "discharge_mean_cfs")
    return [_feature(r["lon"], r["lat"], {k: r[k] for k in cols}) for r in by_site.values()]


def _parse_site_rdb(sc: str, raw: bytes) -> list[dict[str, Any]]:
    """Site-service RDB (tab-delimited) -> station-location Point features (5-field)."""
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(data_lines) < 3:
        return []
    header = data_lines[0].split("\t")
    try:
        i_site = header.index("site_no")
        i_lat = header.index("dec_lat_va")
        i_lon = header.index("dec_long_va")
    except ValueError:
        raise router_upstream_error(sc, f"USGS Site RDB missing required columns; got header {header[:12]}")
    i_name = header.index("station_nm") if "station_nm" in header else None
    features: list[dict[str, Any]] = []
    for row in data_lines[2:]:
        cols = row.split("\t")
        if len(cols) <= max(i_site, i_lat, i_lon):
            continue
        site_no = cols[i_site].strip()
        if not site_no:
            continue
        try:
            lat = float(cols[i_lat])
            lon = float(cols[i_lon])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        site_name = cols[i_name].strip() if (i_name is not None and len(cols) > i_name) else ""
        features.append(_feature(lon, lat, {"site_no": site_no, "site_name": site_name,
                                            "discharge_cfs": None, "gage_height_ft": None, "reading_dt": None}))
    return features


@register_hook("usgs_nwis.parse")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Self-detecting decode of ONE body (parse_fallback calls this per plan).

    A JSON body ({...}) is an IV WaterML-JSON payload (window parser in hydrograph
    mode, latest parser otherwise); anything else is a Site-service RDB body. An
    empty body ([] -> triggers the executor's next plan / honest NO_STATIONS).
    """
    sc = spec.error_code_prefix
    body = bodies[0] if bodies else b""
    stripped = body.lstrip()
    if not stripped:
        return []
    if stripped[:1] == b"{":
        if params.get("_mode") == "hydrograph":
            return _parse_iv_json_window(sc, body)
        return _parse_iv_json(sc, body)
    return _parse_site_rdb(sc, body)
