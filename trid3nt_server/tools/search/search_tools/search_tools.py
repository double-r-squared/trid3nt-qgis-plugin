"""``search_tools`` - hybrid BM25 + dense retrieval routing a free-text need to
the atomic tools that answer it. The corpus per tool is its own docstring plus the
practitioner phrasings beside it. Dense retrieval is OPPORTUNISTIC: real embeddings
when the library is present, a hashed lexical vector otherwise, with BM25 carrying
the load in that degraded mode. Fusion is rank-aware, so no normalization."""

from __future__ import annotations

import difflib
import functools
import hashlib
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import TOOL_REGISTRY

from trid3nt_server.tools import register_tool

__all__ = [
    "search_tools",
    "_reset_index_for_tests",
    "_build_index",
    "_tokenize",
    "_close_vocab_matches",
    "_expand_query_tokens",
    "_reciprocal_rank_fusion",
    "_lexical_reinforcement",
    "_LEX_REINFORCE_GATE_DOOR",
    "_LEX_REINFORCE_GATE_GENERAL",
    "SearchToolsError",
    "CorpusFormatError",
]

logger = logging.getLogger("trid3nt_server.tools.search.search_tools.search_tools")


class SearchToolsError(RuntimeError):
    """Base class for search_tools failures."""

    error_code: str = "SEARCH_TOOLS_ERROR"
    retryable: bool = False


class CorpusFormatError(SearchToolsError):
    """A corpus YAML entry under a tool key is not a string - almost always an
    unquoted phrasing containing a colon, which parses as a one-key dict. REFUSED
    rather than dropped: a dropped phrasing vanishes from retrieval silently."""

    error_code: str = "CORPUS_FORMAT_ERROR"
    retryable: bool = False

    def __init__(self, path: Path, tool: str, entry: object) -> None:
        super().__init__(
            f"non-string corpus entry in {path}: tool {tool!r} has entry "
            f"{entry!r} ({type(entry).__name__}) -- likely an unquoted "
            "phrasing containing a colon; quote it as a YAML string"
        )
        self.path = path
        self.tool = tool
        self.entry = entry


_INDEX_LOCK = threading.Lock()
_INDEX: "_DiscoverIndex | None" = None


