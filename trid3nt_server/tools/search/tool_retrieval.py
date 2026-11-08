"""Which subset of the tool catalog the model sees for a turn: CORE_FLOOR, UNION the
tools the model has called this Case, UNION for each step of the message its best match
and every tool within MATCH_MARGIN of it by embedding similarity, capped at the
TOOL_RETRIEVAL_K ceiling. Nothing here builds the index; a cold index or a fault FAILS
OPEN to the full registry, since dropping a tool is a silent break."""

from __future__ import annotations

import logging
import re
from typing import Any

from trid3nt_server.settings import setting
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
    "retrieve_matched_tools",
    "ranked_docs",
    "doc_similarities",
    "split_steps",
    "CORE_FLOOR",
    "DEFAULT_K",
    "MAX_K",
]

logger = logging.getLogger("trid3nt_server.tools.search.tool_retrieval")

#: The ceiling on the tools a turn's matches add, and the clamp on the TOOL_RETRIEVAL_K setting.
DEFAULT_K = 8
MAX_K = 200

# Search is the front door: a large, documented surface touched occasionally is ranked; a small one needed on most turns is enumerated.
# A browse TREE is refused: it multiplies wrong turns, each a full round trip, and grows dead ends faster than the corpus.

#: The always-visible floor: tools that must NEVER be retrieved out - the prerequisite primitives, the discovery escape hatch, cross-cutting actions.
#: No engine template (flooring one biases every turn), no publish tool (emission is automatic), no data source (found through find_sources, called through fetch).
CORE_FLOOR: frozenset[str] = frozenset(
    {
        "geocode_location",
        "find_sources",
        "fetch",
        "search_tools",
        "run_qgis_algorithm",
        "run_pyqgis",
        "request_spatial_input",
        "generate_chart",
    }
)


def doc_similarities(query_clean: str, index: Any) -> "list[float] | None":
    """The cosine similarity of the query to every document of the index (its best row's), or ``None`` with no dense channel.

    The dense matrix and the query vector are both normalized, so the product IS the cosine: the confidence a match is cut on."""
    # A positive allowlist of CPU-local backends excludes any future network-backed one by default; a per-query network encode would be hot-path I/O.
    if (
        index.dense_matrix is None
        or index.dense_encode_fn is None
        or getattr(index, "backend_name", None)
        not in ("sentence_transformers", "hashed", None)
    ):
        return None
    try:
        import numpy as _np

        q_vec = index.dense_encode_fn([query_clean])
        qn = _np.linalg.norm(q_vec, axis=1, keepdims=True)
        qn[qn == 0.0] = 1.0
        q_vec = q_vec / qn
        rows = index.dense_matrix @ q_vec[0]
        owner = getattr(index, "dense_owner", None)
        if owner is None:
            return [float(v) for v in rows]
        best = _np.full(len(index.tool_names), -1.0, dtype="float32")
        _np.maximum.at(best, _np.asarray(owner), rows)
        return [float(v) for v in best]
    except Exception:  # noqa: BLE001
        logger.warning("tool_retrieval: dense channel failed", exc_info=True)
        return None


def _build_channel_rankings(
    query_clean: str, index: Any
) -> tuple[list[list[int]], list[int]]:
    """The three sync ranking channels over the CACHED index, as rank lists of tool
    indices. Pure CPU; NEVER builds the index. The BM25 rank list is returned separately for the lexical reinforcement."""
    rankings: list[list[int]] = []
    bm25_ranking: list[int] = []

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

    sims = doc_similarities(query_clean, index)
    if sims is not None:
        dense_ranking = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)
        if dense_ranking:
            rankings.append(dense_ranking)

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
        # A substring fallback over tool names, so the routing still produces
        # something when no channel ranked.
        substr = [
            i for i, name in enumerate(index.tool_names)
            if query_clean.lower() in name.lower()
        ]
        if substr:
            rankings = [substr]
    return rankings, bm25_ranking


def ranked_docs(query_clean: str, index: Any) -> list[tuple[int, float]]:
    """THE ranking: the channels fused by RRF, then the lexical champion's bonus, as
    ``[(doc_index, score)]`` descending over the CACHED index. ``[]`` when no
    channel ranked."""
    rankings, bm25_ranking = _build_channel_rankings(query_clean, index)
    if not rankings:
        return []
    return _lexical_reinforcement(
        _reciprocal_rank_fusion(rankings, k=60), bm25_ranking, k=60)


#: Words that open an action phrase: an "and" followed by one joins two steps, not two nouns.
_STEP_VERBS = (
    "fetch|get|pull|download|load|find|search|show|map|plot|draw|chart|graph|display|compute|calculate|run|simulate|model|"
    "clip|mask|overlay|intersect|buffer|reproject|convert|export|extract|summari[sz]e|compare|generate|build|make|create|"
    "route|geocode|locate|estimate|derive|rank|count|measure|mosaic|merge|resample|classify"
)
_STEP_SPLIT = re.compile(
    rf"\s*(?:;|,?\s+and then\s+|,?\s+then\s+|,?\s+after that\s+|\s+and\s+(?=(?:{_STEP_VERBS})\b))\s*",
    re.IGNORECASE,
)


