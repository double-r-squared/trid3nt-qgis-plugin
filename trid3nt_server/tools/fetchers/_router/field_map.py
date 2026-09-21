"""The declarative field map: what a build/parse hook pair states in Python.

A spec states its request template, its body decode, its paging style and its keyed
detail join under ``ingest``; the executors read them wherever no hook is named. The
vocabulary is the station_timeseries one generalized from a station loop to a row list."""

from __future__ import annotations

import datetime as _dt
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
    "enrich_plans",
    "enrich_merge",
    "page_injection",
    "rows_in_body",
]

_FORMATTER = string.Formatter()


def read_path(obj: Any, path: str) -> Any:
    """Read a dotted path out of a decoded body. A whole segment that is all digits
    indexes a list, so a GeoJSON depth is ``geometry.coordinates.2``. A missing or
    mistyped segment yields None rather than raising."""
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
    """The param names a request template references, each stripped to its root so
    ``{bbox[0]}`` and ``{start:%Y%m%d}`` both report ``bbox`` / ``start``."""
    out: list[str] = []
    for _lit, field, _spec, _conv in _FORMATTER.parse(template):
        if field:
            out.append(field.split("[")[0].split(".")[0])
    return out


def _render(template: Any, fmt: dict[str, Any]) -> Any:
    """Render one request value, or None when the template references a param that is
    unset. An unset param DROPS its key: sending the string "None" would ask the
    source a question about a value nobody supplied."""
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
    """Parse one window bound as an ISO date or datetime, UTC. A bare date is the start
    of its day for a window start and the end of it for a window end."""
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
    """Resolve a relative request window into the two format keys the request names.
    It resolves HERE, after the cache key, so an unbounded ask keys as the unbounded
    ask it is instead of keying a fresh instant on every call."""
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
    """The request plans stated by ``ingest.request``, one per endpoint in the resolved
    chain. Every query value is a template over the validated params, and a template
    referencing an unset param drops its key."""
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
        endpoint = spec.endpoints.get(str(named))
        chain = [endpoint] if endpoint is not None else []
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


def _decode(spec: SourceSpec, body: dict[str, Any], raw: bytes) -> Any:
    fmt = str(body.get("format", "json"))
    sc = spec.error_code_prefix
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
    """The row list one body carries, per ``ingest.body``. The pager counts with this,
    so a short page is measured against the same rows the decode yields."""
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


def declared_features(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """The features stated by ``ingest.body``: decode each body, walk to the row list,
    build the geometry, and hand the RAW row to the column map the serializer applies.
    A declared ``ingest.empty`` turns a zero-row answer into the source's typed empty."""
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
            features.append({
                "type": "Feature",
                "geometry": _geometry(body, row),
                "properties": row if isinstance(row, dict) else {},
            })
    if not features:
        empty = declared_empty_error(spec, "the source returned zero records in scope")
        if empty is not None:
            raise empty
    return features


def declared_empty_error(spec: SourceSpec, fallback: str) -> RouterError | None:
    """The source's typed empty from ``ingest.empty``, or None when it declares none -
    in which case the serializer's header-only file is the honest answer."""
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
    """An honest zero wearing an HTTP error: ``ingest.empty.on_status`` with an optional
    ``body_contains`` says which status over which body IS the source's empty. Anything
    else stays an upstream failure."""
    empty = (spec.ingest or {}).get("empty") or {}
    statuses = empty.get("on_status")
    if not statuses or status is None or int(status) not in [int(s) for s in statuses]:
        return None
    needle = empty.get("body_contains")
    if needle and needle not in (body or ""):
        return None
    return declared_empty_error(spec, f"the source answered {status} with no records in scope")


def page_injection(pagination: dict[str, Any], page: int, cursor: Any) -> dict[str, Any]:
    """The params one page adds, by style: ``page`` counts from 1, ``offset`` counts
    rows, ``cursor`` carries the token the previous body named. The request template
    reads them by name, so a build hook and a declared request page identically."""
    style = str(pagination.get("style", "page"))
    size = int(pagination.get("page_size", 1000))
    if style == "offset":
        return {"offset": (page - 1) * size, "page_size": size}
    if style == "cursor":
        return {"cursor": cursor, "page_size": size}
    return {str(pagination.get("page_param", "page")): page, "page_size": size}


def enrich_plans(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]
) -> list[tuple[str, RequestPlan]]:
    """One detail request per DISTINCT value of ``ingest.enrich.key_column``, in first-
    seen order. The key is an already-mapped output column, so the declaration names
    what the layer carries rather than what the payload happened to call it."""
    enrich = (spec.ingest or {}).get("enrich") or {}
    key_column = str(enrich.get("key_column"))
    request = enrich.get("request") or {}
    keys: list[str] = []
    for feat in features:
        value = (feat.get("properties") or {}).get(key_column)
        if value is not None and str(value) != "" and str(value) not in keys:
            keys.append(str(value))
    headers = {"User-Agent": spec.auth.user_agent}
    plans: list[tuple[str, RequestPlan]] = []
    for key in keys:
        fmt = {**params, "key": key}
        endpoint = spec.endpoints.get(str(request.get("endpoint", "detail")))
        url = _render(endpoint.url_template or endpoint.url or "", fmt) if endpoint else None
        plans.append((key, RequestPlan(
            url=url or "",
            params=_render_block(request.get("params") or {}, fmt),
            headers=headers,
        )))
    return plans


