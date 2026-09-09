"""http_json executor: the hook-driven point-event fetch path.

The engine owns the transport, the paging LOOP and the FGB serialize; two PURE hooks
own the source-specific steps -- ``build_request`` constructs the requests, and
``parse_response`` decodes the bodies and raises the honest-empty typed errors."""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from ..hooks import RequestPlan, resolve_hook
from ..transport import TransportError, get_bytes, get_client, post_bytes
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.http_json"
)

__all__ = ["execute", "fetch_bodies"]


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
    a typed error to raise or None, which keeps the retryable upstream default."""
    try:
        return _get_raw(plan)
    except TransportError as exc:
        if spec.hooks is not None and spec.hooks.classify_status:
            classify = resolve_hook(spec.hooks.classify_status)
            typed = classify(spec, exc.status, exc.body)
            if typed is not None:
                raise typed
        raise router_upstream_error(spec.error_code_prefix, f"{type(exc).__name__}: {exc}")


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
    """Walk pages until the declared ``totalPages``, bounded by ``max_pages``. The page
    count is a light probe for loop control only -- the authoritative decode is the
    parse hook over all bodies -- and overrunning raises RESULT_TOO_LARGE."""
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


def _fetch_constant_cache(spec: SourceSpec, plans: list[RequestPlan], cc: dict[str, Any]) -> list[bytes]:
    """Fetch each plan through an INNER constant-key ``read_through``, so a source that
    downloads ONE whole-world file and filters it per AOI caches that download under an
    AOI-independent key while the outer read_through still caches per AOI."""
    from ....cache import read_through
    from ..router import synthesize_metadata

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
        )
        assert res.data is not None, "constant_cache source is cacheable; data must be set"
        bodies.append(res.data)
    return bodies


def fetch_bodies(spec: SourceSpec, params: dict[str, Any]) -> list[bytes]:
    """Resolve the request plans via the build hook and GET the bodies. By declared
    ``ingest.http_source``: ``paging`` walks pages, ``endpoint_fallback`` is a
    first-success mirror chain, and the default joins every plan at parse."""
    build = resolve_hook(spec.hooks.build_request)  # type: ignore[union-attr]
    http_source = (spec.ingest or {}).get("http_source") or {}
    paging = http_source.get("paging")
    if paging:
        return _fetch_paged(spec, params, build, paging)
    plans = build(spec, params)
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
    build = resolve_hook(spec.hooks.build_request)  # type: ignore[union-attr]
    parse = resolve_hook(spec.hooks.parse_response)  # type: ignore[union-attr]
    plans = build(spec, params)
    for plan in plans:
        body = _get(spec, plan)
        features = parse(spec, params, [body])
        if features:
            return features_to_fgb_bytes(features, spec, params)
    raise router_empty_error(
        spec.error_code_prefix,
        f"no records from any of the {len(plans)} source endpoint(s) in scope",
        spec.empty_error_suffix,
    )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch via the hooks and serialize the parsed features to FGB (the fetch_fn body)."""
    if ((spec.ingest or {}).get("http_source") or {}).get("parse_fallback"):
        return _execute_parse_fallback(spec, params)
    bodies = fetch_bodies(spec, params)
    parse = resolve_hook(spec.hooks.parse_response)  # type: ignore[union-attr]
    features = parse(spec, params, bodies)
    return features_to_fgb_bytes(features, spec, params)
