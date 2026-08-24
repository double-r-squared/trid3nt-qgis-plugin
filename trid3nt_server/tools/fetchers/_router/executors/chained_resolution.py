"""chained_resolution executor: resolve-then-fetch + bounded per-item
detail enrichment -- the promotion of the hook-ratchet's 4x MANDATORY-REVIEW pattern.

ONE mode, two composable phases; a source declares only the phase(s) it needs. The
router owns ALL orchestration (the round trips, the offset-paging loop, the
deduped/bounded/best-effort detail loop, the honest per-ref error aggregation); the
source-specific PURE compute lives in named hooks at each edge (build the request,
decode the body, derive the refs, merge the detail).

PHASE R -- resolve (name -> id), PRE-cache-key. :func:`pre_resolve` runs in
``router.route()`` before ``read_through``: the ``resolve_build`` hook builds the
resolution request(s) (or ``[]`` to skip), the router GETs them, the ``resolve_parse``
hook returns a params-merge dict (the resolved id) the router folds into ``params`` so
a name query and its id query collapse to one cache entry (the twin's contract).

MAIN FETCH -- ``build_request`` builds page 1; the optional ``next_page`` hook drives
offset paging (return the next page's plan given the pages so far, or ``None`` to stop);
``parse_response`` decodes the bodies into features.

PHASE E -- enrich (list -> per-item detail). The ``enrich_plan`` hook emits the ordered
``(ref_key, RequestPlan)`` detail set (already sliced to the source's per-pass cap); the
router dedupes by ``ref_key``, bounds by ``ingest.chained.max_detail_fetches``, and fetches
each best-effort (a failed ref records its error, never a silent drop); the ``enrich_merge``
hook folds the ``{ref_key: DetailResult}`` map back into the features (every feature survives).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import RouterError, router_input_error
from ..hooks import resolve_hook
from .http_json import _get
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.chained_resolution"
)

__all__ = ["DetailResult", "pre_resolve", "execute", "fetch_detail_set"]

#: Hard safety ceiling on the offset-paging loop (per-source stop logic lives in the
#: ``next_page`` hook; this only guards a pathological non-terminating response).
_MAX_PAGES = 200

#: Default global cap on distinct detail fetches when a spec omits
#: ``ingest.chained.max_detail_fetches`` (generous; per-pass caps live in enrich_plan).
_DEFAULT_MAX_DETAIL_FETCHES = 3000


@dataclass(frozen=True)
class DetailResult:
    """One resolved detail ref: a body OR a typed error (never both meaningful).

    ``body`` is the raw response bytes on success; ``error`` is a human-readable
    string when the ref could not be fetched (transport error, or the global cap was
    reached). The merge hook reads whichever is set and keeps the owning feature
    regardless -- the never-silent-drop rule.
    """

    body: bytes | None = None
    error: str | None = None


def _chained_block(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("chained") or {}


# --------------------------------------------------------------------------- #
# PHASE R -- resolve (pre-cache-key, runs in route()).
# --------------------------------------------------------------------------- #


def pre_resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Run the resolve phase and return ``params`` with the resolved id merged in.

    No-op (returns ``params`` unchanged) when the spec declares no ``resolve_build``
    hook or the hook returns ``[]`` (already-canonical fast path). Called BEFORE
    ``read_through`` so the resolved id is part of the cache key.
    """
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


# --------------------------------------------------------------------------- #
# MAIN FETCH -- build_request (+ optional next_page offset paging).
# --------------------------------------------------------------------------- #


def _fetch_main(spec: SourceSpec, params: dict[str, Any]) -> list[bytes]:
    """Fetch the round-1 body/bodies: build_request page 1, then next_page paging."""
    build = resolve_hook(spec.hooks.build_request)  # type: ignore[union-attr]
    bodies = [_get(spec, plan) for plan in build(spec, params)]
    if not (spec.hooks and spec.hooks.next_page):
        return bodies
    nxt = resolve_hook(spec.hooks.next_page)
    # tolerate_page_error: a cursor-paged source over a flaky ArcGIS
    # cluster (fema_nfhl 500s unpredictably on later cursor pages) opts in to
    # treating a NON-first-page upstream failure as "cursor exhausted -> partial",
    # matching the twin's documented resilience. The first page always propagates
    # (no partial from a total upstream outage). No-op for every prior spec.
    tolerate = bool(_chained_block(spec).get("tolerate_page_error"))
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
        try:
            bodies.append(_get(spec, plan))
        except RouterError:
            if tolerate and bodies:
                logger.warning(
                    "router.chained: page %d upstream failure tolerated for source=%s; "
                    "returning partial (%d page(s))", page, spec.source_class, len(bodies),
                )
                break
            raise
        page += 1
    return bodies


# --------------------------------------------------------------------------- #
# PHASE E -- bounded / deduped / best-effort detail enrichment.
# --------------------------------------------------------------------------- #


def fetch_detail_set(
    spec: SourceSpec, ref_plans: list[tuple[str, Any]], cap: int
) -> dict[str, DetailResult]:
    """Fetch the ``(ref_key, RequestPlan)`` set: dedup by key, bound by ``cap``, best-effort.

    First occurrence of a ``ref_key`` wins (order preserved so the cap cuts the same
    refs the twin's first-seen loop would). Past the cap an unseen ref records a
    ``cap_reached`` error (kept, never silently dropped). A per-ref transport failure
    records its error and the loop proceeds -- the twin's per-item best-effort join.
    """
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
    plan_hook = resolve_hook(spec.hooks.enrich_plan)  # type: ignore[union-attr]
    merge_hook = resolve_hook(spec.hooks.enrich_merge)  # type: ignore[union-attr]
    ref_plans = list(plan_hook(spec, params, features))
    cap = int(_chained_block(spec).get("max_detail_fetches", _DEFAULT_MAX_DETAIL_FETCHES))
    results = fetch_detail_set(spec, ref_plans, cap) if ref_plans else {}
    return merge_hook(spec, params, features, results)


# --------------------------------------------------------------------------- #
# Executor entry point (the read_through fetch_fn body).
# --------------------------------------------------------------------------- #


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Main fetch (+ paging) -> parse -> optional detail enrichment -> FGB bytes."""
    bodies = _fetch_main(spec, params)
    parse = resolve_hook(spec.hooks.parse_response)  # type: ignore[union-attr]
    features = parse(spec, params, bodies)
    if spec.hooks and spec.hooks.enrich_plan:
        features = _enrich(spec, params, features)
    return features_to_fgb_bytes(features, spec, params)