class _DiscoverIndex:
    """In-memory hybrid retrieval index.

    Every per-tool list is PARALLEL to ``tool_names``; ``dense_encode_fn`` must match the modality ``dense_matrix`` was built with,
    and ``bm25`` or ``dense_matrix`` may each be ``None``."""

    def __init__(
        self,
        tool_names: list[str],
        descriptions: list[str],
        synthetic_queries: list[list[str]],
        corpus_tokens: list[list[str]],
        bm25: Any,
        dense_matrix: Any,
        dense_encode_fn: Any,
        backend_name: str | None,
        vocabulary: frozenset[str] = frozenset(),
    ) -> None:
        self.tool_names = tool_names
        self.descriptions = descriptions
        self.synthetic_queries = synthetic_queries
        self.corpus_tokens = corpus_tokens
        self.bm25 = bm25
        self.dense_matrix = dense_matrix
        self.dense_encode_fn = dense_encode_fn
        self.backend_name = backend_name
        self.vocabulary = vocabulary


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Lowercasing tokenizer for BM25. Splits on any non-alphanumeric character but
    KEEPS underscores, so a tool name referenced verbatim survives as one token."""
    if not isinstance(text, str):
        return []
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


# Typo expansion, model-free (difflib): at QUERY time an out-of-vocabulary token gets up to two close vocabulary matches APPENDED, never replacing,
# so a misspelt word cannot miss the tool and the raw prompt is untouched. Installed on the built index's slots so every consumer inherits it.

#: Minimum token length eligible for fuzzy correction; a short token is too
#: ambiguous to correct toward anything in particular.
_TYPO_MIN_TOKEN_LEN = 4
#: Maximum vocabulary matches appended per out-of-vocab token.
_TYPO_MAX_MATCHES = 2
#: difflib.SequenceMatcher ratio cutoff (inclusive).
_TYPO_CUTOFF = 0.8


@functools.lru_cache(maxsize=4096)
def _close_vocab_matches(token: str, vocabulary: frozenset) -> tuple[str, ...]:
    """Fuzzy vocabulary corrections for ONE query token, most similar first, or ``()``
    when it is already in vocabulary, too short, or numeric. The vocabulary is SORTED first, so iteration order never matters."""
    # The vocabulary frozenset is part of the lru_cache key and frozensets hash by
    # content, so a rebuild with a changed corpus keys fresh entries on its own
    # while a rebuild with identical content reuses them.
    if len(token) < _TYPO_MIN_TOKEN_LEN:
        return ()
    if token.isdigit():
        return ()
    if token in vocabulary:
        return ()
    return tuple(
        difflib.get_close_matches(
            token, sorted(vocabulary), n=_TYPO_MAX_MATCHES, cutoff=_TYPO_CUTOFF
        )
    )


def _expand_query_tokens(
    tokens: list[str], vocabulary: frozenset
) -> list[str]:
    """``tokens`` with fuzzy corrections APPENDED, never replaced: the originals keep
    their order and multiplicity at the head, and each distinct correction is appended at most once."""
    if not vocabulary:
        return list(tokens)
    expanded = list(tokens)
    seen = set(expanded)
    for tok in tokens:
        for match in _close_vocab_matches(tok, vocabulary):
            if match not in seen:
                expanded.append(match)
                seen.add(match)
    return expanded


class _TypoTolerantBM25:
    """Proxy over ``BM25Okapi`` that typo-expands query tokens in ``get_scores``; the corpus is fed to the wrapped BM25 BEFORE the proxy exists."""

    def __init__(self, bm25: Any, vocabulary: frozenset) -> None:
        self._bm25 = bm25
        self._vocabulary = vocabulary

    def get_scores(self, query_tokens: list[str]) -> Any:
        return self._bm25.get_scores(
            _expand_query_tokens(list(query_tokens), self._vocabulary)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bm25, name)


def _wrap_hashed_encode_with_expansion(encode_fn: Any, vocabulary: frozenset) -> Any:
    """Query-side typo expansion for the HASHED fallback ONLY, which is as typo-blind as
    BM25; a real embedding backend is subword-tolerant and must NEVER be handed a synthetic token join."""

    def _encode_expanded(texts: list[str]) -> Any:
        return encode_fn(
            [" ".join(_expand_query_tokens(_tokenize(t), vocabulary)) for t in texts]
        )

    return _encode_expanded


def _short_description(docstring: str | None) -> str:
    """A short snippet for the result payload: the first paragraph, capped near
    240 chars, empty when there is no docstring."""
    if not docstring:
        return ""
    text = docstring.strip()
    parts = text.split("\n\n", 1)
    head = parts[0].strip().replace("\n", " ")
    head = re.sub(r"\s+", " ", head)
    if len(head) > 240:
        head = head[:237] + "..."
    return head


def _read_corpus_yaml(p: Path) -> dict[str, list[str]]:
    """One corpus YAML file as ``{tool: [queries]}``; a MISSING file yields ``{}``, a non-string entry raises ``CorpusFormatError``."""
    if not p.exists():
        return {}
    try:
        with p.open() as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 -- best-effort corpus read
        logger.warning("failed to parse corpus YAML at %s", p)
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for k, v in data.items():
        tool = str(k)
        queries: list[str] = []
        for q in v or []:
            if not isinstance(q, str):
                raise CorpusFormatError(p, tool, q)
            queries.append(q)
        parsed[tool] = queries
    return parsed


def _compose_corpus_from_tree() -> dict[str, list[str]]:
    """The flat ``{key: [queries]}`` corpus: every co-located ``corpus.yaml`` under
    ``tools/`` and ``workflows/`` - engine templates are ordinary pool members.
    A key is a tool name or a DATA CLASS; the residual file merges on top."""
    server_dir = Path(__file__).resolve().parents[3]
    composed: dict[str, list[str]] = {}
    for base in (server_dir / "tools", server_dir / "workflows"):
        for cpath in sorted(base.rglob("corpus.yaml")):
            composed.update(_read_corpus_yaml(cpath))
    residual = server_dir / "tools" / "tool_query_corpus.yaml"
    composed.update(_read_corpus_yaml(residual))
    return composed


def _load_corpus(path: Path | None = None) -> dict[str, list[str]]:
    """The example-query corpus keyed by tool name: the composed tree by default,
    or the single file an explicit ``path`` or the env override pins."""
    if path is not None:
        return _read_corpus_yaml(path)
    env_path = os.environ.get("TRID3NT_TOOL_CORPUS_YAML")
    if env_path:
        return _read_corpus_yaml(Path(env_path).expanduser().resolve())
    return _compose_corpus_from_tree()


#: Default sentence-transformers model id; env-overridable for an experiment
#: without a code change.
_DEFAULT_SENTENCE_TRANSFORMERS_MODEL = "all-MiniLM-L6-v2"


def _try_sentence_transformers_backend() -> tuple[Any, Any, str] | None:
    """``(encode_fn, np_module, backend_name)``, or ``None`` when the library is absent; the model LOAD is deferred to the first encode."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        import numpy as np  # noqa: F401
    except Exception:
        return None

    model_id = os.environ.get("TRID3NT_EMBEDDING_MODEL", _DEFAULT_SENTENCE_TRANSFORMERS_MODEL)
    model_holder: dict[str, Any] = {}

    def _encode(texts: list[str]) -> Any:
        import numpy as _np

        if "model" not in model_holder:
            logger.info("loading sentence-transformers model %r (first call)", model_id)
            model_holder["model"] = SentenceTransformer(model_id)
        emb = model_holder["model"].encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return _np.asarray(emb, dtype="float32")

    import numpy as _np

    return _encode, _np, "sentence_transformers"


