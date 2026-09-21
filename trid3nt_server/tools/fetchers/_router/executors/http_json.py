"""http_json executor: the point-event fetch path, declared or hooked.

The engine owns the transport, the paging LOOP and the FGB serialize. Two switches
pick the source-specific steps -- the ``build_request`` / ``parse_response`` hooks
when the spec names them, else the ``ingest.request`` / ``ingest.body`` field map."""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from ..field_map import (declared_features, declared_plans, declared_status_error,
                         page_injection, rows_in_body)
from ..hooks import RequestPlan, resolve_hook
from ..transport import TransportError, get_bytes, get_client, post_bytes
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.http_json"
)

__all__ = ["execute", "fetch_bodies"]


def _plans(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The request plans: the build hook when the spec names one, else the declared
    ``ingest.request`` over the resolved endpoint chain."""
    if spec.hooks is not None and spec.hooks.build_request:
        return resolve_hook(spec.hooks.build_request)(spec, params)
    return declared_plans(spec, params)


def _features(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """The decoded features: the parse hook when the spec names one, else the declared
    ``ingest.body`` -- decode, walk to the row list, build the geometry, and hand the
    raw row to the column map the serializer already applies."""
    if spec.hooks is not None and spec.hooks.parse_response:
        return resolve_hook(spec.hooks.parse_response)(spec, params, bodies)
    return declared_features(spec, params, bodies)


def _get_raw(plan: RequestPlan) -> bytes:
    """Execute one plan through the shared transport, letting ``TransportError``
    propagate. GET by default; ``plan.method == "POST"`` sends ``plan.json_body`` as
    JSON or ``plan.data`` form-encoded. The transport owns the retry authority."""
    if plan.method == "POST":
        body, _ct, _url = post_bytes(
            get_client(), plan.url, headers=plan.headers, params=plan.params,
            json_body=plan.json_body, data=plan.data,
        )
    else:
        body, _ct, _url = get_bytes(get_client(), plan.url, headers=plan.headers, params=plan.params)
    return body


def _get(spec: SourceSpec, plan: RequestPlan) -> bytes:
    """Execute one plan, mapping a ``TransportError`` to the source-stamped router
    error. A declared ``hooks.classify_status`` is consulted FIRST and returns either
    a typed error to raise or None, which falls through to an upstream error
    carrying the transport's own retryable verdict."""
    try:
        return _get_raw(plan)
    except TransportError as exc:
        if spec.hooks is not None and spec.hooks.classify_status:
            classify = resolve_hook(spec.hooks.classify_status)
            typed = classify(spec, exc.status, exc.body)
            if typed is not None:
                raise typed
        else:
            typed = declared_status_error(spec, exc.status, exc.body)
            if typed is not None:
                raise typed
        raise router_upstream_error(
            spec.error_code_prefix, f"{type(exc).__name__}: {exc}", exc.retryable)


def _fetch_endpoint_fallback(spec: SourceSpec, plans: list[RequestPlan]) -> list[bytes]:
    """Try ``plans`` as a data-source fallback CHAIN, first success wins: a non-429 4xx
    short-circuits (the request itself is bad), a 5xx / 429 / timeout advances to the
    next mirror, and every mirror failing raises a retryable upstream error."""
    sc = spec.error_code_prefix
    last_exc: Exception | None = None
    for i, plan in enumerate(plans):
        try:
            return [_get_raw(plan)]
        except TransportError as exc:
            status = getattr(exc, "status", None)
            # A non-429 4xx (bad query) will not succeed on another mirror -- fail
            # fast rather than hammer every sibling.
            if status is not None and 400 <= status < 500 and status != 429:
                raise router_upstream_error(
                    sc, f"{type(exc).__name__}: {exc}", exc.retryable)
            last_exc = exc
            if i < len(plans) - 1:
                logger.warning(
                    "router.http_json: mirror %d/%d failed (%s); trying next",
                    i + 1, len(plans), exc,
                )
    raise router_upstream_error(
        sc, f"all {len(plans)} mirrors failed; last error: {last_exc}"
    )


def _probe_total_pages(spec: SourceSpec, paging: dict[str, Any], body: bytes, max_pages: int) -> int:
    """Read the declared page count off page 1. It is loop control only -- the decode
    over every body stays authoritative -- and a count past the cap refuses up front
    rather than after walking the pages."""
    sc = spec.error_code_prefix
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"paged response is not valid JSON: {exc}")
    total_items = obj.get(paging.get("total_items_key", "totalItems"))
    raw = obj.get(paging.get("total_pages_key", "totalPages"))
    try:
        total_pages = int(raw) if raw is not None and int(raw) >= 1 else 1
    except (TypeError, ValueError):
        total_pages = 1
    if total_pages > max_pages:
        raise router_input_error(
            sc,
            f"query reports {total_items} records over {total_pages} pages "
            f"(>{max_pages}-page cap). Narrow the bbox, shorten the window, or use a "
            f"sparser observation_type.",
            "RESULT_TOO_LARGE",
        )
    return total_pages


