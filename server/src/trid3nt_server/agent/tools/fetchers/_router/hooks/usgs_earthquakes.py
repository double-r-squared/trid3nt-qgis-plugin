"""usgs_earthquakes hooks (ADR 0056): USGS FDSN Event GeoJSON -> point features.

The one irreducible step the declarative surface cannot carry: the FDSN request
construction (a bespoke relative-window resolution + magnitude/window validation,
not an ArcGIS ``/query``) and the FDSN GeoJSON decode (id from the feature top
level, depth from the geometry Z coordinate, epoch-ms times, the ``metadata.count``
result-cap gate). Everything around them -- transport, retry, cache, payload gate,
FGB serialize, LayerURI, camera bbox -- is the shared router.
"""

from __future__ import annotations

import datetime as _dt
import math
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

#: USGS FDSN Event Web Service query endpoint.
FDSN_EVENT_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
#: FDSN single-response result cap; a query above it is truncated -> typed error.
FDSN_RESULT_LIMIT = 20000


def _resolve_window(sc: str, sfx: str, start_date: Any, end_date: Any) -> tuple[str, str]:
    """Resolve the (starttime, endtime) FDSN window as ISO UTC strings.

    Both omitted -> the most-recent ``DEFAULT_WINDOW_DAYS``. One-sided -> a
    30-day span anchored to the supplied bound. Raises the source-stamped input
    error on a bad date / reversed range / over-``MAX_WINDOW_DAYS`` span.
    """
    now = _dt.datetime.now(_dt.timezone.utc)

    def _parse(s: str, *, is_end: bool) -> _dt.datetime:
        raw = str(s).strip()
        try:
            dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            try:
                d = _dt.date.fromisoformat(raw)
            except ValueError:
                raise router_input_error(
                    sc,
                    f"{'end_date' if is_end else 'start_date'}={s!r} is not a valid "
                    f"ISO date/datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): {exc}",
                    sfx,
                )
            t = _dt.time(23, 59, 59) if is_end else _dt.time(0, 0, 0)
            dt = _dt.datetime.combine(d, t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)

    if start_date is None and end_date is None:
        end = now
        start = now - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    elif start_date is not None and end_date is None:
        start = _parse(start_date, is_end=False)
        end = now
    elif start_date is None and end_date is not None:
        end = _parse(end_date, is_end=True)
        start = end - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    else:
        start = _parse(start_date, is_end=False)
        end = _parse(end_date, is_end=True)

    if start > end:
        raise router_input_error(
            sc,
            f"start_date must be <= end_date; got start={start.isoformat()}, "
            f"end={end.isoformat()}",
            sfx,
        )
    span_days = (end - start).total_seconds() / 86400.0
    if span_days > MAX_WINDOW_DAYS:
        raise router_input_error(
            sc,
            f"time window {span_days:.0f} days exceeds the {MAX_WINDOW_DAYS}-day cap; "
            f"request a shorter window or call in chunks",
            sfx,
        )
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def _validate_min_magnitude(sc: str, sfx: str, min_magnitude: Any) -> float | None:
    """Validate the magnitude floor. ``None`` -> no floor (FDSN default)."""
    if min_magnitude is None:
        return None
    try:
        m = float(min_magnitude)
    except (TypeError, ValueError):
        raise router_input_error(sc, f"min_magnitude must be numeric; got {min_magnitude!r}", sfx)
    if not math.isfinite(m):
        raise router_input_error(sc, f"min_magnitude must be finite; got {min_magnitude!r}", sfx)
    if not (-2.0 <= m <= 12.0):
        raise router_input_error(sc, f"min_magnitude={m} is outside the physical range [-2, 12]", sfx)
    return m


