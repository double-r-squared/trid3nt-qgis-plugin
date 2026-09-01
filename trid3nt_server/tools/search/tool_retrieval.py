"""``retrieve_visible_tools`` -- case-stable, monotonic-grow tool selection.

This is the BUILT-IN surfacing path: it decides WHICH subset of the tool catalog
the model sees for a turn, so the per-turn tool list (and its ~41-46k tokens) stays
trimmed to what the ask needs. Enforce is unconditional -- ``K`` is the only lever
(``TRID3NT_TOOL_RETRIEVAL_K``).

Design:
  visible(turn) = ``CORE_FLOOR``
                  UNION the Case's accumulated visible set (every tool once made
                      visible this Case -- so a tool never leaves mid-task)
                  UNION the top-k RRF ranking for the turn's user_text.

Properties (asserted in tests):
  * DETERMINISTIC -- same (user_text, accrued state) -> same result.
  * NO hot-path I/O beyond the CACHED discover index lookup -- it never builds the
    index (that would block on a cold model load); the orchestrator warms it at
    startup via asyncio.to_thread. If the index is still cold, FAIL-OPEN.
  * CORE FLOOR -- ``CORE_FLOOR`` is ALWAYS a subset of the result.
  * NEVER HIDE MID-TASK -- the result always contains everything in the Case's
    accrued visible set; it composes by UNION, so the visible set only grows.
  * FAIL-OPEN -- any error, a cold index, or an empty ranking returns the FULL
    registry (logged). Over-inclusion is cheap; dropping a needed tool is a silent
    break, so recall@k is optimized, not precision.

Reuse: the ranking reuses ``search_tools``'s cached index, tokenizer, RRF, and
corpus 100% (no new infra). The 3 sync channels (BM25 + local-dense + name-substr)
mirror ``search_tools``'s inline ranking (search_tools.py ~L1073-1182) MINUS
its async Mongo co-occurrence channel, which cannot run on this synchronous path.

ASCII only.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.tools import TOOL_REGISTRY, mounted_tool_names
from trid3nt_server.tools.search.search_tools import search_tools as _dd
from trid3nt_server.tools.search.search_tools.search_tools import (
    _NAME_RANKER_GENERICS,
    _STOPWORDS,
    _lexical_reinforcement,
    _reciprocal_rank_fusion,
    _tokenize,
)

__all__ = [
    "retrieve_visible_tools",
    "retrieve_ranked_tools",
    "CORE_FLOOR",
    "DEFAULT_K",
    "MAX_K",
]

logger = logging.getLogger("trid3nt_server.tools.search.tool_retrieval")

#: search_tools top-k default + clamp ceiling (kickoff: k default 25, [1, 25]).
DEFAULT_K = 25
MAX_K = 25

#: The always-visible floor -- tools that must NEVER be retrieved out, regardless
#: of the turn's ranking: the "before you can do anything else" primitives
#: (geocode, DEM, weather alerts CONUS + state-scoped), the discovery escape
#: hatch (search_tools), and the cross-cutting view/analysis actions a user
#: reaches for at any point (code exec, layer bounds, spatial input, chart,
#: spatial query). retrieve_visible_tools and the openai tool-gating floor both
#: union this set.
#:
#: No engine template belongs in this floor either: a template answers ONE
#: question class, so flooring one biases every turn toward it. Templates reach
#: the model through the turn's ranking, which the corpus-first retrieval matrix
#: pins.
#:
#: No publish tool belongs in this floor: emission is automatic, so there is no
#: "display this" intent for the model to route to. The mechanism lives in
#: ``trid3nt_server/emission/publish.py`` and runs on every renderable layer
#: without being asked.
CORE_FLOOR: frozenset[str] = frozenset(
    {
        "geocode_location",
        "fetch_dem",
        "fetch_nws_alerts_conus",
        "fetch_nws_event",
        "search_tools",
        "code_exec_request",
        "compute_layer_bounds",
        "request_spatial_input",
        "generate_chart",
        "spatial_query",
    }
)


def _build_channel_rankings(
    query_clean: str, index: Any
) -> tuple[list[list[int]], list[int]]:
    """The 3 sync ranking channels (BM25 + local dense + name-substring) over
    the CACHED discover index, as rank lists of tool indices.

    Split out of ``_discover_topk`` (Stage 3) so the scored
    variant ``retrieve_ranked_tools`` fuses the SAME channels -- the visible-set
    and the ambiguity-margin paths can never drift apart. Pure CPU; never
    builds the index.

    Returns ``(rankings, bm25_ranking)``; the BM25 channel's rank list is
    surfaced separately so both callers can feed it to
    ``_lexical_reinforcement`` (the door / lexical-champion boost) without
    re-deriving which channel is BM25.
    """
    rankings: list[list[int]] = []
    bm25_ranking: list[int] = []

    # --- BM25 channel ---
    if index.bm25 is not None:
        q_tokens = _tokenize(query_clean)
        if q_tokens:
            try:
                raw = index.bm25.get_scores(q_tokens)
                order = sorted(range(len(raw)), key=lambda i: float(raw[i]), reverse=True)
                bm25_ranking = [i for i in order if float(raw[i]) > 0.0]
                if bm25_ranking:
                    rankings.append(bm25_ranking)
            except Exception:  # noqa: BLE001 -- drop the channel, keep the others
                logger.warning("tool_retrieval: BM25 channel failed", exc_info=True)
                bm25_ranking = []

    # --- Dense channel (LOCAL backends only; skip Vertex network encode) ---
    # Positive allowlist of the known CPU-local backends so any FUTURE network
    # backend is excluded by default, not by omission.
    if (
        index.dense_matrix is not None
        and index.dense_encode_fn is not None
        and getattr(index, "backend_name", None)
        in ("sentence_transformers", "hashed", None)
    ):
        try:
            import numpy as _np

            q_vec = index.dense_encode_fn([query_clean])
            qn = _np.linalg.norm(q_vec, axis=1, keepdims=True)
            qn[qn == 0.0] = 1.0
            q_vec = q_vec / qn
            sims = (index.dense_matrix @ q_vec[0]).astype("float32")
            dense_ranking = sorted(range(len(sims)), key=lambda i: float(sims[i]), reverse=True)
            if dense_ranking:
                rankings.append(dense_ranking)
        except Exception:  # noqa: BLE001
            logger.warning("tool_retrieval: dense channel failed", exc_info=True)

    # --- Name-substring channel ---
    q_content = [
        t for t in _tokenize(query_clean)
        if t not in _STOPWORDS and t not in _NAME_RANKER_GENERICS
    ]
    if q_content:
        scored: list[tuple[int, int]] = []
        for i, name in enumerate(index.tool_names):
            name_low = name.lower()
            hits = sum(1 for t in q_content if t in name_low)
            stem_hits = 0
            for t in q_content:
                stem = t
                for suf in ("ing", "ed", "s"):
                    if stem.endswith(suf) and len(stem) > len(suf) + 2:
                        stem = stem[: -len(suf)]
                        break
                if stem != t and stem in name_low:
                    stem_hits += 1
            total = hits + stem_hits
            if total > 0:
                scored.append((total, i))
        scored.sort(key=lambda p: p[0], reverse=True)
        name_ranking = [i for _, i in scored]
        if name_ranking:
            rankings.append(name_ranking)

    if not rankings:
        # substring fallback over tool names (mirrors search_tools).
        substr = [
            i for i, name in enumerate(index.tool_names)
            if query_clean.lower() in name.lower()
        ]
        if substr:
            rankings = [substr]
    return rankings, bm25_ranking


def _discover_topk(user_text: str, k: int) -> set[str] | None:
    """Top-k tool names ranked by relevance to ``user_text`` via the CACHED
    discover index (BM25 + name-substring + LOCAL dense).

    Returns ``None`` when the index is COLD (not yet warmed) so the caller can
    FAIL-OPEN without triggering a blocking cold model build on the hot path; an
    empty ``set()`` when the index is warm but nothing matched.

    Mirrors ``search_tools``'s inline ranking minus the async Mongo
    co-occurrence channel, reusing that module's primitives so the paths stay
    aligned. The network-backed Vertex dense backend's per-query encode is skipped
    here (it would be hot-path I/O); local sentence-transformers / hashed dense and
    BM25 are pure-CPU against the cached index.
    """
    query_clean = user_text.strip()
    index = _dd._INDEX  # live module global; None until the orchestrator warms it
    if index is None or not getattr(index, "tool_names", None):
        return None  # cold -- never build on the hot path; caller fail-opens

    rankings, bm25_ranking = _build_channel_rankings(query_clean, index)
    if not rankings:
        return set()

    fused = _reciprocal_rank_fusion(rankings, k=60)
    fused = _lexical_reinforcement(
        fused, bm25_ranking, getattr(index, "tiers", None), k=60
    )
    names: set[str] = set()
    for idx, _score in fused[:k]:
        names.add(index.tool_names[idx])
    return names


def retrieve_ranked_tools(
    user_text: str, k: int = DEFAULT_K
) -> list[tuple[str, float]]:
    """Ranked ``(tool_name, rrf_score)`` list for one turn's query (Stage 3).

    The SCORED face of the same 3-channel RRF ranking ``retrieve_visible_tools``
    uses -- feeds (a) the openai-provider top-k tool gating and (b) the
    ambiguity signal (top-1 vs top-2 margin). Scores are the raw RRF fusion
    values (rank-derived, NOT probabilities; only their ordering + relative
    margin are meaningful).

    Returns ``[]`` when the index is COLD, the query is empty, or nothing
    matched -- callers MUST fail open (no gating / no ambiguity ask) on an
    empty result. Never raises on the hot path; any channel fault degrades to
    the surviving channels exactly like ``retrieve_visible_tools``.
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return []
    query_clean = user_text.strip()
    index = _dd._INDEX  # live module global; None until the orchestrator warms it
    if index is None or not getattr(index, "tool_names", None):
        return []  # cold -- caller fails open
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = DEFAULT_K
    k = max(1, min(k, len(index.tool_names)))
    try:
        rankings, bm25_ranking = _build_channel_rankings(query_clean, index)
    except Exception:  # noqa: BLE001 -- fail open, never break dispatch
        logger.warning("retrieve_ranked_tools: channel build failed", exc_info=True)
        return []
    if not rankings:
        return []
    fused = _reciprocal_rank_fusion(rankings, k=60)
    fused = _lexical_reinforcement(
        fused, bm25_ranking, getattr(index, "tiers", None), k=60
    )
    return [
        (index.tool_names[idx], float(score)) for idx, score in fused[:k]
    ]


