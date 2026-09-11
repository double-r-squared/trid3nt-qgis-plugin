"""``search_spatial_functions`` - BM25 lookup over the vendored DuckDB ``spatial``
function catalog. BM25 ONLY: the corpus is small and flat and the ask is usually
already specific, so no dense channel and no fusion. The vendored dump must be
REGENERATED when the duckdb spatial pin moves; until then a renamed function is
simply absent from the results rather than a crash."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.search_tools.search_tools import _tokenize

__all__ = [
    "search_spatial_functions",
    "_reset_index_for_tests",
    "_build_index",
    "SearchSpatialFunctionsError",
]

logger = logging.getLogger("trid3nt_server.tools.search.search_spatial_functions.search_spatial_functions")


class SearchSpatialFunctionsError(RuntimeError):
    """Base class for search_spatial_functions failures."""

    error_code: str = "SEARCH_SPATIAL_FUNCTIONS_ERROR"
    retryable: bool = False


# Index state (module-level, lazy-built on first call).

_INDEX_LOCK = threading.Lock()
_INDEX: "_SpatialFunctionIndex | None" = None


class _SpatialFunctionIndex:
    """In-memory BM25 index over the vendored catalog. ``entries`` stays in
    vendored-file order, PARALLEL to the bm25 corpus rows; ``bm25`` is ``None`` when
    the library is unimportable, and the search falls back to substring."""

    __slots__ = ("entries", "bm25")

    def __init__(self, entries: list[dict[str, Any]], bm25: Any) -> None:
        self.entries = entries
        self.bm25 = bm25


def _default_data_path() -> Path:
    """The vendored ``duckdb_spatial_functions.json``, env-overridable."""
    env_path = os.environ.get("TRID3NT_SPATIAL_FUNCTIONS_JSON")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # Four levels up from this module is the package root that holds tools/.
    here = Path(__file__).resolve()
    return here.parents[3] / "tools" / "duckdb_spatial_functions.json"


def _load_entries(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the vendored function catalog. Empty list when the file is missing."""
    p = path if path is not None else _default_data_path()
    if not p.exists():
        logger.warning(
            "duckdb_spatial_functions.json not found at %s; search_spatial_functions "
            "will return no results",
            p,
        )
        return []
    with p.open() as fh:
        data = json.load(fh)
    functions = data.get("functions") if isinstance(data, dict) else None
    if not isinstance(functions, list):
        return []
    return [f for f in functions if isinstance(f, dict) and f.get("function")]


def _build_index(data_path: Path | None = None) -> _SpatialFunctionIndex:
    """Construct the BM25 index from the vendored function catalog."""
    entries = _load_entries(data_path)

    documents: list[str] = []
    for e in entries:
        parts = [
            str(e.get("function", "")),
            str(e.get("function", "")),  # doubled: biases an exact-name match
            str(e.get("signature", "")),
            str(e.get("description", "")),
        ]
        documents.append(" ".join(p for p in parts if p))

    corpus_tokens = [_tokenize(doc) for doc in documents]

    bm25 = None
    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

        if corpus_tokens:
            bm25 = BM25Okapi(corpus_tokens)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("rank_bm25 unavailable; search_spatial_functions BM25 disabled (%s)", exc)
        bm25 = None

    logger.info(
        "search_spatial_functions index built: %d functions, bm25=%s",
        len(entries),
        bm25 is not None,
    )
    return _SpatialFunctionIndex(entries=entries, bm25=bm25)


def _get_index() -> _SpatialFunctionIndex:
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


_SEARCH_SPATIAL_FUNCTIONS_METADATA = AtomicToolMetadata(
    name="search_spatial_functions",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _SEARCH_SPATIAL_FUNCTIONS_METADATA,
    supports_global_query=False,
    # In-process BM25 over a vendored static file: no external call, and the same
    # query over the same corpus always ranks the same way.
)
async def search_spatial_functions(
    query: str,
    top_k: int = 5,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Look up DuckDB ``spatial`` extension SQL functions by free-text ask.

    ROUTING: composing or debugging `spatial_query` SQL without the exact function
    name or signature to hand - "distance between two points", "buffer a polygon",
    "reproject coordinates", "intersection area". Pass the raw ask or your own
    distilled one, whichever names the OPERATION more directly. NOT for discovering
    data or tools, NOT for running the SQL, and NOT for anything outside the
    `ST_*` surface - the corpus is scoped to it.

    `query` is required and non-empty; `top_k` is clamped to [1, 25].

    Returns {"results": [{function, signature, description}, ...]} ranked, with the
    signature ready to drop into a later `spatial_query` `sql`. `results` is EMPTY
    when nothing matches or the vendored catalog is unavailable - a routine no-match
    never raises.
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
    if not index.entries:
        return {"results": []}

    ranking: list[int] = []
    if index.bm25 is not None:
        q_tokens = _tokenize(query_clean)
        if q_tokens:
            try:
                raw = index.bm25.get_scores(q_tokens)
                order = sorted(range(len(raw)), key=lambda i: float(raw[i]), reverse=True)
                ranking = [i for i in order if float(raw[i]) > 0.0]
            except Exception as exc:  # noqa: BLE001
                logger.warning("search_spatial_functions: BM25 scoring failed (%s)", exc)

    if not ranking:
        # Substring fallback so a cold/absent rank_bm25 still returns something
        # useful for a query that names the function verbatim (e.g. "ST_Buffer").
        needle = query_clean.lower()
        ranking = [
            i
            for i, e in enumerate(index.entries)
            if needle in str(e.get("function", "")).lower()
            or needle in str(e.get("description", "")).lower()
        ]

    results = [
        {
            "function": index.entries[i].get("function"),
            "signature": index.entries[i].get("signature"),
            "description": index.entries[i].get("description"),
        }
        for i in ranking[:k]
    ]

    logger.info(
        "search_spatial_functions query=%r top_k=%d results=%s",
        query_clean[:80],
        k,
        [r["function"] for r in results],
    )
    return {"results": results}
