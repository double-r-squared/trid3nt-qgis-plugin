"""``search_tools`` - hybrid BM25 + dense retrieval routing a free-text need to
the atomic tools that answer it. The corpus per tool is its own docstring plus the
practitioner phrasings beside it. Dense retrieval is OPPORTUNISTIC: real embeddings
when the library is present, a hashed lexical vector otherwise, with BM25 carrying
the load in that degraded mode. Fusion is rank-aware, so no normalization."""

from __future__ import annotations

import asyncio
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
    "_lexical_reinforcement",
    "_LEX_REINFORCE_GATE_DOOR",
    "_LEX_REINFORCE_GATE_GENERAL",
    "SearchToolsError",
    "CorpusFormatError",
    "_get_cooccurrence_index",
    "_reset_cooccurrence_cache_for_tests",
    "CooccurrenceIndex",
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


# Index state (module-level, lazy-built on first call).


_INDEX_LOCK = threading.Lock()
_INDEX: "_DiscoverIndex | None" = None


# Co-occurrence index state.
#
# Rebuilt from the tool-call telemetry JSONL sink on a ~5-minute cadence, so the
# RRF boost tracks recent behaviour without re-reading the file on every call.
# Telemetry is JSONL-ONLY. When the sink is empty or unreadable the index is EMPTY
# and this fourth channel silently drops out; the three-channel ranking still works.


_COOCCURRENCE_LOCK = threading.Lock()
_COOCCURRENCE_INDEX: "CooccurrenceIndex | None" = None
_COOCCURRENCE_REFRESH_SECONDS: float = 5 * 60.0  # 5-minute refresh window


class CooccurrenceIndex:
    """Per-tool dispatch and co-occurrence counts over the sampled telemetry
    window. ``cooccurrence`` is SYMMETRIC - ``[A][B] == [B][A]`` - and counts
    sessions that dispatched both; ``built_at`` is monotonic, for the refresh."""

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
    """In-memory hybrid retrieval index. Every per-tool list is PARALLEL to
    ``tool_names``; ``dense_encode_fn`` must match the modality ``dense_matrix``
    was built with, and ``bm25`` or ``dense_matrix`` may each be ``None``."""

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
        tiers: list[str] | None = None,
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
        # An index built without tiers defaults every tool to "general", so the
        # reinforcement degrades to the champion-only gate rather than raising.
        self.tiers = tiers if tiers is not None else ["general"] * len(tool_names)




_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """Lowercasing tokenizer for BM25. Splits on any non-alphanumeric character
    but KEEPS underscores, so a tool name referenced verbatim survives as one
    token. No stemming."""
    if not isinstance(text, str):
        return []
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


# Typo query expansion: model-free, stdlib difflib, deterministic.
#
# The BM25 channel is exact-token and the hashed dense fallback hashes the same
# exact tokens, so a single misspelt content word can miss the tool that answers
# the ask outright. At QUERY time only, an out-of-vocabulary token gets up to two
# close vocabulary matches APPENDED - expansion, never replacement - so the raw
# prompt the model sees is untouched and no correct token is ever displaced.
#
# The wrappers below are installed on the built index's slots, so every consumer
# of the cached index inherits the expansion without its own code.

#: Minimum token length eligible for fuzzy correction; a short token is too
#: ambiguous to correct toward anything in particular.
_TYPO_MIN_TOKEN_LEN = 4
#: Maximum vocabulary matches appended per out-of-vocab token.
_TYPO_MAX_MATCHES = 2
#: difflib.SequenceMatcher ratio cutoff (inclusive).
_TYPO_CUTOFF = 0.8


@functools.lru_cache(maxsize=4096)
def _close_vocab_matches(token: str, vocabulary: frozenset) -> tuple[str, ...]:
    """Fuzzy vocabulary corrections for ONE query token, most similar first, or
    ``()`` when it is already in vocabulary, too short, or numeric. The vocabulary
    is SORTED first, so the result never depends on set iteration order."""
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
    """``tokens`` with fuzzy corrections APPENDED, never replaced: the originals
    keep their order and multiplicity at the head, and each distinct correction is
    appended at most once."""
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
    """Proxy over ``BM25Okapi`` that typo-expands query tokens in ``get_scores``.
    The corpus is tokenized and fed to the wrapped BM25 BEFORE this proxy exists,
    so only QUERIES are ever expanded; every other attribute delegates."""

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
    """Query-side typo expansion for the HASHED fallback ONLY, which is as
    typo-blind as BM25. A real embedding backend is subword-tolerant and must NEVER
    be handed a synthetic token join."""

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
    # First paragraph = first blank-line-separated block.
    parts = text.split("\n\n", 1)
    head = parts[0].strip().replace("\n", " ")
    head = re.sub(r"\s+", " ", head)
    if len(head) > 240:
        head = head[:237] + "..."
    return head


