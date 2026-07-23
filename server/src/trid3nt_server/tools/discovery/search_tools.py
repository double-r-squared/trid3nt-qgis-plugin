"""``search_tools`` atomic tool — hybrid BM25 + dense retrieval (Wave 4.10 job-B7).

§F.1.2 Mode 1 routing assist: given a free-text user query, return the top-k
matching atomic tools ranked by hybrid lexical (BM25) + semantic (dense-
embedding) similarity over a corpus built from each tool's audited docstring
plus the Wave 4.10 ``tool_query_corpus.yaml`` synthetic example queries.

Goal: when the LLM sees a free-text need like "show me flood zones" or
"national parks polygons", a single ``search_tools`` call returns the
top-k tool names + short snippets, narrowing the function-calling search
space without forcing the LLM to scan all 70+ atomic tools.

Implementation choices (Wave 4.10 stage 2):
- **BM25** via `rank_bm25.BM25Okapi`. Whitespace + lowercase tokenization;
  no stemming (the corpus and queries are English natural language + a few
  domain terms that don't stem well — "USGS", "WDPA", "NWS", etc).
- **Dense retrieval** is opportunistic:
    1. If `sentence-transformers` is importable, encode with the
       `all-MiniLM-L6-v2` checkpoint (384-dim).
    2. Else if Vertex AI creds are present (`GOOGLE_GENAI_USE_VERTEXAI=1` or
       a default `vertexai` client builds), call `text-embedding-005` for
       both the index and the query.
    3. Else fall back to a deterministic hashed token-count vector (cosine-
       comparable but lexical only — the BM25 signal carries the load in
       this degraded mode).
- **Fusion**: Reciprocal Rank Fusion (RRF, k=60) interleaves BM25 ranking
  and dense-similarity ranking. RRF is rank-aware (not score-aware) so the
  two retrieval modalities don't need score normalization.

Index is built lazily at first call and cached at module level. Subsequent
calls within the same Python process reuse the cached BM25 instance and
dense-vector matrix. Reset for tests via ``_reset_index_for_tests()``.

FR-TA-2 / FR-AS-3: registered with ``ttl_class="static-30d"``,
``source_class="search_tools"``, ``cacheable=False`` — the routing
output depends on the live registry contents and synthetic-corpus file,
both of which are import-time-frozen, but caching the result through the
GCS read_through shim would be wasteful for a sub-millisecond CPU lookup.

Hot-set hook: tagged ``supports_global_query=False`` (the query is the
search input; bbox-less is the only mode). The Wave 4.10 B5 per-turn
filter will add this tool to the agent's hot set so it appears even when
the rest of the catalog is hidden.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import logging
import math
import os
import re
import threading
import time
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
    "SearchToolsError",
    "get_dynamic_hot_set",
    "_get_cooccurrence_index",
    "_reset_cooccurrence_cache_for_tests",
    "_reset_hot_set_cache_for_tests",
    "CooccurrenceIndex",
]

logger = logging.getLogger("trid3nt_server.tools.discovery.search_tools")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class SearchToolsError(RuntimeError):
    """Base class for search_tools failures."""

    error_code: str = "SEARCH_TOOLS_ERROR"
    retryable: bool = False


# ---------------------------------------------------------------------------
# Index state (module-level, lazy-built on first call).
# ---------------------------------------------------------------------------


_INDEX_LOCK = threading.Lock()
_INDEX: "_DiscoverIndex | None" = None


# ---------------------------------------------------------------------------
# Co-occurrence index state (Wave 4.11 M5).
#
# The co-occurrence index is rebuilt from the ``tool_call_telemetry`` Mongo
# collection on a ~5-minute cadence so that the RRF boost reflects recent
# user behavior without round-tripping to Mongo on every search_tools
# call.  When Mongo is unavailable (Persistence singleton unbound), the
# index is left ``None`` and the 4th channel silently drops out — the 3-
# channel ranking continues to work.
# ---------------------------------------------------------------------------


_COOCCURRENCE_LOCK = threading.Lock()
_COOCCURRENCE_INDEX: "CooccurrenceIndex | None" = None
_COOCCURRENCE_REFRESH_SECONDS: float = 5 * 60.0  # 5-minute refresh window

# Hot-set cache (M6 substrate landing here for shared Mongo access path).
_HOT_SET_LOCK = threading.Lock()
_HOT_SET_CACHE: "dict[str | None, tuple[float, frozenset[str]]]" = {}
_HOT_SET_REFRESH_SECONDS: float = 5 * 60.0


class CooccurrenceIndex:
    """Per-tool dispatch + co-occurrence stats derived from telemetry.

    Fields:
    - ``call_counts``: ``{tool_name: int}`` — total dispatch count over the
      sampled telemetry window.
    - ``cooccurrence``: ``{tool_name: {co_tool_name: int}}`` — count of how
      many sessions dispatched BOTH ``tool_name`` and ``co_tool_name``.
      Symmetric (``cooccurrence[A][B] == cooccurrence[B][A]``).
    - ``built_at``: monotonic timestamp at index build (used for the
      5-minute refresh window).
    - ``session_count``: number of distinct sessions sampled.
    """

    __slots__ = ("call_counts", "cooccurrence", "built_at", "session_count")

    def __init__(
        self,
        call_counts: dict[str, int],
        cooccurrence: dict[str, dict[str, int]],
        built_at: float,
        session_count: int,
    ) -> None:
        self.call_counts = call_counts
        self.cooccurrence = cooccurrence
        self.built_at = built_at
        self.session_count = session_count


class _DiscoverIndex:
    """In-memory hybrid retrieval index.

    Fields:
    - ``tool_names``: list of registered tool names (the routing output).
    - ``descriptions``: list of short description snippets (returned to the LLM).
    - ``synthetic_queries``: list of per-tool synthetic-query lists.
    - ``corpus_tokens``: list of token lists (parallel to ``tool_names``).
    - ``vocabulary``: frozenset of every token in ``corpus_tokens`` (tool
      names + docstrings + corpus queries). Used by the typo query-expansion
      helpers; stable for the lifetime of one index build.
    - ``bm25``: a ``_TypoTolerantBM25`` proxy over a rank_bm25.BM25Okapi
      instance, or None when rank_bm25 isn't importable (tests fall back to
      dense-only or zero-score paths).
    - ``dense_matrix``: optional numpy ndarray (N × d) of L2-normalized
      per-tool dense vectors. ``None`` when no dense backend is available.
    - ``dense_encode_fn``: callable ``(list[str]) -> np.ndarray`` used to
      embed the query at search time. Must match the modality used to build
      ``dense_matrix``.
    - ``backend_name``: identifier for the dense backend
      (``"sentence_transformers"`` / ``"vertex"`` / ``"hashed"`` / ``None``).
    """

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


# ---------------------------------------------------------------------------
# Tokenizer + corpus assembly.
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Whitespace + lowercase tokenizer used for BM25.

    Splits on any non-alphanumeric character (keeps underscores so
    ``fetch_dem`` survives as a single token, which is useful when the LLM
    references a tool name verbatim). Lowercases every token. No stemming.
    """
    if not isinstance(text, str):
        return []
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# Typo query expansion (model-free, stdlib difflib, deterministic).
#
# Motivating live failure: "can you show me a gradinet relief ..." (typo for
# "gradient") missed compute_colored_relief because the BM25 channel is
# exact-token and the hashed dense fallback hashes the same exact tokens.
# Fix: at QUERY time only, out-of-vocabulary tokens get up to 2 close
# vocabulary matches APPENDED to the token list (expansion, never
# replacement). Lives entirely inside retrieval scoring -- the LLM always
# sees the raw prompt unchanged.
#
# Seam: the wrappers below are installed on the built index's ``bm25`` and
# (hashed-backend) ``dense_encode_fn`` slots, so EVERY consumer of the cached
# index (search_tools's inline ranking AND tool_retrieval's
# retrieve_visible_tools) inherits the expansion without code changes.
# ---------------------------------------------------------------------------

