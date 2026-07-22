"""``retrieve_visible_tools`` -- case-stable, monotonic-grow tool selection.

The tools-session half of the tool-retrieval feature (tool-retrieval kickoff,
NATE 2026-06-23). This is the PURE selection function the orchestrator wraps with
shadow telemetry + a recall@k dashboard; it decides WHICH subset of the ~122-tool
catalog is made visible to the model for a turn, so the per-turn tool list (and its
~41-46k tokens) can be trimmed once recall proves out (target recall@k >= 0.99).

Design (locked by the kickoff):
  visible(turn) = HOT_SET core floor
                  UNION the Case's accumulated ``AllowedToolSet`` (opened
                      categories + dispatched + explicit -- so a tool once
                      visible NEVER leaves within a Case)
                  UNION ``discover_dataset`` top-k RRF for the turn's user_text.

Properties (asserted in tests):
  * DETERMINISTIC -- same (user_text, allowed_set state) -> same result.
  * NO hot-path I/O beyond the CACHED discover index lookup -- it never builds the
    index (that would block on a cold model load); the orchestrator warms it at
    startup via asyncio.to_thread. If the index is still cold, FAIL-OPEN.
  * CORE FLOOR -- ``HOT_SET_TOOLS`` is ALWAYS a subset of the result.
  * NEVER HIDE MID-TASK -- the result always contains everything already in the
    Case's ``AllowedToolSet``; it composes by UNION, so the visible set only grows.
  * FAIL-OPEN -- any error, a cold index, or an empty ranking returns the FULL
    registry (logged). Over-inclusion is cheap; dropping a needed tool is a silent
    break, so recall@k is optimized, not precision.

Reuse: the ranking reuses ``discover_dataset``'s cached index, tokenizer, RRF, and
corpus 100% (no new infra). The 3 sync channels (BM25 + local-dense + name-substr)
mirror ``discover_dataset``'s inline ranking (discover_dataset.py ~L1073-1182) MINUS
its async Mongo co-occurrence channel, which cannot run on this synchronous path.

ASCII only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..categories import HOT_SET_TOOLS
from . import TOOL_REGISTRY
from . import discover_dataset as _dd
from .discover_dataset import (
    _NAME_RANKER_GENERICS,
    _STOPWORDS,
    _reciprocal_rank_fusion,
    _tokenize,
)

if TYPE_CHECKING:  # avoid a hard import cycle at module load
    from ..categories import AllowedToolSet

__all__ = ["retrieve_visible_tools", "DEFAULT_K", "MAX_K"]

logger = logging.getLogger("grace2_agent.tools.tool_retrieval")

#: discover_dataset top-k default + clamp ceiling (kickoff: k default 25, [1, 25]).
DEFAULT_K = 25
MAX_K = 25


def _discover_topk(user_text: str, k: int) -> set[str] | None:
    """Top-k tool names ranked by relevance to ``user_text`` via the CACHED
    discover index (BM25 + name-substring + LOCAL dense).

    Returns ``None`` when the index is COLD (not yet warmed) so the caller can
    FAIL-OPEN without triggering a blocking cold model build on the hot path; an
    empty ``set()`` when the index is warm but nothing matched.

    Mirrors ``discover_dataset``'s inline ranking minus the async Mongo
    co-occurrence channel, reusing that module's primitives so the paths stay
    aligned. The network-backed Vertex dense backend's per-query encode is skipped
    here (it would be hot-path I/O); local sentence-transformers / hashed dense and
    BM25 are pure-CPU against the cached index.
    """
    query_clean = user_text.strip()
    index = _dd._INDEX  # live module global; None until the orchestrator warms it
    if index is None or not getattr(index, "tool_names", None):
        return None  # cold -- never build on the hot path; caller fail-opens

    rankings: list[list[int]] = []

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
        # substring fallback over tool names (mirrors discover_dataset).
        substr = [
            i for i, name in enumerate(index.tool_names)
            if query_clean.lower() in name.lower()
        ]
        if substr:
            rankings = [substr]
    if not rankings:
        return set()

    fused = _reciprocal_rank_fusion(rankings, k=60)
    names: set[str] = set()
    for idx, _score in fused[:k]:
        names.add(index.tool_names[idx])
    return names


def _full_registry_floor(floor: set[str]) -> set[str]:
    """The FAIL-OPEN result: every registered tool UNION the core floor.

    Ensures the FULL registry is populated first: the catalog + qgis_discovery
    tools (catalog_search / catalog_fetch / list_qgis_algorithms /
    describe_qgis_algorithm) register ONLY via the startup import path, NOT via
    tools/__init__, so without this the fail-open snapshot is short by those 4
    real tools in any process where the startup hook has not yet run
    (tool-retrieval verify, 2026-06-23). Idempotent + guarded; only the rare
    fail-open path pays for it.
    """
    try:
        import grace2_agent.main as _main

        _main._import_tools_registry()
    except Exception:  # noqa: BLE001 -- a degraded snapshot is still a HOT_SET superset
        logger.warning(
            "tool_retrieval: full-registry import failed on fail-open", exc_info=True
        )
    return set(TOOL_REGISTRY) | floor


def retrieve_visible_tools(
    user_text: str,
    allowed_set: "AllowedToolSet | None",
    k: int = DEFAULT_K,
) -> set[str]:
    """Select the set of tool names to make visible for one turn.

    See the module docstring for the design + invariants. ``allowed_set`` is the
    Case's monotonic ``AllowedToolSet`` (may be ``None`` on a brand-new turn); ``k``
    is the discover top-k, clamped to ``[1, MAX_K]``.
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = DEFAULT_K
    k = max(1, min(k, MAX_K))

    # --- Core floor + the Case's accumulated allowed set (ALWAYS included). ---
    # HOT_SET is unioned EXPLICITLY: as_frozenset() may swap the hot-set slot for a
    # (possibly smaller) dynamic hot set, so unioning HOT_SET_TOOLS guarantees the
    # CORE-FLOOR invariant regardless. as_frozenset() carries opened-category tools
    # + dispatched + explicit -> the NEVER-HIDE-MID-TASK guarantee.
    floor: set[str] = set(HOT_SET_TOOLS)
    if allowed_set is not None:
        try:
            floor |= set(allowed_set.as_frozenset())
        except Exception:  # noqa: BLE001 -- never SILENTLY drop the Case's accrued tools
            # FAIL-OPEN to the full registry (not HOT_SET-only) so a once-visible
            # dispatched/explicit/opened-category tool is never hidden mid-task
            # (tool-retrieval verify, 2026-06-23).
            logger.warning(
                "tool_retrieval: allowed_set snapshot failed; FAIL-OPEN to full registry",
                exc_info=True,
            )
            return _full_registry_floor(floor)

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
