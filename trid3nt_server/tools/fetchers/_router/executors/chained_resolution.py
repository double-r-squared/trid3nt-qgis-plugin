"""chained_resolution executor: resolve-then-fetch with bounded detail enrichment.

Two composable phases, each declared only when needed; the router owns the round trips, both
loops and the per-ref error aggregation, and source-specific PURE compute lives in hooks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import field_map
from ..errors import RouterError, router_input_error
from ..hooks import resolve_hook
from .http_json import _features, _get, _plans, fetch_bodies
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.chained_resolution"
)

__all__ = ["DetailResult", "pre_resolve", "execute", "fetch_detail_set"]

#: Ceiling on the offset-paging loop against a non-terminating response.
_MAX_PAGES = 200

#: Default cap on distinct detail fetches when ``ingest.chained.max_detail_fetches`` is omitted.
_DEFAULT_MAX_DETAIL_FETCHES = 3000


@dataclass(frozen=True)
class DetailResult:
    """One resolved detail ref: a body OR a typed error; the merge keeps the owning feature regardless."""

    body: bytes | None = None
    error: str | None = None


def _chained_block(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("chained") or {}


# Resolve phase (pre-cache-key, runs in route()): ``resolve_parse`` returns a params-merge dict so
# a name query and its id query collapse to one cache entry.


def pre_resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Run the resolve phase and return ``params`` with the resolved id merged in; called BEFORE read_through."""
    if spec.hooks is None or not spec.hooks.resolve_build:
        return params
    build = resolve_hook(spec.hooks.resolve_build)
    plans = build(spec, params)
    if not plans:
        return params
    bodies = [_get(spec, plan) for plan in plans]
    parse = resolve_hook(spec.hooks.resolve_parse)  # type: ignore[arg-type]
    update = parse(spec, params, bodies)
    if not isinstance(update, dict):
        return params
    return {**params, **update}


def _fetch_main(spec: SourceSpec, params: dict[str, Any]) -> list[bytes]:
    if not (spec.hooks and spec.hooks.next_page):
        return fetch_bodies(spec, params)
    bodies = [_get(spec, plan) for plan in _plans(spec, params)]
    nxt = resolve_hook(spec.hooks.next_page)
    page = 1
    while True:
        if page > _MAX_PAGES:
            raise router_input_error(
                spec.error_code_prefix,
                f"query exceeds the {_MAX_PAGES}-page safety cap; narrow the bbox / window.",
                "RESULT_TOO_LARGE",
            )
        plan = nxt(spec, params, bodies)
        if plan is None:
            break
        bodies.append(_get(spec, plan))
        page += 1
    return bodies


# Enrich phase: ``enrich_plan`` emits the ordered detail set; the router dedupes by ref_key, bounds
# by ``ingest.chained.max_detail_fetches`` and fetches best-effort; every feature survives the merge.


def fetch_detail_set(
    spec: SourceSpec, ref_plans: list[tuple[str, Any]], cap: int
) -> dict[str, DetailResult]:
    """Fetch the ``(ref_key, RequestPlan)`` set deduped by key and bounded by ``cap``; a per-ref failure records its error and proceeds."""
    results: dict[str, DetailResult] = {}
    fetched = 0
    capped = False
    for ref_key, plan in ref_plans:
        if ref_key in results:
            continue
        if fetched >= cap:
            capped = True
            results[ref_key] = DetailResult(error="detail-fetch cap reached")
            continue
        fetched += 1
        try:
            results[ref_key] = DetailResult(body=_get(spec, plan))
        except RouterError as exc:
            logger.info("router.chained: detail ref %s failed (best-effort skip): %s", ref_key, exc)
            results[ref_key] = DetailResult(error=str(exc))
    if capped:
        logger.warning(
            "router.chained: detail-fetch cap (%d) reached for source=%s; some items "
            "keep partial/null detail", cap, spec.source_class,
        )
    logger.info(
        "router.chained: %d distinct detail fetch(es) for source=%s", fetched, spec.source_class,
    )
    return results


def _enrich(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if spec.hooks is not None and spec.hooks.enrich_plan:
        plan_for = resolve_hook(spec.hooks.enrich_plan)
        merge = resolve_hook(spec.hooks.enrich_merge)  # type: ignore[arg-type]
    else:
        plan_for, merge = field_map.enrich_plans, field_map.enrich_merge
    ref_plans = list(plan_for(spec, params, features))
    enrich_block = (spec.ingest or {}).get("enrich") or _chained_block(spec)
    cap = int(enrich_block.get("max_detail_fetches", _DEFAULT_MAX_DETAIL_FETCHES))
    results = fetch_detail_set(spec, ref_plans, cap) if ref_plans else {}
    return merge(spec, params, features, results)




def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    bodies = _fetch_main(spec, params)
    features = _features(spec, params, bodies)
    if (spec.hooks and spec.hooks.enrich_plan) or (spec.ingest or {}).get("enrich"):
        features = _enrich(spec, params, features)
    return features_to_fgb_bytes(features, spec, params)