#: Minimum token length eligible for fuzzy correction (short tokens are too
#: ambiguous -- "teh" could be anything).
_TYPO_MIN_TOKEN_LEN = 4
#: Maximum vocabulary matches appended per out-of-vocab token.
_TYPO_MAX_MATCHES = 2
#: difflib.SequenceMatcher ratio cutoff (inclusive).
_TYPO_CUTOFF = 0.8


@functools.lru_cache(maxsize=4096)
def _close_vocab_matches(token: str, vocabulary: frozenset) -> tuple[str, ...]:
    """Fuzzy vocabulary corrections for ONE query token (cached).

    Returns ``()`` (no expansion) when the token is: already in the
    vocabulary, shorter than ``_TYPO_MIN_TOKEN_LEN`` chars, or purely
    numeric. Otherwise returns up to ``_TYPO_MAX_MATCHES`` close vocabulary
    matches via ``difflib.get_close_matches`` (cutoff ``_TYPO_CUTOFF``),
    most similar first. The vocabulary is sorted before matching so the
    result is deterministic regardless of set iteration order.

    Cache keying: the vocabulary frozenset itself is part of the lru_cache
    key (frozensets hash by content, and CPython caches the hash), so an
    index rebuild with a changed corpus keys fresh entries automatically
    while a rebuild with identical content correctly reuses them.
    """
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
    """Return ``tokens`` with fuzzy corrections APPENDED (never replaced).

    The original tokens keep their order and multiplicity at the head of the
    result; each distinct correction is appended at most once and only when
    it is not already present. With an empty vocabulary the input is
    returned unchanged (no expansion possible).
    """
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
    """Proxy over ``BM25Okapi`` that typo-expands query tokens in get_scores.

    Installed on ``_DiscoverIndex.bm25`` at build time. Corpus documents are
    tokenized and fed to the wrapped BM25 BEFORE this proxy exists, so only
    QUERIES are ever expanded. All other attribute access is delegated.
    """

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
    """Query-side typo expansion for the HASHED dense fallback only.

    The hashed backend is purely lexical (token hash counts), so it is as
    typo-blind as BM25; expanding the token list before hashing applies the
    same fix. Installed as ``dense_encode_fn`` AFTER the index matrix is
    built from the raw documents. Real embedding backends
    (sentence-transformers / Vertex) keep the raw text unchanged -- they are
    subword-tolerant and must not be fed a synthetic token join.
    """

    def _encode_expanded(texts: list[str]) -> Any:
        return encode_fn(
            [" ".join(_expand_query_tokens(_tokenize(t), vocabulary)) for t in texts]
        )

    return _encode_expanded