def split_steps(text: str) -> list[str]:
    """The message cut into its steps at "then", "and then", "after that", ";" and an "and" that joins two action phrases.

    No model reads it: a message with none of those is one step, and each step is ranked on its own."""
    parts = [p.strip(" ,.") for p in _STEP_SPLIT.split(text.strip())]
    steps = [p for p in parts if p]
    return steps or [text.strip()]


def _step_matches(step: str, index: Any, margin: float, minimum: float) -> list[tuple[str, float]]:
    """One step's tools: the best by similarity and every tool within ``margin`` of it, best RRF order first; none below ``minimum``.

    A tool with several documents (find_sources has one per data class and source) counts at its best one."""
    sims = doc_similarities(step, index)
    if sims is None:
        return []
    best: dict[str, float] = {}
    for at, name in enumerate(index.tool_names):
        if sims[at] > best.get(name, -1.0):
            best[name] = sims[at]
    top = max(best.values())
    if top < minimum:
        return []
    kept = {name for name, sim in best.items() if sim >= top * (1.0 - margin)}
    ordered: list[str] = []
    for at, _score in ranked_docs(step, index):
        name = index.tool_names[at]
        if name in kept and name not in ordered:
            ordered.append(name)
    ordered += sorted(kept - set(ordered), key=lambda n: -best[n])
    return [(name, best[name]) for name in ordered]


def retrieve_matched_tools(user_text: str, k: int = DEFAULT_K) -> list[tuple[str, float]]:
    """The tools the message's steps match, as ``[(name, similarity)]``, at most ``k``.

    The steps' matches are taken in turn, one from each step per round, so a late step's best match is not crowded out by an early step's runners-up.
    ``[]`` on a cold index, no dense channel or no step above the minimum similarity; a fault raises."""
    if not isinstance(user_text, str) or not user_text.strip():
        return []
    index = _dd._INDEX  # live module global; None until the orchestrator warms it
    if index is None or not getattr(index, "tool_names", None):
        return []  # cold -- never build on the hot path
    try:
        k = max(1, min(int(k), MAX_K))
    except (TypeError, ValueError):
        k = DEFAULT_K
    margin = float(setting("MATCH_MARGIN"))
    minimum = float(setting("MIN_MATCH_SIMILARITY"))
    per_step = [_step_matches(step, index, margin, minimum) for step in split_steps(user_text)]
    matched: list[tuple[str, float]] = []
    seen: set[str] = set()
    for rank in range(max((len(rows) for rows in per_step), default=0)):
        for rows in per_step:
            if rank < len(rows) and rows[rank][0] not in seen and len(matched) < k:
                seen.add(rows[rank][0])
                matched.append(rows[rank])
    return matched


def _full_registry_floor(floor: set[str]) -> set[str]:
    """The FAIL-OPEN result: every model-facing registered tool UNION the core floor.

    The FULL registry is populated first, because a coded tool registers only when its module is imported."""
    try:
        import trid3nt_server.main as _main

        _main._import_tools_registry()
    except Exception:  # noqa: BLE001 -- a degraded snapshot is still a HOT_SET superset
        logger.warning(
            "tool_retrieval: full-registry import failed on fail-open", exc_info=True
        )
    # Engine templates (tier=template) are ordinary retrieval-pool members, so the
    # FAIL-OPEN dump INCLUDES them. Only tier="internal" (an absorbed in-process
    # seam: registry-resolvable, never model-facing) stays out of the visible set, and so does a data source.
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    sources = registered_spec_names()
    visible = {
        name
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general") != "internal"
        and name not in sources
    }
    return visible | floor


def retrieve_visible_tools(
    user_text: str,
    called: "set[str] | frozenset[str] | None",
    k: int = DEFAULT_K,
) -> set[str]:
    """The set of tool names to make visible for one turn: the floor, the tools ``called`` this Case, and what the message's steps match.

    ``k`` is the ceiling on the matches, clamped to ``[1, MAX_K]``, never a fill. A message that matches nothing gets the floor
    and the called tools, with ``search_tools`` to reach the rest; a cold index or a fault gets the full registry."""
    # A MOUNTED tool joins the floor while its session is open: no ranking channel can surface it.
    floor: set[str] = set(CORE_FLOOR) | set(mounted_tool_names())
    if called:
        # A data source a Case called before sources left the declared sets is not declared again.
        from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

        floor |= set(called) - registered_spec_names()

    if not isinstance(user_text, str) or not user_text.strip():
        return floor

    index = _dd._INDEX
    if index is None or not getattr(index, "tool_names", None) or index.dense_matrix is None:
        logger.info("tool_retrieval: no dense index; FAIL-OPEN to full registry")
        return _full_registry_floor(floor)
    try:
        matched = retrieve_matched_tools(user_text, k)
    except Exception:  # noqa: BLE001 -- fail open, never break dispatch
        logger.warning("tool_retrieval: matching failed; FAIL-OPEN to full registry", exc_info=True)
        return _full_registry_floor(floor)
    return floor | {name for name, _sim in matched}