def _try_hashed_backend() -> tuple[Any, Any, str] | None:
    """Final-fallback dense backend: hashed token-count vectors, LEXICAL ONLY (keeps the fusion path live, adds no semantics)."""
    try:
        import numpy as _np
    except Exception:
        return None

    DIM = 256

    def _encode(texts: list[str]) -> Any:
        out = _np.zeros((len(texts), DIM), dtype="float32")
        for i, text in enumerate(texts):
            for tok in _tokenize(text):
                h = int.from_bytes(hashlib.blake2s(tok.encode("utf-8"), digest_size=4).digest(), "big")
                out[i, h % DIM] += 1.0
        norms = _np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return out / norms

    return _encode, _np, "hashed"


def _select_dense_backend() -> tuple[Any, Any, str] | None:
    """Pick the best-available dense backend, in priority order."""
    for builder in (
        _try_sentence_transformers_backend,
        _try_hashed_backend,
    ):
        try:
            picked = builder()
        except Exception as exc:  # noqa: BLE001 -- backend probe is best-effort
            logger.debug("dense-backend probe %s raised: %s", builder.__name__, exc)
            picked = None
        if picked is not None:
            logger.info("search_tools dense backend = %s", picked[2])
            return picked
    return None


def _build_index(
    corpus_path: Path | None = None,
    registry_snapshot: dict[str, Any] | None = None,
) -> _DiscoverIndex:
    """The BM25 + dense index over the registry and the composed corpus."""
    from trid3nt_contracts.coverage import DATA_CLASSES
    from trid3nt_server.tools.search.find_sources.find_sources import FIND_SOURCES
    from trid3nt_server.tools.search.match import covered_sources

    snapshot = registry_snapshot if registry_snapshot is not None else dict(TOOL_REGISTRY)
    corpus = _load_corpus(corpus_path)
    # A fetcher with a ROW is found, not ranked: its extent, window and cell decide which source measures a class at a place. It leaves the index,
    # class phrasings route to find_sources, and a fetcher with no row stays here, routed by description.
    covered = covered_sources()

    tool_names: list[str] = []
    descriptions: list[str] = []
    synthetic_queries: list[list[str]] = []
    documents: list[str] = []
    corpus_tokens: list[list[str]] = []

    for name in sorted(snapshot.keys()):
        entry = snapshot[name]
        # Templates (tier=template) index here; only tier=internal is withheld (registry-resolvable, not searchable).
        if getattr(entry.metadata, "tier", "general") in ("internal",):
            continue
        if name in covered:
            continue
        # ROUTING FRONT BLOCK ONLY: the text an author wrote to win retrieval.
        # A generated body - the keyword sheet, a need row's sentence - describes a
        # slot the MATCH fills, so its words are not the tool's routing words.
        doc = (getattr(entry.fn, "routing_doc", None)
               or getattr(entry.fn, "__doc__", "") or "")
        snippet = _short_description(doc)
        # The FULL docstring feeds BM25 and dense - a much richer signal than the
        # first paragraph - while the short snippet is what the payload returns.
        full_doc = " ".join((doc or "").split())
        qs = corpus.get(name, [])
        # The name is DOUBLED to bias BM25 toward an exact-name hit, which is
        # cheaper than per-field weighting rank_bm25 does not offer.
        body_parts = [name, name, full_doc] + qs
        body = "\n".join(p for p in body_parts if p)

        tool_names.append(name)
        descriptions.append(snippet)
        synthetic_queries.append(list(qs))
        documents.append(body)
        corpus_tokens.append(_tokenize(body))

    # A DATA CLASS indexes as its OWN document routing to the match: one class's
    # phrasings ranked against their own length, never the whole vocabulary folded
    # into a single document BM25's length normalization then dilutes.
    if FIND_SOURCES in tool_names:
        at = tool_names.index(FIND_SOURCES)
        for cls in sorted(key for key in corpus if key in DATA_CLASSES):
            queries = list(corpus[cls])
            body = "\n".join([cls.replace("_", " "), *queries])
            tool_names.append(FIND_SOURCES)
            descriptions.append(descriptions[at])
            synthetic_queries.append(queries)
            documents.append(body)
            corpus_tokens.append(_tokenize(body))

    # The typo-expansion vocabulary REUSES the tokens the index already produced
    # rather than re-deriving them. Frozen per build, and the correction cache keys
    # on this object, so a rebuild with new content invalidates cleanly.
    vocabulary: frozenset[str] = frozenset(
        tok for toks in corpus_tokens for tok in toks
    )

    bm25 = None
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

        if corpus_tokens:
            # The proxy expands QUERY tokens only: the corpus is already tokenized
            # inside the wrapped BM25Okapi.
            bm25 = _TypoTolerantBM25(BM25Okapi(corpus_tokens), vocabulary)
    except Exception as exc:  # noqa: BLE001 -- non-fatal
        logger.warning("rank_bm25 unavailable; BM25 path disabled (%s)", exc)
        bm25 = None

    dense_matrix = None
    dense_encode_fn = None
    backend_name = None
    backend = _select_dense_backend()
    if backend is not None and documents:
        encode_fn, _np, backend_name = backend
        try:
            dense_matrix = encode_fn(documents)
            dense_encode_fn = encode_fn
            if backend_name == "hashed":
                # Typo-expand queries for the lexical hashed fallback ONLY: the
                # matrix above was built from the RAW documents, and a real
                # embedding backend keeps the raw query text unchanged.
                dense_encode_fn = _wrap_hashed_encode_with_expansion(
                    encode_fn, vocabulary
                )
        except Exception as exc:  # noqa: BLE001 -- non-fatal
            logger.warning(
                "dense backend %r failed at index-build time (%s); disabling dense path",
                backend_name,
                exc,
            )
            dense_matrix = None
            dense_encode_fn = None
            backend_name = None

    logger.info(
        "search_tools index built: %d tools, bm25=%s, dense=%s",
        len(tool_names),
        bm25 is not None,
        backend_name,
    )

    return _DiscoverIndex(
        tool_names=tool_names,
        descriptions=descriptions,
        synthetic_queries=synthetic_queries,
        corpus_tokens=corpus_tokens,
        bm25=bm25,
        dense_matrix=dense_matrix,
        dense_encode_fn=dense_encode_fn,
        backend_name=backend_name,
        vocabulary=vocabulary,
    )