def _short_description(docstring: str | None) -> str:
    """Pull a short snippet from a tool's docstring for the result payload.

    Uses the first non-empty paragraph (up to ~240 chars). Falls back to
    the tool name when no docstring is present.
    """
    if not docstring:
        return ""
    text = docstring.strip()
    # First paragraph = first blank-line-separated block.
    parts = text.split("\n\n", 1)
    head = parts[0].strip().replace("\n", " ")
    head = re.sub(r"\s+", " ", head)
    if len(head) > 240:
        head = head[:237] + "..."
    return head


def _default_corpus_path() -> Path:
    """Resolve ``tool_query_corpus.yaml`` under the package's ``data/`` dir."""
    env_path = os.environ.get("TRID3NT_TOOL_CORPUS_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # tools/discovery/search_tools.py -> trid3nt_server/ is parents[2]
    here = Path(__file__).resolve()
    return here.parents[2] / "data" / "tool_query_corpus.yaml"


def _load_corpus(path: Path | None = None) -> dict[str, list[str]]:
    """Load the synthetic example-query corpus YAML, keyed by tool name.

    Returns an empty dict when the file is missing — search_tools still
    works in docstring-only mode in that case (e.g. when running outside the
    repo with no data file).
    """
    p = path if path is not None else _default_corpus_path()
    if not p.exists():
        logger.warning(
            "tool_query_corpus.yaml not found at %s; falling back to docstring-only index",
            p,
        )
        return {}
    with p.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): [str(q) for q in (v or []) if isinstance(q, str)]
        for k, v in data.items()
    }


# ---------------------------------------------------------------------------
# Dense-embedding backends (graceful degradation).
# ---------------------------------------------------------------------------