def _fetch_paginated(spec: SourceSpec, params: dict[str, Any], paging: dict[str, Any]) -> list[bytes]:
    """Walk pages in the declared style. ``page`` counts to the page count the first
    body reports; ``offset`` counts rows and stops on a short page or the row cap. The
    page value enters ``params``, so a build hook and a declared request page alike."""
    sc = spec.error_code_prefix
    style = str(paging.get("style", "page"))
    max_pages = int(paging.get("max_pages", 25))
    page_size = int(paging.get("page_size", 1000))
    row_cap = paging.get("row_cap")
    bodies: list[bytes] = []
    total_pages = max_pages
    total_rows = 0
    page = 1
    while page <= max_pages:
        plan = _plans(spec, {**params, **page_injection(paging, page, None)})[0]
        body = _get(spec, plan)
        bodies.append(body)
        if style == "page":
            if page == 1:
                total_pages = _probe_total_pages(spec, paging, body, max_pages)
            if page >= total_pages:
                return bodies
        else:
            n_rows = len(rows_in_body(spec, body))
            total_rows += n_rows
            if paging.get("stop_on_short_page", True) and n_rows < page_size:
                return bodies
            if row_cap is not None and total_rows >= int(row_cap):
                return bodies
        page += 1
    raise router_input_error(
        sc,
        f"query spans more than the {max_pages}-page cap. Narrow the bbox, shorten the "
        f"window, or filter harder.",
        "RESULT_TOO_LARGE",
    )


def _fetch_constant_cache(spec: SourceSpec, plans: list[RequestPlan], cc: dict[str, Any]) -> list[bytes]:
    """Fetch each plan through an INNER constant-key ``read_through``, so a source that
    downloads ONE whole-world file and filters it per AOI caches that download under an
    AOI-independent key while the outer read_through still caches per AOI."""
    from ....cache import read_through
    from ..router import synthesize_metadata
    from ..spec import record_shape

    metadata = synthesize_metadata(spec)
    ext = str(cc.get("ext", "bin"))
    file_id = str(cc.get("file_id") or spec.source_class)
    bodies: list[bytes] = []
    for plan in plans:
        res = read_through(
            metadata=metadata,
            params={"file": file_id},
            ext=ext,
            fetch_fn=lambda p=plan: _get(spec, p),
            record_shape=record_shape(spec),
        )
        assert res.data is not None, "constant_cache source is cacheable; data must be set"
        bodies.append(res.data)
    return bodies


def fetch_bodies(spec: SourceSpec, params: dict[str, Any]) -> list[bytes]:
    """Resolve the request plans and GET the bodies. ``ingest.pagination`` walks pages,
    ``ingest.http_source.endpoint_fallback`` is a first-success mirror chain, and the
    default joins every plan at parse."""
    ingest = spec.ingest or {}
    http_source = ingest.get("http_source") or {}
    paging = ingest.get("pagination")
    if paging:
        return _fetch_paginated(spec, params, paging)
    plans = _plans(spec, params)
    cc = (spec.ingest or {}).get("constant_cache")
    if cc:
        return _fetch_constant_cache(spec, plans, cc)
    if http_source.get("endpoint_fallback"):
        return _fetch_endpoint_fallback(spec, plans)
    return [_get(spec, plan) for plan in plans]


def _execute_parse_fallback(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """PARSE-driven fallback chain: parse EACH plan's body on its own and stop at the
    first yielding >= 1 feature, so an empty body degrades to the next plan. Every plan
    empty raises the source's typed EMPTY, never a fabricated header-only layer."""
    plans = _plans(spec, params)
    for plan in plans:
        body = _get(spec, plan)
        features = _features(spec, params, [body])
        if features:
            return features_to_fgb_bytes(features, spec, params)
    raise router_empty_error(
        spec.error_code_prefix,
        f"no records from any of the {len(plans)} source endpoint(s) in scope",
        spec.empty_error_suffix,
    )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch and serialize the parsed features to FGB (the fetch_fn body)."""
    if ((spec.ingest or {}).get("http_source") or {}).get("parse_fallback"):
        return _execute_parse_fallback(spec, params)
    bodies = fetch_bodies(spec, params)
    features = _features(spec, params, bodies)
    return features_to_fgb_bytes(features, spec, params)