def _get_index() -> _DiscoverIndex:
    """Return the lazy-built index, building once under a lock."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = _build_index()
    return _INDEX


def _reset_index_for_tests() -> None:
    """Clear the cached index. ONLY for tests."""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
    # Content-keyed, so a stale entry can never be WRONG after a rebuild; clearing
    # only keeps memory and isolation tidy.
    _close_vocab_matches.cache_clear()


def _reciprocal_rank_fusion(
    rankings: list[list[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of N rank lists of doc indices, returning ``[(doc_index,
    fused_score), ...]`` descending. Rank-aware, not score-aware: an absent doc contributes 0."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    out = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return out


# Lexical-champion reinforcement: plain RRF buries a tool's best exact-lexical match beneath tools that rank mid on both channels,
# so the BM25 champion earns ONE extra reciprocal-rank term on the same 1/(k+rank) scale; bounded and deterministic.


def _lexical_reinforcement(
    fused: list[tuple[int, float]],
    bm25_ranking: list[int],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Re-rank ``fused`` after the BM25 champion's one ``1.0 / (k + 1)`` bonus.
    Returns a NEW list; the input comes back unchanged with no BM25 ranking."""
    if not bm25_ranking:
        return fused
    champion = bm25_ranking[0]
    bonus = 1.0 / (k + 1)
    rescored = [(doc, score + (bonus if doc == champion else 0.0)) for doc, score in fused]
    rescored.sort(key=lambda pair: pair[1], reverse=True)
    return rescored