def _detail_rows(spec: SourceSpec, enrich: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    """Index every fetched detail row by the join's ``detail_key`` path. A ref that failed is
    absent from the index, which the join then treats as an unmatched key."""
    body = enrich.get("body") or {}
    detail_key = str((enrich.get("join") or {}).get("detail_key"))
    index: dict[str, Any] = {}
    for result in results.values():
        raw = getattr(result, "body", None)
        if not raw:
            continue
        obj = _decode(spec, body, raw)
        if isinstance(obj, dict) and obj.get("error"):
            raise router_upstream_error(
                spec.error_code_prefix, f"detail endpoint returned an error: {obj['error']}")
        path = body.get("rows_path")
        rows = read_path(obj, path) if path else obj
        for row in rows if isinstance(rows, list) else []:
            key = read_path(row, detail_key)
            if key is not None:
                index[str(key)] = row
    return index


def enrich_merge(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]],
    results: dict[str, Any],
) -> list[dict[str, Any]]:
    """Left-join the fetched detail onto each feature by ``feature_key`` -> ``detail_key``.
    ``take`` names the output columns lifted off the detail row, ``geometry`` lifts its
    shape, and an unmatched feature survives unless the join declares otherwise."""
    # The keys are spelled detail_key / feature_key rather than on / onto: YAML reads a
    # bare ``on`` as the boolean true, which would silently lose the join field.
    enrich = (spec.ingest or {}).get("enrich") or {}
    join = enrich.get("join") or {}
    feature_key = str(join.get("feature_key"))
    take = join.get("take") or {}
    lift_geometry = bool(join.get("geometry"))
    drop_unmatched = bool(join.get("drop_unmatched"))
    index = _detail_rows(spec, enrich, results)
    clip = _clip_shape(spec, params) if enrich.get("clip_to_bbox") else None

    out: list[dict[str, Any]] = []
    for feat in features:
        props = dict(feat.get("properties") or {})
        detail = index.get(str(props.get(feature_key)))
        if detail is None:
            if drop_unmatched:
                continue
            out.append({"type": "Feature", "geometry": feat.get("geometry"), "properties": props})
            continue
        geometry = read_path(detail, "geometry") if lift_geometry else feat.get("geometry")
        if clip is not None and not _intersects(spec, geometry, clip):
            continue
        for out_col, source_field in take.items():
            props[str(out_col)] = read_path(detail, str(source_field))
        out.append({"type": "Feature", "geometry": geometry, "properties": props})

    if not out:
        empty = declared_empty_error(spec, "no record joined a detail shape in scope")
        if empty is not None:
            raise empty
    return out


def _clip_shape(spec: SourceSpec, params: dict[str, Any]) -> Any:
    bbox = params.get("bbox")
    if not bbox:
        return None
    try:
        from shapely.geometry import box
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"shapely unavailable for the clip: {exc}")
    return box(*(float(v) for v in bbox))


def _intersects(spec: SourceSpec, geometry: Any, clip: Any) -> bool:
    from shapely.geometry import shape

    try:
        return bool(shape(geometry).intersects(clip))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
