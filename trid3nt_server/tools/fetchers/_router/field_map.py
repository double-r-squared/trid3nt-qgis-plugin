"""The declarative field map: what a build/parse hook pair states in Python.

A row states its request template, body decode and paging style under ``ingest``; the executors read
them wherever no hook is named. A row's record is what its service returned, untouched."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import math
import string
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .errors import (RouterError, router_empty_error, router_input_error,
                     router_upstream_error)
from .hooks import RequestPlan

__all__ = [
    "declared_plans",
    "declared_features",
    "declared_empty_error",
    "declared_status_error",
    "page_injection",
    "rows_in_body",
]

_FORMATTER = string.Formatter()


def read_path(obj: Any, path: str) -> Any:
    """Read a dotted path out of a decoded body; an all-digit segment indexes a list, a missing segment yields None."""
    cur = obj
    for seg in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)) and seg.isdigit():
            idx = int(seg)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def _template_fields(template: str) -> list[str]:
    out: list[str] = []
    for _lit, field, _spec, _conv in _FORMATTER.parse(template):
        if field:
            out.append(field.split("[")[0].split(".")[0])
    return out


def _render(template: Any, fmt: dict[str, Any]) -> Any:
    """Render one request value, or None when it references an unset param: an unset param DROPS its key."""
    if not isinstance(template, str):
        return template
    for name in _template_fields(template):
        if fmt.get(name) is None:
            return None
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _render_block(block: Any, fmt: dict[str, Any]) -> Any:
    if isinstance(block, dict):
        out = {}
        for k, v in block.items():
            rendered = _render_block(v, fmt)
            if rendered is not None:
                out[str(k)] = rendered
        return out
    if isinstance(block, list):
        return [_render_block(v, fmt) for v in block]
    return _render(block, fmt)


def _parse_moment(spec: SourceSpec, name: str, raw: Any, *, is_end: bool) -> _dt.datetime:
    """Parse one window bound as an ISO date or datetime, UTC; a bare date is the start of its day for a start, the end for an end."""
    text = str(raw).strip()
    try:
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = _dt.date.fromisoformat(text)
        except ValueError:
            raise router_input_error(
                spec.error_code_prefix,
                f"{name}={raw!r} is not a valid ISO date/datetime "
                f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
                spec.input_error_suffix,
            )
        clock = _dt.time(23, 59, 59) if is_end else _dt.time(0, 0, 0)
        moment = _dt.datetime.combine(day, clock)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc)


def _date_window(spec: SourceSpec, window: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a relative request window into the request's format keys, AFTER the cache key so an unbounded ask keys as unbounded."""
    start_name = str(window.get("start", "start_date"))
    end_name = str(window.get("end", "end_date"))
    days = int(window.get("default_days", 30))
    now = _dt.datetime.now(_dt.timezone.utc)
    raw_start = params.get(start_name)
    raw_end = params.get(end_name)
    start = _parse_moment(spec, start_name, raw_start, is_end=False) if raw_start is not None else None
    end = _parse_moment(spec, end_name, raw_end, is_end=True) if raw_end is not None else None
    if start is None and end is None:
        start, end = now - _dt.timedelta(days=days), now
    elif end is None:
        end = now
    elif start is None:
        start = end - _dt.timedelta(days=days)
    if start > end:
        raise router_input_error(
            spec.error_code_prefix,
            f"{start_name} must be <= {end_name}; got start={start.isoformat()}, "
            f"end={end.isoformat()}",
            spec.input_error_suffix,
        )
    cap = window.get("max_span_days")
    if cap is not None:
        span = (end - start).total_seconds() / 86400.0
        if span > float(cap):
            raise router_input_error(
                spec.error_code_prefix,
                f"time window {span:.0f} days exceeds the {int(cap)}-day cap; "
                f"request a shorter window or call in chunks",
                spec.input_error_suffix,
            )
    fmt = str(window.get("format", "%Y-%m-%dT%H:%M:%S"))
    return {start_name: start.strftime(fmt), end_name: end.strftime(fmt)}