@_hooks.register_hook("usgs_earthquakes.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the window + magnitude floor and build the FDSN Event query URL."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    bbox = params.get("bbox")
    starttime, endtime = _resolve_window(sc, sfx, params.get("start_date"), params.get("end_date"))
    min_magnitude = _validate_min_magnitude(sc, sfx, params.get("min_magnitude"))

    query: list[tuple[str, str]] = [
        ("format", "geojson"),
        ("starttime", starttime),
        ("endtime", endtime),
        ("orderby", "time"),
        ("limit", str(FDSN_RESULT_LIMIT)),
    ]
    if bbox is not None:
        west, south, east, north = bbox
        query += [
            ("minlongitude", repr(float(west))),
            ("minlatitude", repr(float(south))),
            ("maxlongitude", repr(float(east))),
            ("maxlatitude", repr(float(north))),
        ]
    if min_magnitude is not None:
        query.append(("minmagnitude", repr(min_magnitude)))
    url = FDSN_EVENT_URL + "?" + urllib.parse.urlencode(query)
    return [_hooks.RequestPlan(url=url, headers={"User-Agent": spec.auth.user_agent})]


def _epoch_ms_to_iso(ms: Any) -> str | None:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    try:
        dt = _dt.datetime.fromtimestamp(v / 1000.0, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@_hooks.register_hook("usgs_earthquakes.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode the FDSN GeoJSON FeatureCollection into point features.

    Raises the source-stamped RESULT_TOO_LARGE on the FDSN cap, the EMPTY error
    on zero events (honest-empty, never a fabricated layer), and UPSTREAM on a
    non-JSON / non-FeatureCollection body.
    """
    import json

    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        records, count = [], None
    else:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"USGS FDSN response is not valid JSON: {exc}")
        if not isinstance(obj, dict):
            raise router_upstream_error(sc, f"USGS FDSN response is not a JSON object: type={type(obj).__name__}")
        if obj.get("type") != "FeatureCollection":
            raise router_upstream_error(sc, f"USGS FDSN response is not a GeoJSON FeatureCollection: type={obj.get('type')!r}")
        meta = obj.get("metadata") or {}
        try:
            count = int(meta.get("count")) if meta.get("count") is not None else None
        except (TypeError, ValueError):
            count = None
        records = _records_from_features(obj.get("features") or [])

    if (count is not None and count > FDSN_RESULT_LIMIT) or (len(records) >= FDSN_RESULT_LIMIT):
        raise router_input_error(
            sc,
            f"USGS FDSN matched {count if count is not None else '>='}{len(records)} events, "
            f"exceeding the {FDSN_RESULT_LIMIT}-event response cap. Narrow the bbox, shorten "
            f"the window, or raise min_magnitude.",
            "RESULT_TOO_LARGE",
        )
    if not records:
        raise router_empty_error(
            sc,
            "No earthquakes matched the requested scope/window/magnitude. The USGS FDSN "
            "service returned zero events. Widen the window, lower min_magnitude, or pick a "
            "more seismically active area.",
            spec.empty_error_suffix,
        )
    return records


def _records_from_features(features: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        depth_km: float | None = None
        if len(coords) >= 3:
            try:
                d = float(coords[2])
                depth_km = d if math.isfinite(d) else None
            except (TypeError, ValueError):
                depth_km = None
        props = feat.get("properties") or {}
        mag: float | None = None
        try:
            mv = props.get("mag")
            if mv is not None:
                fm = float(mv)
                mag = fm if math.isfinite(fm) else None
        except (TypeError, ValueError):
            mag = None
        out.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "id": str(feat.get("id") or "").strip(),
                    "mag": mag,
                    "depth_km": depth_km,
                    "mag_type": str(props.get("magType") or "").strip() or None,
                    "place": str(props.get("place") or "").strip() or None,
                    "time": _epoch_ms_to_iso(props.get("time")),
                    "updated": _epoch_ms_to_iso(props.get("updated")),
                    "url": str(props.get("url") or "").strip() or None,
                    "event_type": str(props.get("type") or "").strip() or None,
                    "status": str(props.get("status") or "").strip() or None,
                    "tsunami": int(props.get("tsunami") or 0),
                    "felt": (int(props["felt"]) if props.get("felt") not in (None, "") else None),
                    "sig": (int(props["sig"]) if props.get("sig") not in (None, "") else None),
                    "net": str(props.get("net") or "").strip() or None,
                },
            }
        )
    return out
