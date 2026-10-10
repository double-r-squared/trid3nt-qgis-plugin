"""station-timeseries executor: a station catalog becomes a point FGB.

Catalog-discover under a bbox filter and a cap, then a per-station data loop, then
one Point per station carrying the rollups and the inline series. One station's
failure never aborts the bbox; every station empty raises the typed ``*_EMPTY``."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_upstream_error
from ..transport import get_bytes, get_client, is_staged_uri
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.station_timeseries"
)

__all__ = [
    "stations_to_point_fgb",
    "fetch_station_records",
    "snapshots_to_point_fgb",
    "coops_currents_select",
    "execute",
]


def _normalize_time(t: Any, mode: str | None) -> Any:
    """Normalize a timestamp per ``time_normalize``; ``iso8601z`` rewrites only a value carrying a space."""
    if mode == "iso8601z" and isinstance(t, str) and " " in t:
        return t.replace(" ", "T") + "Z"
    return t


def _as_date(v: Any) -> Any:
    """Coerce a ``YYYY-MM-DD`` string to a ``date`` so a ``{start:%Y%m%d}`` template can strftime."""
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return v

#: The point-FGB column schema; ``time_series_csv`` carries the inline per-station series.
_COLUMNS = [
    "station_id", "station_name", "lon", "lat", "product", "datum",
    "time_start", "time_end", "n_timesteps",
    "wl_min_m", "wl_max_m", "wl_mean_m", "time_series_csv",
]


def _points_to_fgb(records: list[dict[str, Any]], rows: list[dict[str, Any]],
                   spec: SourceSpec) -> bytes:
    """One Point feature per station, its row as the properties."""
    return features_to_fgb_bytes(
        [{"type": "Feature",
          "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
          "properties": row} for rec, row in zip(records, rows)],
        spec,
    )


def _get_json(spec: SourceSpec, url: str, params: dict[str, Any]) -> Any:
    body, _ct, _url = get_bytes(get_client(), url, params=params,
                                headers={"User-Agent": spec.auth.user_agent})
    return json.loads(body)


def _format_request(template: dict[str, Any], fmt: dict[str, Any]) -> dict[str, Any]:
    req: dict[str, Any] = {}
    for k, v in template.items():
        try:
            req[k] = v.format(**fmt) if isinstance(v, str) else v
        except (KeyError, IndexError, ValueError):
            req[k] = v
    return req


def stations_to_point_fgb(
    records: list[dict[str, Any]],
    spec: SourceSpec,
    *,
    product: str = "water_level",
) -> bytes:
    """Serialize ``{station_id, station_name, lon, lat, rows}`` records to point-FGB bytes; an
    all-empty set raises ``*_EMPTY``, never a header-only FGB."""
    import numpy as np

    datum = spec.normalize.datum or "MLLW"
    time_norm = ((spec.ingest or {}).get("per_station") or {}).get("time_normalize")
    rows_out: list[dict[str, Any]] = []
    for rec in records:
        series = rec.get("rows") or []
        if not series:
            continue
        buf = io.StringIO()
        writer = csv.writer(buf)
        values: list[float] = []
        norm_ts: list[Any] = []
        for entry in series:
            t = _normalize_time(entry["t"], time_norm)
            v = entry["v"]
            writer.writerow([t, f"{v:.6f}"])
            values.append(v)
            norm_ts.append(t)
        ts_csv = buf.getvalue()
        rows_out.append({
            "station_id": rec["station_id"],
            "station_name": rec.get("station_name"),
            "lon": rec["lon"],
            "lat": rec["lat"],
            "product": product,
            "datum": datum,
            "time_start": norm_ts[0],
            "time_end": norm_ts[-1],
            "n_timesteps": len(values),
            "wl_min_m": float(np.nanmin(values)),
            "wl_max_m": float(np.nanmax(values)),
            "wl_mean_m": float(np.nanmean(values)),
            "time_series_csv": ts_csv,
        })

    if not rows_out:
        raise router_empty_error(
            spec.error_code_prefix,
            "no station carried data in the requested bbox/window",
            spec.empty_error_suffix,
        )

    return _points_to_fgb(rows_out, rows_out, spec)


def _guard_not_staged(spec: SourceSpec, url: str) -> None:
    """Refuse a staged ``s3://`` uri with a typed error: this executor talks a REST API."""
    if is_staged_uri(url):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"a staged s3:// uri is not readable by the station-timeseries executor: {url!r}",
        )