def _match_synthetic_queries(
    query: str, synthetic_queries: list[str], limit: int = 3
) -> list[str]:
    """Up to ``limit`` corpus phrasings sharing the most content tokens with ``query``.
    Purely lexical, and returned so the caller can see WHY a tool surfaced."""
    if not synthetic_queries:
        return []
    q_tokens = set(_tokenize(query)) - _STOPWORDS
    if not q_tokens:
        return synthetic_queries[:limit]
    scored: list[tuple[int, str]] = []
    for sq in synthetic_queries:
        sq_tokens = set(_tokenize(sq)) - _STOPWORDS
        overlap = len(q_tokens & sq_tokens)
        if overlap > 0:
            scored.append((overlap, sq))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [sq for _, sq in scored[:limit]]


#: Generic operator/shape tokens in many utility tool names that the LLM uses descriptively; skipped by the name-substring ranker
#: so "flood zone polygons" does not over-boost a polygon-named utility over the data-intent target.
_NAME_RANKER_GENERICS: set[str] = {
    "polygon",
    "polygons",
    # Bare domain nouns are routed by the content channels; name-channel RRF terms would make name-bearing tools unbeatable for analytical asks.
    "building",
    "buildings",
    "flood",
    "depth",
    "layer",
    "layers",
    "statistics",
    "population",
    "zone",
    "zones",
    "raster",
    "vector",
    "clip",
    "compute",
    "run",
    "fetch",
    "extract",
    "publish",
    "aggregate",
    "lookup",
    "geocode",
    "discover",
    "wait",
    "process",
    "model",  # too common -- present in many *_model_* names
}


_STOPWORDS: set[str] = {
    "the",
    "a",
    "an",
    "for",
    "in",
    "on",
    "to",
    "of",
    "and",
    "or",
    "is",
    "are",
    "i",
    "me",
    "my",
    "we",
    "show",
    "give",
    "fetch",
    "get",
    "pull",
    "want",
    "need",
    "data",
    # Demonstratives carry no routing signal, and the name channel stems a trailing "s" ("this" -> "thi") and would match any tool name containing it.
    "this",
    "that",
    "these",
    "those",
}


_SEARCH_TOOLS_METADATA = AtomicToolMetadata(
    name="search_tools",
    ttl_class="live-no-cache",
    source_class=None,
)


@register_tool(
    _SEARCH_TOOLS_METADATA,
    supports_global_query=False,
)
async def search_tools(
    query: str,
    top_k: int = 5,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Route a free-text need to the atomic tools that answer it.

    ROUTING: a free-text need with no obvious tool in mind - "show me flood zones",
    "hurricane wind probabilities" - where a narrow shortlist beats scanning the
    whole surface. NOT for enumerating every tool (the registry is the inventory)
    and NOT to dispatch a run.

    `query` is required and non-empty; `top_k` is clamped to [1, 25]. A degenerate
    empty query returns an empty result rather than raising.

    Returns {"results": [{tool_name, score, description_snippet, matched_queries}]}.
    `score` is rank-derived, so only its ORDERING is meaningful - higher is more
    relevant, but the number is not a probability. `matched_queries` are the corpus
    phrasings that overlapped the ask, there to confirm the routing made sense.
    """
    if not isinstance(query, str):
        return {"results": []}
    query_clean = query.strip()
    if not query_clean:
        return {"results": []}

    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 5
    k = max(1, min(25, k))

    from trid3nt_server.tools.search.tool_retrieval import ranked_docs

    index = _get_index()
    if not index.tool_names:
        return {"results": []}
    fused = ranked_docs(query_clean, index)
    if not fused:
        return {"results": []}

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, score in fused:
        if len(results) >= k:
            break
        tool_name = index.tool_names[idx]
        if tool_name in seen:
            continue
        seen.add(tool_name)
        snippet = index.descriptions[idx]
        matched = _match_synthetic_queries(query_clean, index.synthetic_queries[idx])
        results.append(
            {
                "tool_name": tool_name,
                "score": round(float(score), 6),
                "description_snippet": snippet,
                "matched_queries": matched,
            }
        )

    logger.info(
        "search_tools query=%r top_k=%d backend=%s results=%s",
        query_clean[:80],
        k,
        index.backend_name,
        [r["tool_name"] for r in results],
    )
    return {"results": results}
