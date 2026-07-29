"""station-timeseries-fgb executor (contract sec 2.3).

Catalog-discover (``ingest.station_catalog`` bbox filter, ``max_stations`` cap)
-> per-station data loop -> point-FGB with one Point per station + the scalar
rollups + the inline ``time_series_csv`` attribute (comma-separated ``iso,value``
rows for SFINCS boundary consumption). Individual station failures swallow to
``None`` (one bad station never aborts the bbox); all-empty -> typed ``*_EMPTY``.
This is the ``_fetch_coops_tides_bytes`` + ``_build_flatgeobuf`` contract,
generalized.

The pure serializer ``stations_to_point_fgb`` is offline-testable with synthetic
station records; the network path routes through ``fetch_station_records`` which
tests monkeypatch.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_upstream_error

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.station_timeseries"
)

__all__ = ["stations_to_point_fgb", "fetch_station_records", "execute"]


def _normalize_time(t: Any, mode: str | None) -> Any:
    """Normalize a per-observation timestamp per ``ingest.per_station.time_normalize``.

    ``iso8601z`` mirrors the CO-OPS twin EXACTLY (``_fetch_station_data``):
    ``"2022-09-28 00:00" -> "2022-09-28T00:00Z"`` (space -> ``T`` + ``Z`` suffix),
    but ONLY when the value carries a space (already-ISO values pass through).
    """
    if mode == "iso8601z" and isinstance(t, str) and " " in t:
        return t.replace(" ", "T") + "Z"
    return t


def _as_date(v: Any) -> Any:
    """Coerce an ISO ``YYYY-MM-DD`` string (router-validated) to a ``date`` so a
    ``{start:%Y%m%d}`` request template strftimes instead of raising on a str
    (the CO-OPS datagetter needs YYYYMMDD; VERDICT live-gap #coops)."""
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return v

#: The generalized point-FGB column schema (coops reference). ``time_series_csv``
#: carries the inline per-station series for SFINCS forcing.
_COLUMNS = [
    "station_id", "station_name", "lon", "lat", "product", "datum",
    "time_start", "time_end", "n_timesteps",
    "wl_min_m", "wl_max_m", "wl_mean_m", "time_series_csv",
]


def stations_to_point_fgb(
    records: list[dict[str, Any]],
    spec: SourceSpec,
    *,
    product: str = "water_level",
) -> bytes:
    """Serialize per-station time-series records to point-FGB bytes (pure).

    Each record: ``{station_id, station_name, lon, lat, rows: [{t, v}, ...]}``.
    Emits one Point per station carrying the scalar rollups + the inline
    ``time_series_csv`` (``iso,value`` rows). An all-empty record set raises the
    typed ``*_EMPTY`` error (station sources are typed-empty, not honest-empty).
    """
    import numpy as np

    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"geopandas/shapely unavailable: {exc}")

    datum = spec.normalize.datum or "MLLW"
    time_norm = ((spec.ingest or {}).get("per_station") or {}).get("time_normalize")
    rows_out: list[dict[str, Any]] = []
    geoms: list[Any] = []
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
        geoms.append(Point(rec["lon"], rec["lat"]))

    if not rows_out:
        raise router_empty_error(
            spec.error_code_prefix,
            "no station carried data in the requested bbox/window",
            spec.empty_error_suffix,
        )

    df = pd.DataFrame(rows_out)
    gdf = gpd.GeoDataFrame(df, geometry=geoms, crs=spec.normalize.crs)

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_router_sta_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(
                spec.error_code_prefix, f"FlatGeobuf write failed: {exc}"
            )
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "router.station_timeseries: FlatGeobuf = %d bytes (%d station(s), source=%s)",
            len(fgb_bytes), len(rows_out), spec.source_class,
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Catalog discover + per-station loop (network). Tests monkeypatch this.
# --------------------------------------------------------------------------- #


def _discover_stations(spec: SourceSpec, bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Fetch the station catalog and bbox-filter it. Network."""
    import httpx

    ingest = spec.ingest or {}
    cat = ingest.get("station_catalog", {})
    lat_key = cat.get("lat_key", "lat")
    lon_key = cat.get("lon_key", "lng")
    id_key = cat.get("id_key", "id")
    name_key = cat.get("name_key", "name")
    rows_key = cat.get("rows_key", "stations")
    endpoint = spec.endpoints.get("catalog") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url, params=dict(endpoint.query or {}),
                              headers={"User-Agent": spec.auth.user_agent})
        resp.raise_for_status()
        body = resp.json()
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
    """Fetch one station's time series. Network. Returns ``[]`` on failure/empty."""
    import httpx

    ingest = spec.ingest or {}
    per = ingest.get("per_station", {})
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url_template or endpoint.url or ""
    req_tmpl = dict(per.get("request", {}))
    # start/end are date objects so a "{start:%Y%m%d}" template strftimes to the
    # CO-OPS datagetter's required YYYYMMDD (a raw str would raise on %Y).
    fmt = {"id": station["station_id"], "product": params.get("product", "water_level"),
           "start": _as_date(params.get("start_date")), "end": _as_date(params.get("end_date"))}
    req = {}
    for k, v in req_tmpl.items():
        try:
            req[k] = v.format(**fmt) if isinstance(v, str) else v
        except (KeyError, IndexError, ValueError):
            req[k] = v
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url, params=req, headers={"User-Agent": spec.auth.user_agent})
        resp.raise_for_status()
        body = resp.json()
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
    """Discover stations and fetch each series. Returns records with ``rows``."""
    bbox = params["bbox"]
    stations = _discover_stations(spec, bbox)
    records: list[dict[str, Any]] = []
    for st in stations:
        rows = _fetch_station_series(spec, st, params)
        records.append({**st, "rows": rows})
    return records


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Discover + fetch stations and serialize to point-FGB bytes."""
    records = fetch_station_records(spec, params)
    return stations_to_point_fgb(records, spec, product=params.get("product", "water_level"))