def _full_registry_floor(floor: set[str]) -> set[str]:
    """The FAIL-OPEN result: every registered tool UNION the core floor.

    Ensures the FULL registry is populated first: the catalog tools
    (search_data_catalog / fetch_from_catalog) register ONLY via the startup
    import path, NOT via tools/__init__, so without this the fail-open
    snapshot is short by those real tools in any process where the startup
    hook has not yet run
    (tool-retrieval verify, 2026-06-23). Idempotent + guarded; only the rare
    fail-open path pays for it.
    """
    try:
        import trid3nt_server.main as _main

        _main._import_tools_registry()
    except Exception:  # noqa: BLE001 -- a degraded snapshot is still a HOT_SET superset
        logger.warning(
            "tool_retrieval: full-registry import failed on fail-open", exc_info=True
        )
    # Door dissolution: engine templates (tier=template) are ordinary
    # retrieval-pool members, so the FAIL-OPEN dump INCLUDES them. Only
    # tier="catalog" (catalog-surfacing experiment, arm-flagged; no tool carries
    # it in the DEFAULT config) and tier="internal" (an absorbed in-process seam,
    # e.g. fetch_copernicus_dem -- registry-resolvable but never model-facing)
    # stay out of the visible set.
    visible = {
        name
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general")
        not in ("catalog", "internal")
    }
    return visible | floor