def _request_fmt(spec: SourceSpec, request: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    fmt = dict(params)
    window = request.get("date_window")
    if window:
        fmt.update(_date_window(spec, window, params))
    return fmt


def declared_plans(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The request plans stated by ``ingest.request``, one per endpoint in the chain; a template over an unset param drops its key."""
    from .executors.vector_fgb import resolve_endpoints

    request = (spec.ingest or {}).get("request") or {}
    fmt = _request_fmt(spec, request, params)
    query = _render_block(request.get("params") or {}, fmt)
    json_body = _render_block(request.get("json_body"), fmt) if request.get("json_body") else None
    headers = {"User-Agent": spec.auth.user_agent}
    headers.update(_render_block(request.get("headers") or {}, fmt))
    method = str(request.get("method", "GET")).upper()
    named = request.get("endpoint")
    if named:
        # A list names the endpoints this request is MADE of: every one fetched, the bodies joined at parse.
        names = [named] if isinstance(named, str) else list(named)
        chain = [spec.endpoints[n] for n in names if n in spec.endpoints]
    else:
        chain = resolve_endpoints(spec, params)
    plans: list[RequestPlan] = []
    for endpoint in chain:
        url = _render(endpoint.url_template or endpoint.url or "", fmt) or ""
        base = dict(endpoint.query or {})
        base.update(query)
        plans.append(RequestPlan(
            url=url,
            params=dict(base) if method == "GET" or not json_body else dict(base),
            headers=headers,
            method=method,
            json_body=json_body,
            data=_render_block(request.get("data"), fmt) if request.get("data") else None,
        ))
    return plans


def _refuse_declared(spec: SourceSpec, body: dict[str, Any], text: str) -> None:
    """The source's refusal read off the body: ``ingest.body.refuse`` markers, whether the service reports under HTTP 200 or a 4xx."""
    for rule in body.get("refuse") or []:
        if str(rule.get("contains", "")).lower() not in text.lower():
            continue
        raise router_input_error(
            spec.error_code_prefix,
            str(rule.get("message") or f"the source refused the request: {text[:200]}"),
            str(rule.get("code") or spec.input_error_suffix),
        )


def _csv_rows(spec: SourceSpec, body: dict[str, Any], text: str) -> list[dict[str, str]]:
    """A comma-separated body as its header's rows, every value the text the service sent; an unreadable geometry header is refused."""
    reader = csv.DictReader(io.StringIO(text))
    names = list(reader.fieldnames or [])
    geometry = body.get("geometry") or {}
    needed = [str(geometry[axis]) for axis in ("lon", "lat") if axis in geometry]
    missing = [name for name in needed if name not in names]
    if not names or missing:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"CSV body names no {missing or ['header']} column; got columns={names}")
    return [dict(row) for row in reader]


def _decode(spec: SourceSpec, body: dict[str, Any], raw: bytes) -> Any:
    fmt = str(body.get("format", "json"))
    sc = spec.error_code_prefix
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"response is not UTF-8: {exc}")
    _refuse_declared(spec, body, text)
    if fmt == "csv":
        return _csv_rows(spec, body, text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise router_upstream_error(sc, f"response is not valid JSON: {exc}")
    if fmt == "geojson":
        if not isinstance(obj, dict):
            raise router_upstream_error(
                sc, f"response is not a JSON object: type={type(obj).__name__}")
        if obj.get("type") != "FeatureCollection":
            raise router_upstream_error(
                sc, f"response is not a GeoJSON FeatureCollection: type={obj.get('type')!r}")
    elif fmt != "json":
        raise router_upstream_error(sc, f"ingest.body.format {fmt!r} has no declared reader")
    return obj


def rows_in_body(spec: SourceSpec, raw: bytes) -> list[Any]:
    """The row list one body carries, per ``ingest.body``; the pager counts with this."""
    body = (spec.ingest or {}).get("body") or {}
    obj = _decode(spec, body, raw) if raw else None
    if obj is None:
        return []
    path = body.get("rows_path")
    rows = read_path(obj, path) if path else obj
    return list(rows) if isinstance(rows, list) else []


def _geometry(body: dict[str, Any], row: Any) -> Any:
    geometry = body.get("geometry") or {}
    kind = str(geometry.get("kind", "none"))
    if kind == "geojson":
        return read_path(row, geometry.get("path", "geometry"))
    if kind != "point":
        return None
    try:
        lon = float(read_path(row, geometry["lon"]))
        lat = float(read_path(row, geometry["lat"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def _check_row_cap(spec: SourceSpec, body: dict[str, Any], obj: Any, n_rows: int) -> None:
    cap = body.get("row_cap")
    if cap is None:
        return
    cap = int(cap)
    reported = read_path(obj, body["row_cap_path"]) if body.get("row_cap_path") else None
    try:
        reported = int(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported = None
    if n_rows < cap and (reported is None or reported <= cap):
        return
    hint = body.get("row_cap_hint") or "Narrow the bbox or shorten the window."
    raise router_input_error(
        spec.error_code_prefix,
        f"the source matched {reported if reported is not None else n_rows} records, "
        f"at or above its {cap}-record response cap. {hint}",
        "RESULT_TOO_LARGE",
    )


def _epoch_ms_iso(value: Any) -> str | None:
    """Epoch milliseconds as an ISO 8601 UTC instant (a date alone would drop the time of day); None where it is no instant."""
    try:
        ms = float(value)
        return _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _produced(body: dict[str, Any], row: Any) -> dict[str, Any]:
    """What ``ingest.body.produced`` makes of a row: each name from a path of the row, ``epoch_ms`` reading it as an instant."""
    made: dict[str, Any] = {}
    for name, rule in (body.get("produced") or {}).items():
        if "epoch_ms" in rule:
            made[str(name)] = _epoch_ms_iso(read_path(row, rule["epoch_ms"]))
        else:
            made[str(name)] = read_path(row, rule["from"])
    return made


def declared_features(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """The features stated by ``ingest.body``: a GeoJSON feature's own properties, a row as it came, beside what ``produced`` makes; ``ingest.empty`` makes a zero-row answer the typed empty."""
    body = (spec.ingest or {}).get("body") or {}
    features: list[dict[str, Any]] = []
    for raw in bodies:
        if not raw:
            continue
        obj = _decode(spec, body, raw)
        path = body.get("rows_path")
        rows = read_path(obj, path) if path else obj
        rows = list(rows) if isinstance(rows, list) else []
        _check_row_cap(spec, body, obj, len(rows))
        for row in rows:
            record = row.get("properties") if body.get("format") == "geojson" and isinstance(row, dict) else row
            features.append({
                "type": "Feature",
                "geometry": _geometry(body, row),
                "properties": record if isinstance(record, dict) else {},
                "produced": _produced(body, row),
            })
    if not features:
        empty = declared_empty_error(spec, "the source returned zero records in scope")
        if empty is not None:
            raise empty
    return features


def declared_empty_error(spec: SourceSpec, fallback: str) -> RouterError | None:
    """The source's typed empty from ``ingest.empty``, or None (the header-only file is then the honest answer)."""
    empty = (spec.ingest or {}).get("empty")
    if not empty:
        return None
    return router_empty_error(
        spec.error_code_prefix,
        str(empty.get("message") or fallback),
        str(empty.get("code") or spec.empty_error_suffix),
    )


def declared_status_error(
    spec: SourceSpec, status: int | None, body: str | None
) -> RouterError | None:
    """The typed error an HTTP failure body states: ``ingest.body.refuse``, then ``ingest.empty.on_status`` (optional ``body_contains``); else upstream."""
    try:
        _refuse_declared(spec, (spec.ingest or {}).get("body") or {}, body or "")
    except RouterError as refusal:
        return refusal
    empty = (spec.ingest or {}).get("empty") or {}
    statuses = empty.get("on_status")
    if not statuses or status is None or int(status) not in [int(s) for s in statuses]:
        return None
    needle = empty.get("body_contains")
    if needle and needle not in (body or ""):
        return None
    return declared_empty_error(spec, f"the source answered {status} with no records in scope")


def page_injection(pagination: dict[str, Any], page: int, cursor: Any) -> dict[str, Any]:
    """The params one page adds: ``page`` counts from 1, ``offset`` counts rows, ``cursor`` carries the previous body's token."""
    style = str(pagination.get("style", "page"))
    size = int(pagination.get("page_size", 1000))
    if style == "offset":
        return {"offset": (page - 1) * size, "page_size": size}
    if style == "cursor":
        return {"cursor": cursor, "page_size": size}
    return {str(pagination.get("page_param", "page")): page, "page_size": size}
