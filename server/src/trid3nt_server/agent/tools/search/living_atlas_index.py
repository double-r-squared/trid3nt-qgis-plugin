"""BM25 + dense retrieval over the harvested Living Atlas entries.

Reuses the SAME machinery as the tool-discovery index (``search_tools``'s
``_tokenize`` / ``_TypoTolerantBM25`` / ``_select_dense_backend`` /
``_reciprocal_rank_fusion``) -- exactly how ``_router/stratified.py`` reuses it for
the (gated) source pool -- but runs it over the Living Atlas catalog ENTRIES
(title + snippet + tags), not the tool registry.

Two-pool structure (NATE's rule): a SEPARATE index per curation stratum, built
lazily and independently, so the community pool can never crowd the authoritative
ranking. ``rank_stratum`` fuses BM25 + dense per stratum with no fused leaderboard
across strata; the composition policy (authoritative-first, community only on
opt-in / last-resort) lives in the ``search_living_atlas`` tool.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from trid3nt_server.agent.tools.search.living_atlas_common import (
    CurationClass,
    LivingAtlasEntry,
    load_living_atlas,
)
from trid3nt_server.agent.tools.search.search_tools.search_tools import (
    _reciprocal_rank_fusion,
    _select_dense_backend,
    _tokenize,
    _TypoTolerantBM25,
)

__all__ = ["rank_stratum", "reset_index"]

logger = logging.getLogger("trid3nt_server.agent.tools.search.living_atlas_index")


def _entry_document(entry: LivingAtlasEntry) -> str:
    """The indexed text for one entry: title doubled (BM25 name-bias) + snippet + tags."""
    parts = [entry.title, entry.title, entry.snippet, " ".join(entry.tags)]
    return "\n".join(p for p in parts if p)


class _EntryIndex:
    """A BM25(+dense) index over one stratum's entries (built once, then queried)."""

    def __init__(self, entries: list[LivingAtlasEntry]) -> None:
        self.entries = entries
        documents = [_entry_document(e) for e in entries]
        corpus_tokens = [_tokenize(d) for d in documents]
        vocabulary = frozenset(tok for toks in corpus_tokens for tok in toks)

        self.bm25 = None
        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

            if corpus_tokens:
                self.bm25 = _TypoTolerantBM25(BM25Okapi(corpus_tokens), vocabulary)
        except Exception as exc:  # noqa: BLE001 -- BM25 optional, degrade gracefully
            logger.warning("living_atlas_index: BM25 disabled (%s)", exc)

        # Dense is optional; reuse the shared backend selector (CPU-local backends
        # only are used at query time -- a network backend is skipped, matching
        # tool_retrieval). Encode is one-time at build.
        self.dense_matrix = None
        self.dense_encode_fn = None
        self.backend_name = None
        backend = _select_dense_backend()
        if backend is not None and documents:
            encode_fn, _np, backend_name = backend
            try:
                self.dense_matrix = encode_fn(documents)
                self.dense_encode_fn = encode_fn
                self.backend_name = backend_name
            except Exception as exc:  # noqa: BLE001 -- non-fatal
                logger.warning("living_atlas_index: dense disabled (%s)", exc)
                self.dense_matrix = None
                self.dense_encode_fn = None
                self.backend_name = None

    def rank(self, query: str, k: int) -> list[tuple[LivingAtlasEntry, float]]:
        """Top-k ``(entry, rrf_score)`` for ``query`` (BM25 + local dense, RRF-fused)."""
        if not self.entries:
            return []
        query_clean = (query or "").strip()
        rankings: list[list[int]] = []

        if self.bm25 is not None:
            q_tokens = _tokenize(query_clean)
            if q_tokens:
                try:
                    raw = self.bm25.get_scores(q_tokens)
                    order = sorted(range(len(raw)), key=lambda i: float(raw[i]), reverse=True)
                    bm25_ranking = [i for i in order if float(raw[i]) > 0.0]
                    if bm25_ranking:
                        rankings.append(bm25_ranking)
                except Exception:  # noqa: BLE001
                    logger.warning("living_atlas_index: BM25 channel failed", exc_info=True)

        if (
            self.dense_matrix is not None
            and self.dense_encode_fn is not None
            and self.backend_name in ("sentence_transformers", "hashed", None)
        ):
            try:
                import numpy as _np

                q_vec = self.dense_encode_fn([query_clean])
                qn = _np.linalg.norm(q_vec, axis=1, keepdims=True)
                qn[qn == 0.0] = 1.0
                q_vec = q_vec / qn
                sims = (self.dense_matrix @ q_vec[0]).astype("float32")
                dense_ranking = sorted(range(len(sims)), key=lambda i: float(sims[i]), reverse=True)
                if dense_ranking:
                    rankings.append(dense_ranking)
            except Exception:  # noqa: BLE001
                logger.warning("living_atlas_index: dense channel failed", exc_info=True)

        if not rankings:
            # substring fallback over titles (mirrors search_tools).
            substr = [
                i for i, e in enumerate(self.entries)
                if query_clean.lower() in e.title.lower()
            ]
            if not substr:
                return []
            rankings = [substr]

        fused = _reciprocal_rank_fusion(rankings, k=60)
        return [(self.entries[i], score) for i, score in fused[:k]]


_INDEXES: dict[CurationClass, _EntryIndex] = {}
_LOCK = threading.Lock()


def _get_index(curation: CurationClass) -> _EntryIndex:
    if curation not in _INDEXES:
        with _LOCK:
            if curation not in _INDEXES:
                _INDEXES[curation] = _EntryIndex(load_living_atlas(curation))
    return _INDEXES[curation]


def rank_stratum(
    query: str, curation: CurationClass, k: int
) -> list[tuple[LivingAtlasEntry, float]]:
    """Rank one curation stratum's entries for ``query`` (lazy per-stratum index)."""
    return _get_index(curation).rank(query, k)


def reset_index() -> None:
    """Drop the built indexes (test seam; also after a re-harvest)."""
    with _LOCK:
        _INDEXES.clear()