def _discover_stations(spec: SourceSpec, bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    ingest = spec.ingest or {}
    cat = ingest.get("station_catalog", {})
    lat_key = cat.get("lat_key", "lat")
    lon_key = cat.get("lon_key", "lng")
    id_key = cat.get("id_key", "id")
    name_key = cat.get("name_key", "name")
    rows_key = cat.get("rows_key", "stations")
    endpoint = spec.endpoints.get("catalog") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    _guard_not_staged(spec, url)
    try:
        body = _get_json(spec, url, dict(endpoint.query or {}))
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(spec.error_code_prefix, f"station catalog fetch failed: {exc}")
    stations = body.get(rows_key, []) if isinstance(body, dict) else []
    west, south, east, north = bbox
    out: list[dict[str, Any]] = []
    for s in stations:
        try:
            lat = float(s[lat_key]); lon = float(s[lon_key])
        except (KeyError, TypeError, ValueError):
            continue
        if west <= lon <= east and south <= lat <= north:
            out.append({"station_id": str(s.get(id_key)), "station_name": s.get(name_key),
                        "lon": lon, "lat": lat})
    max_stations = spec.gates.max_stations or 50
    return out[:max_stations]


def _fetch_station_series(spec: SourceSpec, station: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    ingest = spec.ingest or {}
    per = ingest.get("per_station", {})
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url_template or endpoint.url or ""
    _guard_not_staged(spec, url)
    product = params.get("product", "water_level")
    # A product's overrides replace the shared request's; a null override DROPS the shared key.
    req_tmpl = {k: v for k, v in
                {**per.get("request", {}),
                 **(per.get("request_by_product", {}).get(product) or {})}.items()
                if v is not None}
    # start/end are date objects so ``{start:%Y%m%d}`` strftimes to the datagetter's YYYYMMDD.
    fmt = {"id": station["station_id"], "product": product,
           "start": _as_date(params.get("start_date")), "end": _as_date(params.get("end_date"))}
    try:
        body = _get_json(spec, url, _format_request(req_tmpl, fmt))
    except Exception:  # noqa: BLE001 -- one bad station never aborts the bbox
        return []
    rows_keys = per.get("rows_key", ["data", "predictions"])
    if isinstance(rows_keys, str):
        rows_keys = [rows_keys]
    raw = None
    for rk in rows_keys:
        if isinstance(body, dict) and body.get(rk):
            raw = body[rk]
            break
    if not raw:
        return []
    time_key = per.get("time_key", "t")
    value_key = per.get("value_key", "v")
    out: list[dict[str, Any]] = []
    for r in raw:
        try:
            out.append({"t": r[time_key], "v": float(r[value_key])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_station_records(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    bbox = params["bbox"]
    stations = _discover_stations(spec, bbox)
    records: list[dict[str, Any]] = []
    for st in stations:
        rows = _fetch_station_series(spec, st, params)
        records.append({**st, "rows": rows})
    return records


# SNAPSHOT mode (``emit == "snapshot"``): one row per station, not the series path over a
# one-sample window: a current sample is a vector (speed and bearing; the sign of the major
# velocity picks flood or ebb), and a prediction pick is the row nearest now, not the last one.


def _snapshot_window(
    product: str, wcfg: dict[str, Any] | None, now: _dt.datetime
) -> tuple[_dt.date, _dt.date]:
    """The now-relative ``(begin, end)`` window: ``lookback`` is ``[now-days, now]``, ``lookahead`` ``[now, now+days]``."""
    cfg = (wcfg or {}).get(product) or {}
    days = int(cfg.get("days", 2))
    if cfg.get("mode") == "lookahead":
        return now.date(), (now + _dt.timedelta(days=days)).date()
    return (now - _dt.timedelta(days=days)).date(), now.date()


def _cur_to_dt(t: Any) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(str(t), "%Y-%m-%d %H:%M").replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _cur_iso(t: Any) -> str:
    s = str(t)
    return s.replace(" ", "T") + "Z" if " " in s else s


def coops_currents_select(
    body: dict[str, Any], product: str, now: _dt.datetime
) -> dict[str, Any] | None:
    """The CO-OPS currents snapshot transform: the latest valid observed row, or the predicted
    row nearest ``now`` with flood/ebb/slack read off the velocity sign, or ``None``."""
    import math

    if product == "currents_predictions":
        cp = body.get("current_predictions") or {}
        rows = cp.get("cp") if isinstance(cp, dict) else None
        if not rows:
            return None
        best: dict[str, Any] | None = None
        best_gap: float | None = None
        for row in rows:
            rdt = _cur_to_dt(row.get("Time", ""))
            if rdt is None:
                continue
            try:
                vmaj = float(row.get("Velocity_Major"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(vmaj):
                continue
            flow = str(row.get("Type", "")).lower()
            try:
                flood_dir = float(row.get("meanFloodDir"))
            except (TypeError, ValueError):
                flood_dir = float("nan")
            try:
                ebb_dir = float(row.get("meanEbbDir"))
            except (TypeError, ValueError):
                ebb_dir = float("nan")
            if vmaj > 0:
                direction = flood_dir
            elif vmaj < 0:
                direction = ebb_dir
            else:
                direction = flood_dir if math.isfinite(flood_dir) else ebb_dir
            if not math.isfinite(direction):
                direction = 0.0
            gap = abs((rdt - now).total_seconds())
            if best_gap is None or gap < best_gap:
                best_gap = gap
                try:
                    bin_idx = int(row.get("Bin"))
                except (TypeError, ValueError):
                    bin_idx = -1
                best = {
                    "speed_kn": abs(vmaj),
                    "direction_deg": direction % 360.0,
                    "datetime": _cur_iso(row.get("Time", "")),
                    "bin": bin_idx,
                    "flow_state": flow,
                }
        return best

    rows = body.get("data") or []
    best = None
    best_dt: _dt.datetime | None = None
    for row in rows:
        s_raw = row.get("s")
        d_raw = row.get("d")
        if s_raw in (None, "") or d_raw in (None, ""):
            continue
        try:
            speed = float(s_raw)
            direction = float(d_raw)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(speed) and math.isfinite(direction)):
            continue
        rdt = _cur_to_dt(row.get("t", ""))
        if rdt is None:
            continue
        if best_dt is None or rdt > best_dt:
            best_dt = rdt
            try:
                bin_idx = int(row.get("b"))
            except (TypeError, ValueError):
                bin_idx = -1
            best = {
                "speed_kn": speed,
                "direction_deg": direction % 360.0,
                "datetime": _cur_iso(row.get("t", "")),
                "bin": bin_idx,
                "flow_state": "",
            }
    return best


_SNAPSHOT_SELECTORS = {"coops_currents": coops_currents_select}


def _fetch_station_snapshot(
    spec: SourceSpec, station: dict[str, Any], params: dict[str, Any], now: _dt.datetime
) -> dict[str, Any] | None:
    ingest = spec.ingest or {}
    per = ingest.get("per_station", {})
    snap = per.get("snapshot", {})
    product = params.get("product", "currents")
    d0, d1 = _snapshot_window(product, snap.get("window"), now)
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url_template or endpoint.url or ""
    _guard_not_staged(spec, url)
    fmt = {"id": station["station_id"], "product": product, "start": d0, "end": d1}
    req = _format_request(dict(per.get("request", {})), fmt)
    for k, v in (snap.get("request_by_product", {}).get(product) or {}).items():
        req[k] = v
    try:
        body = _get_json(spec, url, req)
    except Exception:  # noqa: BLE001 -- one bad station never aborts the bbox
        return None
    if not isinstance(body, dict) or "error" in body:
        return None
    selector = _SNAPSHOT_SELECTORS.get(snap.get("transform"))
    if selector is None:
        return None
    picked = selector(body, product, now)
    if not picked:
        return None
    return {
        "station_id": station["station_id"],
        "station_name": station.get("station_name"),
        "lon": station["lon"],
        "lat": station["lat"],
        "product": product,
        **picked,
    }


def snapshots_to_point_fgb(
    records: list[dict[str, Any]], spec: SourceSpec, *, product: str = "currents"
) -> bytes:
    """Serialize per-station snapshot records to point-FGB bytes; an empty set raises ``*_EMPTY``."""
    if not records:
        raise router_empty_error(
            spec.error_code_prefix,
            "no station returned current data in the requested bbox/window",
            spec.empty_error_suffix,
        )

    snap = ((spec.ingest or {}).get("per_station") or {}).get("snapshot") or {}
    cols = [str(c) for c in (snap.get("columns") or list(records[0].keys()))]
    return _points_to_fgb(records, [{c: rec.get(c) for c in cols} for rec in records], spec)


def execute_snapshot(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    now = _dt.datetime.now(_dt.timezone.utc)
    stations = _discover_stations(spec, params["bbox"])
    product = params.get("product", "currents")
    records: list[dict[str, Any]] = []
    for st in stations:
        snap = _fetch_station_snapshot(spec, st, params, now)
        if snap is not None:
            records.append(snap)
    return snapshots_to_point_fgb(records, spec, product=product)


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Discover and fetch stations, then serialize to point-FGB bytes; ``emit == "snapshot"`` takes the one-row-per-station path."""
    if ((spec.ingest or {}).get("per_station") or {}).get("emit") == "snapshot":
        return execute_snapshot(spec, params)
    records = fetch_station_records(spec, params)
    return stations_to_point_fgb(records, spec, product=params.get("product", "water_level"))