def retrieve_visible_tools(
    user_text: str,
    accrued: "set[str] | frozenset[str] | None",
    k: int = DEFAULT_K,
) -> set[str]:
    """Select the set of tool names to make visible for one turn.

    See the module docstring for the design + invariants. ``accrued`` is the
    Case's monotonic visible set (may be ``None`` on a brand-new turn); ``k``
    is the discover top-k, clamped to ``[1, MAX_K]``.
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = DEFAULT_K
    k = max(1, min(k, MAX_K))

    # --- Core floor + the Case's accumulated visible set (ALWAYS included). ---
    # The accrued set carries every tool once made visible this Case -> the
    # NEVER-HIDE-MID-TASK guarantee. A MOUNTED tool joins the floor for as long
    # as its session is open: it did not exist when the index was built, so no
    # ranking channel can surface it.
    floor: set[str] = set(CORE_FLOOR) | set(mounted_tool_names())
    if accrued:
        floor |= set(accrued)

    # --- No query -> floor only (nothing to rank; do NOT dump the full catalog). ---
    if not isinstance(user_text, str) or not user_text.strip():
        return floor

    # --- Query relevance via the cached discover index. FAIL-OPEN on any fault. ---
    try:
        topk = _discover_topk(user_text, k)
    except Exception:  # noqa: BLE001
        logger.warning(
            "tool_retrieval: discovery raised; FAIL-OPEN to full registry",
            exc_info=True,
        )
        return _full_registry_floor(floor)

    if topk is None:
        logger.info("tool_retrieval: discover index COLD; FAIL-OPEN to full registry")
        return _full_registry_floor(floor)
    if not topk:
        # warm index but nothing matched -> be safe, show everything (recall floor).
        logger.info(
            "tool_retrieval: empty ranking for %r; FAIL-OPEN to full registry",
            user_text[:80],
        )
        return _full_registry_floor(floor)

    return floor | topk
