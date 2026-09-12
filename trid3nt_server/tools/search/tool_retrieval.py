"""Which subset of the tool catalog the model sees for a turn: CORE_FLOOR, UNION
the Case's accrued visible set, UNION the top-k ranking of the turn's text.
Composing by UNION is what makes it DETERMINISTIC and monotonic - a tool never
leaves mid-task. Nothing here builds the index; a cold index, a fault or an empty
ranking FAILS OPEN to the full registry, since dropping a tool is a silent break."""

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

#: Top-k default and clamp ceiling for the turn's ranking.
DEFAULT_K = 25
MAX_K = 25

# SEARCH IS THE FRONT DOOR; ENUMERATION IS THE EXCEPTION. A surface that is
# large, documented and touched OCCASIONALLY per turn is reached by ranking it -
# the catalog, the data sources, the engine keywords. A surface that is SMALL
# and needed on MOST turns is enumerated, because a lookup for it would be pure
# overhead. A browse TREE over either is the shape this refuses: it multiplies
# wrong turns, each one a full round trip, and grows dead ends faster than the
# corpus grows wide.
#
# The revisit trigger is MEASURED, not felt: a registry of order a thousand tools
# AND a demonstrated fall in recall at k. Until both hold, a ranking is
# sub-millisecond CPU and a tree buys back no latency to pay for its errors.

#: The always-visible floor: tools that must NEVER be retrieved out whatever the
#: turn ranks - the "before you can do anything else" primitives, the discovery
#: escape hatch, and the cross-cutting actions a user reaches for at any point.
#:
#: No engine template belongs here: a template answers ONE question class, so
#: flooring one biases every turn toward it. Templates reach the model through the
#: turn's ranking instead.
#:
#: No publish tool belongs here: emission is automatic, so there is no "display
#: this" intent for the model to route to.
CORE_FLOOR: frozenset[str] = frozenset(
    {
        "geocode_location",
        "fetch_dem",
        "fetch_nws_alerts_conus",
        "fetch_nws_event",
        "search_tools",
        "run_qgis_algorithm",
        "run_pyqgis",
        "compute_layer_bounds",
        "request_spatial_input",
        "generate_chart",
    }
)


def _build_channel_rankings(
    query_clean: str, index: Any
) -> tuple[list[list[int]], list[int]]:
    """The three sync ranking channels over the CACHED index, as rank lists of
    tool indices. Pure CPU; NEVER builds the index. The BM25 rank list is returned
    separately so a caller can feed it to the lexical reinforcement."""
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

    # --- Dense channel, LOCAL backends only ---
    # A positive allowlist of the CPU-local backends, so any FUTURE network-backed
    # backend is excluded by default rather than by omission: a per-query network
    # encode here would be hot-path I/O.
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
    """Top-k tool names by relevance to ``user_text`` over the CACHED index.
    ``None`` means the index is COLD, so the caller fails open without triggering a
    blocking build; an empty set means warm but nothing matched."""
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
    """The SCORED face of the ranking ``retrieve_visible_tools`` uses. Scores are
    rank-derived, NOT probabilities, so only ordering and relative margin mean
    anything. ``[]`` on a cold index or no match, and the caller MUST fail open."""
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
    """The FAIL-OPEN result: every model-facing registered tool UNION the core
    floor. The FULL registry is populated first, because a coded tool registers
    only when its module is imported. Idempotent; only this rare path pays."""
    try:
        import trid3nt_server.main as _main

        _main._import_tools_registry()
    except Exception:  # noqa: BLE001 -- a degraded snapshot is still a HOT_SET superset
        logger.warning(
            "tool_retrieval: full-registry import failed on fail-open", exc_info=True
        )
    # Engine templates (tier=template) are ordinary retrieval-pool members, so the
    # FAIL-OPEN dump INCLUDES them. Only tier="catalog" (arm-flagged; no tool
    # carries it in the default config) and tier="internal" (an absorbed in-process
    # seam: registry-resolvable, never model-facing) stay out of the visible set.
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
    """The set of tool names to make visible for one turn. ``accrued`` is the
    Case's monotonic visible set, ``None`` on a brand-new turn; ``k`` is clamped to
    ``[1, MAX_K]``. An empty query returns the floor, never the full catalog."""
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