def _try_sentence_transformers_backend() -> tuple[Any, Any, str] | None:
    """Try to load sentence-transformers all-MiniLM-L6-v2 backend.

    Returns ``(encode_fn, np_module, backend_name)`` or ``None`` if the
    library is not installed. Loading the model is deferred to the first
    ``encode_fn`` call to keep import-time cost low.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        import numpy as np  # noqa: F401
    except Exception:
        return None

    model_holder: dict[str, Any] = {}

    def _encode(texts: list[str]) -> Any:
        import numpy as _np

        if "model" not in model_holder:
            logger.info("loading sentence-transformers all-MiniLM-L6-v2 (first call)")
            model_holder["model"] = SentenceTransformer("all-MiniLM-L6-v2")
        emb = model_holder["model"].encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return _np.asarray(emb, dtype="float32")

    import numpy as _np

    return _encode, _np, "sentence_transformers"


def _try_vertex_backend() -> tuple[Any, Any, str] | None:
    """Try to load Vertex AI ``text-embedding-005`` backend.

    Returns ``(encode_fn, np_module, backend_name)`` or ``None`` if Vertex
    creds aren't available. Avoid live calls at import time — defer to first
    ``encode_fn``.
    """
    try:
        import numpy as _np  # noqa: F401
    except Exception:
        return None
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if not use_vertex:
        return None
    try:
        from vertexai.language_models import TextEmbeddingModel  # type: ignore[import-not-found]
    except Exception:
        return None

    model_holder: dict[str, Any] = {}

    def _encode(texts: list[str]) -> Any:
        import numpy as _np

        if "model" not in model_holder:
            logger.info("loading Vertex AI text-embedding-005 (first call)")
            model_holder["model"] = TextEmbeddingModel.from_pretrained("text-embedding-005")
        embeddings = model_holder["model"].get_embeddings(texts)
        arr = _np.asarray([e.values for e in embeddings], dtype="float32")
        # L2-normalize for cosine via dot product.
        norms = _np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return arr / norms

    import numpy as _np

    return _encode, _np, "vertex"


def _try_hashed_backend() -> tuple[Any, Any, str] | None:
    """Final-fallback "dense" backend: hashed token-count vectors (256-dim).

    Lexical-only — produces near-duplicate signal to BM25 — but lets the
    hybrid-fusion path stay live even without sentence-transformers / Vertex.
    Tests that rely on dense-vector shape (not semantic correctness) pass.
    """
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
        _try_vertex_backend,
        _try_hashed_backend,
    ):
        try:
            picked = builder()
        except Exception as exc:  # noqa: BLE001 — backend probe is best-effort
            logger.debug("dense-backend probe %s raised: %s", builder.__name__, exc)
            picked = None
        if picked is not None:
            logger.info("search_tools dense backend = %s", picked[2])
            return picked
    return None


# ---------------------------------------------------------------------------
# Index build.
# ---------------------------------------------------------------------------


def _build_index(
    corpus_path: Path | None = None,
    registry_snapshot: dict[str, Any] | None = None,
) -> _DiscoverIndex:
    """Construct the BM25 + dense index from TOOL_REGISTRY + corpus YAML.

    Per-tool concatenated document text fed into BOTH BM25 and the dense
    encoder:

        ``"{name} {name} {description} {q1} {q2} ... {qN}"``

    The tool name is repeated to bias BM25 toward exact-name matches without
    needing per-field weighting (rank_bm25 treats the corpus as a flat bag).
    """
    snapshot = registry_snapshot if registry_snapshot is not None else dict(TOOL_REGISTRY)
    corpus = _load_corpus(corpus_path)

    tool_names: list[str] = []
    descriptions: list[str] = []
    synthetic_queries: list[list[str]] = []
    documents: list[str] = []
    corpus_tokens: list[list[str]] = []

    for name in sorted(snapshot.keys()):
        entry = snapshot[name]
        doc = getattr(entry.fn, "__doc__", "") or ""
        snippet = _short_description(doc)
        # Full docstring fed to BM25 + dense — much richer signal than just
        # the first paragraph. We keep ``snippet`` short for the returned
        # ``description_snippet`` payload (LLM-visible UX); the longer text
        # only lives in the indexed corpus.
        full_doc = " ".join((doc or "").split())
        qs = corpus.get(name, [])
        # Build the document. Name is doubled to bias BM25 toward exact-name
        # hits ("call fetch_nws_alerts_conus" → exact match).
        body_parts = [name, name, full_doc] + qs
        body = "\n".join(p for p in body_parts if p)

        tool_names.append(name)
        descriptions.append(snippet)
        synthetic_queries.append(list(qs))
        documents.append(body)
        corpus_tokens.append(_tokenize(body))

    # Vocabulary for typo query expansion: every token the index already
    # produced from tool names, docstrings, and corpus queries (reused, not
    # re-derived). Frozen per build; the lru_cache on _close_vocab_matches
    # keys on this object so a rebuild with new content invalidates cleanly.
    vocabulary: frozenset[str] = frozenset(
        tok for toks in corpus_tokens for tok in toks
    )

    # BM25 (optional — degrades gracefully when rank_bm25 is absent).
    bm25 = None
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

        if corpus_tokens:
            # Typo-expansion proxy: expands QUERY tokens only (the corpus
            # is already tokenized and inside the wrapped BM25Okapi).
            bm25 = _TypoTolerantBM25(BM25Okapi(corpus_tokens), vocabulary)
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.warning("rank_bm25 unavailable; BM25 path disabled (%s)", exc)
        bm25 = None

    # Dense (optional — graceful degradation).
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
                # Typo-expand queries for the lexical hashed fallback ONLY.
                # The matrix above was built from the RAW documents; real
                # embedding backends keep the raw query text unchanged.
                dense_encode_fn = _wrap_hashed_encode_with_expansion(
                    encode_fn, vocabulary
                )
        except Exception as exc:  # noqa: BLE001 — non-fatal
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
    # Content-keyed, so stale entries can never be WRONG after a rebuild;
    # clearing just keeps test memory/isolation tidy.
    _close_vocab_matches.cache_clear()


# ---------------------------------------------------------------------------
# Co-occurrence + dynamic hot-set (Wave 4.11 M5).
# ---------------------------------------------------------------------------


# Sampling caps — last 30 sessions OR last 1000 calls, whichever is smaller.
# Both knobs guard against runaway growth as the telemetry collection ages.
_COOCC_SESSION_CAP: int = 30
_COOCC_CALL_CAP: int = 1000

# Default top-K for the dynamic hot set.  M6 will adjust as needed.
_HOT_SET_TOP_K: int = 8


def _get_persistence_safe() -> Any:
    """Return the bound Persistence singleton, or ``None`` on any error.

    Imports ``server.get_persistence`` lazily to avoid a circular import at
    module load time.  Any failure (singleton unbound, server not yet
    bootstrapped, ImportError) falls through to ``None`` so callers degrade
    to the existing static/3-channel paths.
    """
    try:
        from trid3nt_server.server import get_persistence as _server_get_persistence  # type: ignore[import-not-found]

        return _server_get_persistence()
    except Exception:  # noqa: BLE001 — best-effort lookup
        return None


async def _fetch_recent_telemetry_docs(
    persistence: Any,
    *,
    call_cap: int = _COOCC_CALL_CAP,
) -> list[dict[str, Any]]:
    """Read the most recent ``call_cap`` telemetry rows via MCP.

    Returns an empty list on any error — the caller treats that the same as
    "no telemetry yet" and skips the co-occurrence channel.

    The ULID ``_id`` field is time-sortable, so a descending sort by ``_id``
    gives us the most recent rows even without a secondary ``called_at_utc``
    index.  We pass ``limit`` when the MCP server supports it; the
    file-backed dev shim ignores unknown fields, which is fine — the cap is
    enforced in Python below either way.
    """
    try:
        from trid3nt_contracts.mongo_collections import TELEMETRY_COLLECTION
        from trid3nt_server.persistence import DEFAULT_DATABASE, _unwrap_mcp_result
    except Exception:  # noqa: BLE001
        return []

    try:
        raw = await persistence._mcp.call_tool(
            "find",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                "filter": {},
                "sort": {"_id": -1},
                "limit": call_cap,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("co-occurrence: telemetry find failed (%s)", exc)
        return []

    docs = _unwrap_mcp_result(raw) or []
    if isinstance(docs, dict):
        docs = [docs]
    if not isinstance(docs, list):
        return []

    # Defensive cap — the MCP server's ``limit`` semantics vary; enforce here.
    return [d for d in docs if isinstance(d, dict)][:call_cap]


def _build_cooccurrence_from_docs(
    docs: list[dict[str, Any]],
    *,
    session_cap: int = _COOCC_SESSION_CAP,
) -> CooccurrenceIndex:
    """Build the per-tool dispatch + pairwise co-occurrence map.

    Algorithm:
    1. Group recent telemetry rows by ``session_id``.
    2. Take the most recent ``session_cap`` sessions (the docs list is
       already DESC-by-ULID).
    3. Within each session, count tool dispatches; the per-tool count
       contributes to ``call_counts``.
    4. For every UNORDERED pair of distinct tools dispatched in the same
       session, increment ``cooccurrence[A][B]`` and ``cooccurrence[B][A]``
       by 1 (symmetric).  Pair count is per-session, not per-call, so
       calling ``fetch_dem`` three times in one session still counts as a
       single co-occurrence with each other tool in that session.
    """
    seen_sessions: list[str] = []
    seen_set: set[str] = set()
    by_session: dict[str, list[str]] = {}

    for d in docs:
        sid = d.get("session_id")
        tool = d.get("tool_name")
        if not isinstance(sid, str) or not isinstance(tool, str):
            continue
        if sid not in seen_set:
            if len(seen_sessions) >= session_cap:
                continue
            seen_set.add(sid)
            seen_sessions.append(sid)
            by_session[sid] = []
        by_session[sid].append(tool)

    call_counts: dict[str, int] = {}
    cooccurrence: dict[str, dict[str, int]] = {}
    for sid in seen_sessions:
        tools_in_session = by_session.get(sid, [])
        for t in tools_in_session:
            call_counts[t] = call_counts.get(t, 0) + 1
        # Distinct tools for co-occurrence — pair count is per-session.
        unique = sorted(set(tools_in_session))
        for i, a in enumerate(unique):
            row_a = cooccurrence.setdefault(a, {})
            for b in unique[i + 1 :]:
                row_b = cooccurrence.setdefault(b, {})
                row_a[b] = row_a.get(b, 0) + 1
                row_b[a] = row_b.get(a, 0) + 1

    return CooccurrenceIndex(
        call_counts=call_counts,
        cooccurrence=cooccurrence,
        built_at=time.monotonic(),
        session_count=len(seen_sessions),
    )


async def _refresh_cooccurrence_index() -> CooccurrenceIndex | None:
    """Fetch telemetry from Mongo and rebuild the co-occurrence index.

    Returns the new index on success, or ``None`` when Mongo is unavailable.
    Callers should treat ``None`` as "stay on the 3-channel path".
    """
    persistence = _get_persistence_safe()
    if persistence is None:
        return None
    mcp = getattr(persistence, "_mcp", None)
    if mcp is None:
        return None
    docs = await _fetch_recent_telemetry_docs(persistence)
    return _build_cooccurrence_from_docs(docs)


async def _get_cooccurrence_index() -> CooccurrenceIndex | None:
    """Return the cached co-occurrence index, refreshing past the 5-min window.

    Thread-safety: the cache pointer swap is guarded by a lock, but the
    rebuild itself runs without holding the lock so a slow Mongo read doesn't
    block other callers (they get the stale-but-usable index until the
    refresh task swaps the pointer).
    """
    global _COOCCURRENCE_INDEX
    now = time.monotonic()
    with _COOCCURRENCE_LOCK:
        cached = _COOCCURRENCE_INDEX
    if cached is not None and (now - cached.built_at) < _COOCCURRENCE_REFRESH_SECONDS:
        return cached
    new_index = await _refresh_cooccurrence_index()
    if new_index is None:
        # Mongo unavailable — keep the stale entry (if any) rather than
        # nuking the cache.  The 3-channel ranking continues to work either
        # way; reusing a stale index just preserves any prior boost signal.
        return cached
    with _COOCCURRENCE_LOCK:
        _COOCCURRENCE_INDEX = new_index
    return new_index


def _reset_cooccurrence_cache_for_tests() -> None:
    """Clear the co-occurrence cache.  ONLY for tests."""
    global _COOCCURRENCE_INDEX
    with _COOCCURRENCE_LOCK:
        _COOCCURRENCE_INDEX = None


def _reset_hot_set_cache_for_tests() -> None:
    """Clear the dynamic-hot-set cache.  ONLY for tests."""
    with _HOT_SET_LOCK:
        _HOT_SET_CACHE.clear()


def _name_matches_query(name: str, q_content_tokens: list[str]) -> bool:
    """True iff the query content tokens reference this tool name.

    Used by the co-occurrence channel to decide which "user-named" tools to
    project co-occurrence boost from.  Mirrors the existing name-substring
    ranker behavior (substring + crude suffix stemming).
    """
    if not q_content_tokens:
        return False
    name_low = name.lower()
    for t in q_content_tokens:
        if t in name_low:
            return True
        stem = t
        for suf in ("ing", "ed", "s"):
            if stem.endswith(suf) and len(stem) > len(suf) + 2:
                stem = stem[: -len(suf)]
                break
        if stem != t and stem in name_low:
            return True
    return False


def _build_cooccurrence_ranking(
    tool_names: list[str],
    q_content_tokens: list[str],
    cooc_index: CooccurrenceIndex,
) -> list[int]:
    """Rank tools by co-occurrence + call-frequency signal.

    Two contributions, summed per candidate:
    1. Tools the user's query directly names get a boost proportional to
       ``call_counts[name]`` (Σ over query-named tools — but here we score
       the candidate ITSELF when its name matches the query).
    2. Tools that co-occur with query-named tools get
       ``Σ cooccurrence[query_named_tool][candidate]``.

    Returns a list of indices descending by score; ties broken by original
    name order so the ranking is deterministic.  Candidates with score == 0
    are omitted (no signal → no contribution to RRF).
    """
    if not cooc_index.call_counts and not cooc_index.cooccurrence:
        return []

    # Set of tools whose names the user explicitly referenced in the query.
    query_named: list[str] = [
        n for n in tool_names if _name_matches_query(n, q_content_tokens)
    ]

    scores: list[tuple[int, float, int]] = []  # (score, index, tiebreak-orig-index)
    for i, name in enumerate(tool_names):
        score = 0.0
        # 1. Candidate itself referenced by the query → boost by its own
        #    historical dispatch frequency.
        if name in query_named:
            score += float(cooc_index.call_counts.get(name, 0))
        # 2. Candidate co-occurs with the query-named tools.
        for qn in query_named:
            if qn == name:
                continue
            row = cooc_index.cooccurrence.get(qn, {})
            if name in row:
                score += float(row[name])
        if score > 0.0:
            scores.append((i, score, i))

    # Sort by score DESC; stable on index to give a reproducible tiebreak.
    scores.sort(key=lambda triple: (-triple[1], triple[2]))
    return [i for i, _, _ in scores]


async def get_dynamic_hot_set(
    user_id: str | None = None,
    *,
    top_k: int = _HOT_SET_TOP_K,
    call_cap: int = 100,
) -> frozenset[str]:
    """Return the top-K most-dispatched tools for a user (or globally).

    Reads the last ``call_cap`` rows of ``tool_call_telemetry`` (filtered by
    ``user_id`` when provided), tallies dispatch frequency, and returns the
    top-K tool names as a frozenset.  Cached per ``user_id`` with a 5-min
    refresh window.

    Falls back to the static ``HOT_SET_TOOLS`` from ``categories.py`` when:
    - Mongo / Persistence is unavailable;
    - No telemetry rows match the filter (cold-start);
    - The MCP find call raises.

    The static fallback preserves the M1 behavior so a fresh deploy without
    any telemetry still has a sensible hot set.

    Args:
        user_id: optional user filter.  ``None`` (default) tallies globally
            across all sessions.
        top_k: maximum tools to return (default 8 — matches the static
            HOT_SET_TOOLS size).
        call_cap: telemetry rows to sample (default 100).

    Returns:
        Frozen set of tool names.  Always non-empty when the static fallback
        engages; may be empty only if the static HOT_SET_TOOLS is empty
        (which it isn't in production).
    """
    # Resolve static fallback up-front so any failure path is symmetric.
    try:
        from trid3nt_server.categories import HOT_SET_TOOLS as _STATIC_HOT_SET
    except Exception:  # noqa: BLE001 — defensive against import-order races
        _STATIC_HOT_SET = frozenset()

    cache_key = user_id  # ``None`` is a valid key (global)
    now = time.monotonic()
    with _HOT_SET_LOCK:
        cached = _HOT_SET_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _HOT_SET_REFRESH_SECONDS:
        return cached[1]

    persistence = _get_persistence_safe()
    if persistence is None or getattr(persistence, "_mcp", None) is None:
        return _STATIC_HOT_SET

    try:
        from trid3nt_contracts.mongo_collections import TELEMETRY_COLLECTION
        from trid3nt_server.persistence import DEFAULT_DATABASE, _unwrap_mcp_result
    except Exception:  # noqa: BLE001
        return _STATIC_HOT_SET

    filt: dict[str, Any] = {}
    if user_id is not None:
        filt["user_id"] = user_id

    try:
        raw = await persistence._mcp.call_tool(
            "find",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                "filter": filt,
                "sort": {"_id": -1},
                "limit": call_cap,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("dynamic hot-set: telemetry find failed (%s)", exc)
        return _STATIC_HOT_SET

    docs = _unwrap_mcp_result(raw) or []
    if isinstance(docs, dict):
        docs = [docs]
    if not isinstance(docs, list) or not docs:
        return _STATIC_HOT_SET

    counts: dict[str, int] = {}
    for d in docs:
        if not isinstance(d, dict):
            continue
        tool = d.get("tool_name")
        if not isinstance(tool, str):
            continue
        counts[tool] = counts.get(tool, 0) + 1

    if not counts:
        return _STATIC_HOT_SET

    # Top-K by count DESC; stable tiebreak by name for determinism.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    result = frozenset(name for name, _ in ranked[:top_k])

    with _HOT_SET_LOCK:
        _HOT_SET_CACHE[cache_key] = (now, result)
    return result


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion.
# ---------------------------------------------------------------------------


def _reciprocal_rank_fusion(
    rankings: list[list[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of N rank lists.

    Each ``rankings[m]`` is a list of doc indices in descending relevance.
    The fused score for doc ``i`` is::

        sum_m  1.0 / (k + rank_m(i))

    Where rank starts at 1. Returns ``[(doc_index, fused_score), ...]``
    sorted by score descending. Docs absent from a ranking simply don't
    contribute that modality's term — the formula is rank-aware so the two
    retrieval modalities don't need score normalization.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    out = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Query-time matched-queries selection.
# ---------------------------------------------------------------------------


def _match_synthetic_queries(
    query: str, synthetic_queries: list[str], limit: int = 3
) -> list[str]:
    """Pick up to ``limit`` synthetic queries that overlap most with ``query``.

    Best-effort lexical: rank by count of shared (lowercased) content tokens.
    Returned to the caller as ``matched_queries`` so the LLM can see *why*
    this tool surfaced.
    """
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


#: Generic operator/shape tokens that appear in many utility tool names but
#: that the LLM uses descriptively in queries. Skipped by the name-substring
#: ranker so e.g. "national parks polygons" doesn't over-boost
#: ``clip_vector_to_polygon`` or ``clip_raster_to_polygon`` over the
#: data-intent target ``fetch_wdpa_protected_areas``.
_NAME_RANKER_GENERICS: set[str] = {
    "polygon",
    "polygons",
    # Bare domain nouns (2026-07-22): content channels route these; letting
    # them earn NAME-channel RRF terms made name-bearing tools (fetch_buildings,
    # compute_flood_depth_damage) structurally unbeatable for analytical asks
    # like "summary statistics for the building layer" (spatial_query fold).
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
    "model",  # too common — present in many *_model_* names
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
}


# ---------------------------------------------------------------------------
# Tool registration.
# ---------------------------------------------------------------------------


_SEARCH_TOOLS_METADATA = AtomicToolMetadata(
    name="search_tools",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _SEARCH_TOOLS_METADATA,
    supports_global_query=False,
    # Annotations: readOnlyHint=True (in-process BM25 + dense retrieval; no
    # external API calls or state mutations), openWorldHint=False (queries the
    # local tool corpus, not the internet), destructiveHint=False,
    # idempotentHint=True (deterministic ranking for the same query + corpus).
)
async def search_tools(
    query: str,
    top_k: int = 5,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Route a free-text user query to the top-k matching atomic tools.

    Use this when: the LLM has a free-text need ("show me flood zones", "national
    parks polygons in Yosemite", "fetch hurricane wind probabilities") and wants
    a narrow shortlist of atomic tools to call next, rather than scanning the
    full 70+ tool surface. Hybrid retrieval ranks tools by BM25 over the
    audited docstring + Wave 4.10 synthetic example-query corpus, fused with a
    dense-embedding similarity (sentence-transformers all-MiniLM-L6-v2 or
    Vertex text-embedding-005 when available, else hashed token vector as a
    deterministic fallback) via reciprocal rank fusion (RRF, k=60).

    Do NOT use this for: enumerating EVERY atomic tool the agent has (the ADK
    tool catalog is the authoritative inventory); deciding whether to use
    Mode 1 catalog substrate (use ``search_data_catalog`` for the curator-vetted
    external-data catalog); finding a DuckDB spatial SQL function (use
    ``search_spatial_functions``); planning multi-step workflows (use
    ``solver``).

    Params:
        query: free-text user query (required, non-empty). Lowercased and
            tokenized for BM25; embedded with the dense backend.
        top_k: maximum number of tools to return (default 5). Clamped to
            [1, 25].

    Returns:
        A dict shaped::

            {
              "results": [
                {
                  "tool_name": "fetch_fema_nfhl_zones",
                  "score": 0.0312,
                  "description_snippet": "Fetches FEMA National Flood Hazard Layer...",
                  "matched_queries": ["show flood zones", "...", ...]
                },
                ...
              ]
            }

        ``score`` is the RRF fused score (rank-aware; higher = more relevant).
        ``description_snippet`` is the first ~240 chars of the tool's
        docstring. ``matched_queries`` is up to 3 synthetic-corpus queries
        that lexically overlap the user query — useful diagnostics for the
        LLM to confirm the routing made sense.

    Empty-query handling: returns ``{"results": []}`` rather than raising,
    so an LLM that fires the tool with a degenerate ``query=""`` doesn't
    surface a hard error mid-conversation.

    FR-CE-8: registered with ``ttl_class="live-no-cache"``, ``cacheable=False``
    — the routing call is sub-millisecond CPU and the result depends on the
    LLM-supplied query verbatim, so caching would be wasteful.
    """
    if not isinstance(query, str):
        return {"results": []}
    query_clean = query.strip()
    if not query_clean:
        return {"results": []}

    # Clamp top_k to a safe range.
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 5
    k = max(1, min(25, k))

    index = _get_index()
    if not index.tool_names:
        return {"results": []}

    # BM25 ranking (sorted doc indices descending).
    bm25_ranking: list[int] = []
    bm25_scores: list[float] = []
    if index.bm25 is not None:
        q_tokens = _tokenize(query_clean)
        if q_tokens:
            try:
                raw = index.bm25.get_scores(q_tokens)
                # Sort indices by score descending.
                pairs = sorted(
                    range(len(raw)), key=lambda i: float(raw[i]), reverse=True
                )
                bm25_ranking = [i for i in pairs if float(raw[i]) > 0.0]
                bm25_scores = [float(s) for s in raw]
            except Exception as exc:  # noqa: BLE001
                logger.warning("BM25 scoring failed (%s); dropping BM25 channel", exc)

    # Dense ranking.
    dense_ranking: list[int] = []
    if index.dense_matrix is not None and index.dense_encode_fn is not None:
        try:
            import numpy as _np

            q_vec = index.dense_encode_fn([query_clean])
            # L2-normalize the query (the index is already normalized).
            qn = _np.linalg.norm(q_vec, axis=1, keepdims=True)
            qn[qn == 0.0] = 1.0
            q_vec = q_vec / qn
            sims = (index.dense_matrix @ q_vec[0]).astype("float32")
            pairs = sorted(
                range(len(sims)), key=lambda i: float(sims[i]), reverse=True
            )
            # Keep the dense ranking unfiltered; RRF handles low-similarity items.
            dense_ranking = pairs
        except Exception as exc:  # noqa: BLE001
            logger.warning("dense scoring failed (%s); dropping dense channel", exc)

    # Name-substring ranking — third channel that catches "model flooding"
    # → run_model_flood_scenario even when BM25 misses ("flooding" ≠
    # "flood-modeling"). Score = count of query content tokens whose
    # (optionally de-suffixed) form is a substring of the tool name.
    # Generic operator words ("polygon", "raster", "clip", "compute", "run",
    # "fetch", "vector") are filtered so e.g. "national parks polygons"
    # doesn't over-boost ``clip_vector_to_polygon``.
    name_substr_ranking: list[int] = []
    q_content_tokens = [
        t
        for t in _tokenize(query_clean)
        if t not in _STOPWORDS and t not in _NAME_RANKER_GENERICS
    ]
    if q_content_tokens:
        scored_names: list[tuple[int, int]] = []
        for i, name in enumerate(index.tool_names):
            name_low = name.lower()
            hits = sum(1 for t in q_content_tokens if t in name_low)
            stem_hits = 0
            for t in q_content_tokens:
                stem = t
                for suf in ("ing", "ed", "s"):
                    if stem.endswith(suf) and len(stem) > len(suf) + 2:
                        stem = stem[: -len(suf)]
                        break
                if stem != t and stem in name_low:
                    stem_hits += 1
            total = hits + stem_hits
            if total > 0:
                scored_names.append((total, i))
        scored_names.sort(key=lambda pair: pair[0], reverse=True)
        name_substr_ranking = [i for _, i in scored_names]

    # Co-occurrence ranking (Wave 4.11 M5 — 4th channel).
    # When telemetry is available, tools that frequently co-occur with tools
    # the user explicitly named in the query get boosted.  Cache-backed so
    # subsequent calls within the 5-min window don't hit Mongo.
    # Falls through silently (empty ranking) on any error.
    cooc_ranking: list[int] = []
    try:
        cooc_index = await _get_cooccurrence_index()
    except Exception as exc:  # noqa: BLE001 — telemetry-channel failure is non-fatal
        logger.debug("co-occurrence index fetch failed (%s)", exc)
        cooc_index = None
    if cooc_index is not None:
        try:
            cooc_ranking = _build_cooccurrence_ranking(
                index.tool_names, q_content_tokens, cooc_index
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("co-occurrence ranking failed (%s)", exc)
            cooc_ranking = []

    # Fuse. If no channel produced a ranking, fall back to a substring
    # match over tool names so the routing still produces *something* (better
    # than empty).
    rankings = [
        r
        for r in (bm25_ranking, dense_ranking, name_substr_ranking, cooc_ranking)
        if r
    ]
    if not rankings:
        substr = [
            i
            for i, name in enumerate(index.tool_names)
            if query_clean.lower() in name.lower()
        ]
        if substr:
            rankings = [substr]
    if not rankings:
        return {"results": []}

    fused = _reciprocal_rank_fusion(rankings, k=60)

    # Build the response payload.
    results: list[dict[str, Any]] = []
    for idx, score in fused[:k]:
        tool_name = index.tool_names[idx]
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
