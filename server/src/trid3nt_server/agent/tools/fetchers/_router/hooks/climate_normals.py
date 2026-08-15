"""climate_normals hooks (chained_resolution enrich/0071): NOAA NCEI
1991-2020 U.S. Climate Normals as station points.

The twin's two-stage shape folds onto the EXISTING enrich phase with zero new
machinery: ``build_request`` GETs the fixed-width station inventory; ``parse_response``
slices it, spatially filters to the bbox, caps at ``gates.max_stations``, and emits one
Point feature per station (no normals yet), raising a typed CLIMATE_NORMALS_EMPTY when
the bbox holds no stations. PHASE E (``enrich_plan`` emits one per-station access-CSV
ref; ``enrich_merge`` decodes each CSV, folds the annual normals back, and DROPS a
station with no usable annual normal -- the twin's skip) produces the final records,
raising CLIMATE_NORMALS_EMPTY when none survive. All I/O (inventory GET, per-station
GETs, retry, cache, FGB serialize) stays router-owned; the hooks only compute.
"""

from __future__ import annotations

import csv
import io
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_upstream_error

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

_INVENTORY_URL = "https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/doc/inventory_30yr.txt"
_ACCESS_URL = "https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/access/{sid}.csv"
_MISSING_SENTINEL = -9999.0
_COL_TAVG = "ANN-TAVG-NORMAL"
_COL_TMIN = "ANN-TMIN-NORMAL"
_COL_TMAX = "ANN-TMAX-NORMAL"
_COL_PRCP = "ANN-PRCP-NORMAL"
_COLUMNS = (
    "station_id", "name", "elevation_m",
    "normal_temp_f", "normal_tmin_f", "normal_tmax_f", "normal_precip_in",
)


def _num(value: Any) -> float | None:
    """Coerce a Normals field to float, mapping NCEI sentinels to None."""
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= _MISSING_SENTINEL:
        return None
    return x


@_hooks.register_hook("climate_normals.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """One GET of the fixed-width NCEI station inventory."""
    return [_hooks.RequestPlan(url=_INVENTORY_URL, headers={"User-Agent": spec.auth.user_agent})]


@_hooks.register_hook("climate_normals.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Slice the inventory, filter to the bbox, cap, and emit station Point features."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    text = raw.decode("utf-8", errors="replace")
    stations: list[dict[str, Any]] = []
    for line in text.splitlines():
        if len(line) < 40:
            continue
        sid = line[0:11].strip()
        if not sid:
            continue
        try:
            lat = float(line[12:20])
            lon = float(line[21:30])
        except ValueError:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        stations.append({
            "sid": sid, "lat": lat, "lon": lon,
            "elev": _num(line[30:37]), "state": line[38:40].strip(),
            "name": line[41:71].strip(),
        })
    if not stations:
        raise router_upstream_error(
            sc, "NCEI Normals inventory parsed to zero stations -- file format may have "
            "changed or the download was truncated")

    west, south, east, north = (float(v) for v in params["bbox"])
    cap = int(spec.gates.max_stations or 120)
    feats: list[dict[str, Any]] = []
    for st in stations:
        if west <= st["lon"] <= east and south <= st["lat"] <= north:
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
                "properties": {
                    "sid": st["sid"], "station_id": st["sid"], "name": st["name"],
                    "elevation_m": st["elev"], "inv_lat": st["lat"], "inv_lon": st["lon"],
                },
            })
            if len(feats) >= cap:
                break
    if not feats:
        raise router_empty_error(
            sc,
            f"No 1991-2020 Climate Normals stations found inside bbox={list(params['bbox'])!r}; "
            "the NCEI Normals footprint is the U.S. + territories -- widen the bbox or move it "
            "over U.S. land",
            spec.empty_error_suffix,
        )
    return feats


@_hooks.register_hook("climate_normals.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """One access-CSV ref per station (ref_key = station id)."""
    ua = {"User-Agent": spec.auth.user_agent}
    out: list[tuple[str, "_hooks.RequestPlan"]] = []
    for f in features:
        sid = (f.get("properties") or {}).get("sid")
        if sid:
            out.append((str(sid), _hooks.RequestPlan(url=_ACCESS_URL.format(sid=sid), headers=ua)))
    return out


def _parse_station_csv(raw: bytes | None) -> dict[str, Any] | None:
    """Parse one station's annual access CSV into a normals dict (None if unusable)."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    reader = csv.DictReader(io.StringIO(text))
    try:
        row = next(reader)
    except StopIteration:
        return None
    return {
        "tavg": _num(row.get(_COL_TAVG)), "tmin": _num(row.get(_COL_TMIN)),
        "tmax": _num(row.get(_COL_TMAX)), "prcp": _num(row.get(_COL_PRCP)),
        "name": (row.get("NAME") or "").strip(),
        "lat": _num(row.get("LATITUDE")), "lon": _num(row.get("LONGITUDE")),
        "elev": _num(row.get("ELEVATION")),
    }


@_hooks.register_hook("climate_normals.enrich_merge")
def enrich_merge(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold each station's annual normals in; DROP stations with no usable normal; EMPTY if none."""
    sc = spec.error_code_prefix
    out: list[dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        sid = props.get("sid")
        res = results.get(str(sid)) if sid is not None else None
        body = getattr(res, "body", None) if res is not None else None
        norm = _parse_station_csv(body)
        if norm is None:
            continue
        if norm["tavg"] is None and norm["prcp"] is None:
            continue
        lat = norm["lat"] if norm["lat"] is not None else props.get("inv_lat")
        lon = norm["lon"] if norm["lon"] is not None else props.get("inv_lon")
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        row = {
            "station_id": props.get("station_id"),
            "name": norm["name"] or props.get("name"),
            "elevation_m": norm["elev"] if norm["elev"] is not None else props.get("elevation_m"),
            "normal_temp_f": norm["tavg"], "normal_tmin_f": norm["tmin"],
            "normal_tmax_f": norm["tmax"], "normal_precip_in": norm["prcp"],
        }
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {c: row.get(c) for c in _COLUMNS},
        })
    if not out:
        raise router_empty_error(
            sc,
            "No 1991-2020 Climate Normals stations with annual normals were found in the "
            "requested bbox",
            spec.empty_error_suffix,
        )
    return out
