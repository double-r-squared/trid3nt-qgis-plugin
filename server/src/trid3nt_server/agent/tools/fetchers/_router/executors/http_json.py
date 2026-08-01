"""http_json executor (ADR 0056): the tier-3 hook-driven point-event fetch path.

Selected when a spec declares ``hooks.build_request``. The engine owns the
transport (the shared pooled client + retry authority), the paging LOOP, and the
FGB serialize; the two named PURE hooks own the source-specific steps:
``build_request`` constructs the request(s), ``parse_response`` decodes the body
/ bodies into GeoJSON point features (raising the honest-empty / too-large /
bad-body typed errors). Multi-request (N static plans, joined at parse) and paging
(one plan per page, bounded by the declared ``totalPages`` probe) both funnel a
LIST of response bodies into the single parse hook -- identical downstream.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from ..hooks import RequestPlan, resolve_hook
from ..transport import TransportError, get_bytes, get_client, post_bytes
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.http_json"
)

__all__ = ["execute", "fetch_bodies"]


def _get_raw(plan: RequestPlan) -> bytes:
    """Execute one plan through the shared transport; let ``TransportError`` propagate.

    GET by default; ``plan.method == "POST"`` sends ``plan.json_body`` as a JSON
    body or ``plan.data`` as a form-encoded body (the Overpass QL ``data`` field).
    The transport owns the retry authority either way.
    """
    if plan.method == "POST":
        body, _ct, _url = post_bytes(
            get_client(), plan.url, headers=plan.headers, params=plan.params,
            json_body=plan.json_body, data=plan.data,
        )
    else:
        body, _ct, _url = get_bytes(get_client(), plan.url, headers=plan.headers, params=plan.params)
    return body


def _get(spec: SourceSpec, plan: RequestPlan) -> bytes:
    """Execute one plan, mapping a ``TransportError`` to the source-stamped router error."""
    try:
        return _get_raw(plan)
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"{type(exc).__name__}: {exc}")


def _fetch_endpoint_fallback(spec: SourceSpec, plans: list[RequestPlan]) -> list[bytes]:
    """Try ``plans`` as a data-source fallback CHAIN; the first success wins.

    The build hook returns one plan per mirror (the primary + its siblings, the
    spec fallback-chain). Each is tried in order through the shared transport;
    the first success returns a single-body list. A non-429 4xx short-circuits the
    chain (the request itself is bad -- another mirror will not help); a 5xx / 429 /
    timeout / connection error advances to the next mirror. If every mirror fails a
    source-stamped upstream error is raised (retryable), naming the count.
    """
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
                raise router_upstream_error(sc, f"{type(exc).__name__}: {exc}")
            last_exc = exc
            if i < len(plans) - 1:
                logger.warning(
                    "router.http_json: mirror %d/%d failed (%s); trying next",
                    i + 1, len(plans), exc,
                )
    raise router_upstream_error(
        sc, f"all {len(plans)} mirrors failed; last error: {last_exc}"
    )


def _fetch_paged(spec: SourceSpec, params: dict[str, Any], build: Any, paging: dict[str, Any]) -> list[bytes]:
    """Walk pages until the declared ``totalPages`` (bounded by ``max_pages``).

    The page count is read from the first page's ``total_pages_key`` (a light JSON
    probe purely for loop control; the AUTHORITATIVE decode is the parse hook over
    all bodies). Overrunning ``max_pages`` -> the source-stamped RESULT_TOO_LARGE.
    """
    sc = spec.error_code_prefix
    page_param = paging.get("page_param", "page")
    total_pages_key = paging.get("total_pages_key", "totalPages")
    total_items_key = paging.get("total_items_key", "totalItems")
    max_pages = int(paging.get("max_pages", 25))

    bodies: list[bytes] = []
    total_pages = 1
    total_items: Any = None
    page = 1
    while page <= total_pages:
        if page > max_pages:
            raise router_input_error(
                sc,
                f"query spans {total_pages} pages (>{max_pages}-page cap, "
                f"~{total_items if total_items is not None else 'many'} records). Narrow the "
                f"bbox, shorten the window, or use a sparser observation_type.",
                "RESULT_TOO_LARGE",
            )
        plan = build(spec, {**params, page_param: page})[0]
        body = _get(spec, plan)
        bodies.append(body)
        if page == 1:
            try:
                obj = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise router_upstream_error(sc, f"paged response is not valid JSON: {exc}")
            total_items = obj.get(total_items_key)
            tp = obj.get(total_pages_key)
            try:
                total_pages = int(tp) if tp is not None and int(tp) >= 1 else 1
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
        page += 1
    return bodies


def fetch_bodies(spec: SourceSpec, params: dict[str, Any]) -> list[bytes]:
    """Resolve the request plan(s) via the build hook and GET the response body/bodies.

    Three modes, by declared ``ingest.http_source``: ``paging`` walks pages;
    ``endpoint_fallback`` treats the plans as a first-success-wins mirror chain
    (the Overpass 3-mirror fallback, the spec fallback-chain); the default fetches
    every plan and joins them at parse (a static multi-endpoint set).
    """
    build = resolve_hook(spec.hooks.build_request)  # type: ignore[union-attr]
    http_source = (spec.ingest or {}).get("http_source") or {}
    paging = http_source.get("paging")
    if paging:
        return _fetch_paged(spec, params, build, paging)
    plans = build(spec, params)
    if http_source.get("endpoint_fallback"):
        return _fetch_endpoint_fallback(spec, plans)
    return [_get(spec, plan) for plan in plans]


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch via the hooks and serialize the parsed features to FGB (the fetch_fn body)."""
    bodies = fetch_bodies(spec, params)
    parse = resolve_hook(spec.hooks.parse_response)  # type: ignore[union-attr]
    features = parse(spec, params, bodies)
    return features_to_fgb_bytes(features, spec, params)