def _default_corpus_path() -> Path:
    """The RESIDUAL corpus file: phrasings for tools registered OUTSIDE a
    co-located folder. Every atomic tool carries its own ``corpus.yaml`` beside it.
    ``TRID3NT_TOOL_CORPUS_YAML`` pins a single file instead."""
    env_path = os.environ.get("TRID3NT_TOOL_CORPUS_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # Four levels up is the package root that holds tools/.
    here = Path(__file__).resolve()
    return here.parents[3] / "tools" / "tool_query_corpus.yaml"


def _read_corpus_yaml(p: Path) -> dict[str, list[str]]:
    """One corpus YAML file as ``{tool: [queries]}``. A MISSING file yields
    ``{}`` and the index still builds in docstring-only mode; a non-string entry
    raises ``CorpusFormatError`` naming the file rather than being dropped."""
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
    """The flat ``{tool: [queries]}`` corpus: every co-located ``corpus.yaml``
    under ``tools/`` and under ``workflows/`` - engine templates are ordinary pool
    members - then the residual file merged on top. No tier semantics."""
    here = Path(__file__).resolve()
    server_dir = here.parents[3]
    tools_dir = server_dir / "tools"
    workflows_dir = server_dir / "workflows"
    composed: dict[str, list[str]] = {}
    for base in (tools_dir, workflows_dir):
        for cpath in sorted(base.rglob("corpus.yaml")):
            composed.update(_read_corpus_yaml(cpath))
    # Merge the residual: tools registered outside either tree.
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
    """``(encode_fn, np_module, backend_name)``, or ``None`` when the library is
    absent. The model LOAD is deferred to the first encode call, so importing this
    module never pays for it."""
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
    """Final-fallback dense backend: hashed token-count vectors. LEXICAL ONLY, so
    its signal near-duplicates BM25 - it exists to keep the fusion path live
    without a real embedding library, not to add semantics."""
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
    """The BM25 + dense index over the registry and the composed corpus. ONE
    document per tool feeds both channels; a spec-driven tool indexes exactly like a
    hand-written one, with no special case."""
    snapshot = registry_snapshot if registry_snapshot is not None else dict(TOOL_REGISTRY)
    corpus = _load_corpus(corpus_path)

    tool_names: list[str] = []
    descriptions: list[str] = []
    synthetic_queries: list[list[str]] = []
    documents: list[str] = []
    corpus_tokens: list[list[str]] = []
    tiers: list[str] = []

    for name in sorted(snapshot.keys()):
        entry = snapshot[name]
        # Engine templates (tier=template) index HERE alongside general tools:
        # their practitioner phrasings do the routing. tier=internal is the ONE
        # tier withheld - an absorbed in-process seam, registry-resolvable but not
        # searchable. tier=catalog is not withheld.
        if getattr(entry.metadata, "tier", "general") in ("internal",):
            continue
        doc = getattr(entry.fn, "__doc__", "") or ""
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
        tiers.append(getattr(entry.metadata, "tier", "general") or "general")

    # The typo-expansion vocabulary REUSES the tokens the index already produced
    # rather than re-deriving them. Frozen per build, and the correction cache keys
    # on this object, so a rebuild with new content invalidates cleanly.
    vocabulary: frozenset[str] = frozenset(
        tok for toks in corpus_tokens for tok in toks
    )

    # BM25 (optional -- degrades gracefully when rank_bm25 is absent).
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

    # Dense (optional -- graceful degradation).
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
        tiers=tiers,
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




# Sampling caps: the last 30 sessions OR the last 1000 calls, whichever is
# smaller. Both guard against runaway growth as the telemetry sink ages.
_COOCC_SESSION_CAP: int = 30
_COOCC_CALL_CAP: int = 1000


async def _fetch_recent_telemetry_docs(
    *,
    call_cap: int = _COOCC_CALL_CAP,
) -> list[dict[str, Any]]:
    """The most recent ``call_cap`` tool-call rows, NEWEST FIRST and with shadow
    rows excluded. Any error returns an empty list, which the caller reads as "no
    telemetry yet". The read runs in a thread so a large sink never blocks."""
    try:
        from trid3nt_server.telemetry import load_tool_call_records
    except Exception:  # noqa: BLE001
        return []

    try:
        docs = await asyncio.to_thread(load_tool_call_records, limit=call_cap)
    except Exception as exc:  # noqa: BLE001
        logger.debug("co-occurrence: telemetry read failed (%s)", exc)
        return []

    if not isinstance(docs, list):
        return []
    # The reader already caps; enforced again here so a change there cannot
    # silently widen the window this channel samples.
    return [d for d in docs if isinstance(d, dict)][:call_cap]


def _build_cooccurrence_from_docs(
    docs: list[dict[str, Any]],
    *,
    session_cap: int = _COOCC_SESSION_CAP,
) -> CooccurrenceIndex:
    """The per-tool dispatch and pairwise co-occurrence map over the most recent
    ``session_cap`` sessions. Pair counting is PER-SESSION, not per-call: calling
    one tool three times in a session is still one co-occurrence with each other."""
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
        # Distinct tools only: the pair count is per-session.
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
    """Rebuild the co-occurrence index from the telemetry sink. Possibly EMPTY,
    which yields no boost and leaves the three-channel ranking standing; a read
    fault reaches here as an empty doc list, never as an exception."""
    docs = await _fetch_recent_telemetry_docs()
    return _build_cooccurrence_from_docs(docs)


async def _get_cooccurrence_index() -> CooccurrenceIndex | None:
    """The cached co-occurrence index, refreshed once past the refresh window.
    The pointer swap is lock-guarded but the REBUILD is not, so a slow read never
    blocks another caller - they see the stale-but-usable index until the swap."""
    global _COOCCURRENCE_INDEX
    now = time.monotonic()
    with _COOCCURRENCE_LOCK:
        cached = _COOCCURRENCE_INDEX
    if cached is not None and (now - cached.built_at) < _COOCCURRENCE_REFRESH_SECONDS:
        return cached
    new_index = await _refresh_cooccurrence_index()
    if new_index is None:
        # Keep the stale entry rather than nuking the cache: the three-channel
        # ranking works either way, and a stale index preserves a prior boost.
        return cached
    with _COOCCURRENCE_LOCK:
        _COOCCURRENCE_INDEX = new_index
    return new_index


def _reset_cooccurrence_cache_for_tests() -> None:
    """Clear the co-occurrence cache.  ONLY for tests."""
    global _COOCCURRENCE_INDEX
    with _COOCCURRENCE_LOCK:
        _COOCCURRENCE_INDEX = None


def _name_matches_query(name: str, q_content_tokens: list[str]) -> bool:
    """True iff the query's content tokens reference this tool name, by substring
    or a crude suffix stem - the same test the name-substring ranker applies."""
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
    """Rank tool indices by co-occurrence and call-frequency signal, descending,
    with ties broken on original name order so the ranking is deterministic. A
    candidate scoring zero is OMITTED: no signal means no contribution to RRF."""
    if not cooc_index.call_counts and not cooc_index.cooccurrence:
        return []

    # Set of tools whose names the user explicitly referenced in the query.
    query_named: list[str] = [
        n for n in tool_names if _name_matches_query(n, q_content_tokens)
    ]

    scores: list[tuple[int, float, int]] = []  # (score, index, tiebreak-orig-index)
    for i, name in enumerate(tool_names):
        score = 0.0
        # A candidate the query itself names is boosted by its own historical
        # dispatch frequency.
        if name in query_named:
            score += float(cooc_index.call_counts.get(name, 0))
        # And a candidate that co-occurs with a query-named tool is boosted by
        # how often the two were dispatched together.
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




def _reciprocal_rank_fusion(
    rankings: list[list[int]],
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of N rank lists of doc indices, returning
    ``[(doc_index, fused_score), ...]`` descending. Rank-aware, not score-aware, so
    the channels need no normalization and an absent doc simply contributes 0."""
    # The fused score for doc i is sum over channels of 1/(k + rank), rank from 1.
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    out = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return out


# Lexical-champion reinforcement.
#
# For a short or domain-worded query the dense channel often ranks a tool's BEST
# exact-lexical match only mid-pack, so plain RRF buries that lexical number one
# beneath tools that merely rank mid on BOTH channels. The correction is to award a
# lexically dominant tool ONE extra reciprocal-rank term, on the same 1/(k+rank)
# scale as a real channel: bounded, deterministic, never a hard slot, and no corpus
# edit. The gate is tier-aware, wider for a tier structurally disadvantaged in the
# dense and name channels.
_LEX_REINFORCE_GATE_GENERAL = 1  # reinforce only the BM25 champion
_LEX_REINFORCE_GATE_DOOR = 3     # the wider gate: the top-3 BM25 lexical matches


def _lexical_reinforcement(
    fused: list[tuple[int, float]],
    bm25_ranking: list[int],
    tiers: list[str] | None,
    *,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Re-rank ``fused`` after a bounded BM25-reinforcement term. A doc's bonus is
    ``1.0 / (k + its_bm25_rank)``, applied ONCE and only within its tier's gate.
    Returns a NEW list; the input comes back unchanged when no doc qualifies."""
    if not bm25_ranking:
        return fused
    bonus: dict[int, float] = {}
    for rank, doc in enumerate(bm25_ranking, start=1):
        tier = tiers[doc] if tiers is not None and doc < len(tiers) else "general"
        gate = _LEX_REINFORCE_GATE_DOOR if tier == "door" else _LEX_REINFORCE_GATE_GENERAL
        if rank <= gate:
            bonus[doc] = 1.0 / (k + rank)
        elif rank > _LEX_REINFORCE_GATE_DOOR:
            break  # ranks are ascending; nothing past the widest gate qualifies
    if not bonus:
        return fused
    rescored = [(doc, score + bonus.get(doc, 0.0)) for doc, score in fused]
    rescored.sort(key=lambda pair: pair[1], reverse=True)
    return rescored




def _match_synthetic_queries(
    query: str, synthetic_queries: list[str], limit: int = 3
) -> list[str]:
    """Up to ``limit`` corpus phrasings sharing the most content tokens with
    ``query``. Purely lexical, and returned so the caller can see WHY a tool
    surfaced."""
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
#: ranker so e.g. "flood zone polygons" doesn't over-boost
#: ``clip_raster_to_polygon`` over the
#: data-intent target ``fetch_fema_nfhl_zones``.
_NAME_RANKER_GENERICS: set[str] = {
    "polygon",
    "polygons",
    # Bare domain nouns: content channels route these; letting
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
    # Demonstratives carry no routing signal and appear in most AOI phrasings,
    # but the name channel STEMS a trailing "s" before substring-matching, so
    # "this" becomes "thi" and silently matches any tool name containing it -
    # a one-entry name channel is enough to take the top RRF slot.
    "this",
    "that",
    "these",
    "those",
}




_SEARCH_TOOLS_METADATA = AtomicToolMetadata(
    name="search_tools",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _SEARCH_TOOLS_METADATA,
    supports_global_query=False,
    # In-process retrieval over the local corpus: no external call, and the same
    # query over the same corpus always ranks the same way.
)
async def search_tools(
    query: str,
    top_k: int = 5,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Route a free-text need to the atomic tools that answer it.

    ROUTING: a free-text need with no obvious tool in mind - "show me flood zones",
    "hurricane wind probabilities" - where a narrow shortlist beats scanning the
    whole surface. NOT for enumerating every tool (the registry is the inventory),
    NOT for a DuckDB spatial SQL function (`search_spatial_functions`), and NOT to
    dispatch a run.

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

    # The name-substring channel catches a tool whose NAME carries the ask even
    # when BM25 misses on inflection. Score = the count of query content tokens
    # whose de-suffixed form is a substring of the tool name. Generic operator
    # words are filtered out first, or a phrase like "national parks polygons"
    # over-boosts every tool with "polygon" in its name.
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

    # The fourth channel: when telemetry is available, a tool that frequently
    # co-occurs with a tool the query explicitly names is boosted. Cache-backed, so
    # calls inside the refresh window do no I/O, and it falls through silently to an
    # empty ranking on any error.
    cooc_ranking: list[int] = []
    try:
        cooc_index = await _get_cooccurrence_index()
    except Exception as exc:  # noqa: BLE001 -- telemetry-channel failure is non-fatal
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
    fused = _lexical_reinforcement(fused, bm25_ranking, index.tiers, k=60)

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
