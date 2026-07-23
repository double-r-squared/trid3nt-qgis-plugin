"""``search_spatial_functions`` atomic tool - BM25 lookup over the vendored
DuckDB ``spatial`` extension function catalog.

Companion to ``spatial_query`` (job-B7 lane-B ADR 0019 landing): once
``spatial_query`` resolved the diet-in-miniature problem of NOT enumerating
every ``ST_*`` function inline in its own docstring (that prose was
redundant with this tool and grew unboundedly as the extension gained
functions), the LLM needs a narrow way to look one up when composing SQL.
This tool is that lookup - free-text query in, top-k
``{function, signature, description}`` matches out.

Data source: ``trid3nt_server/data/duckdb_spatial_functions.json``, a static
vendored dump of ``duckdb_functions()`` filtered to ``ST_%`` (285 entries at
generation time; scalar / aggregate / table / macro function types included).
Generated OFFLINE from the *installed* duckdb's own catalog - no web fetch at
runtime or at generation time beyond the one-time ``INSTALL spatial``. Stays
correct as long as the vendored file is regenerated when ``server``'s duckdb
pin moves to a spatial-extension release with function additions/renames;
until then it degrades gracefully (a missing/stale function is just absent
from search results, not a crash).

Reuses ``search_tools``'s BM25 + tokenizer index infra pattern (same
``rank_bm25.BM25Okapi`` library, same whitespace/lowercase tokenizer) but
scoped to this tiny 285-row corpus - no dense/embedding channel, no RRF
fusion, no telemetry co-occurrence channel. Those exist in ``search_tools``
to rank the ~190-tool registry against ambiguous natural-language asks; this
corpus is small, flat, and the query is nearly always already a fairly
specific ask ("distance between two points", "buffer a polygon",
"reproject coordinates") where BM25 alone resolves cleanly. Adding the
heavier machinery here would violate the simplicity-over-completeness norm
for no measurable recall gain at this corpus size.

Index is built lazily at first call and cached at module level, exactly like
``search_tools``. Reset for tests via ``_reset_index_for_tests()``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.discovery.search_tools import _tokenize

__all__ = [
    "search_spatial_functions",
    "_reset_index_for_tests",
    "_build_index",
    "SearchSpatialFunctionsError",
]

logger = logging.getLogger("trid3nt_server.tools.discovery.search_spatial_functions")


class SearchSpatialFunctionsError(RuntimeError):
    """Base class for search_spatial_functions failures."""

    error_code: str = "SEARCH_SPATIAL_FUNCTIONS_ERROR"
    retryable: bool = False


# ---------------------------------------------------------------------------
# Index state (module-level, lazy-built on first call).
# ---------------------------------------------------------------------------

_INDEX_LOCK = threading.Lock()
_INDEX: "_SpatialFunctionIndex | None" = None


class _SpatialFunctionIndex:
    """In-memory BM25 index over the vendored spatial-function catalog.

    Fields:
    - ``entries``: the raw list of ``{function, function_type, signature,
      description}`` dicts, in vendored-file order (parallel to ``bm25``'s
      corpus rows).
    - ``bm25``: a ``rank_bm25.BM25Okapi`` instance, or ``None`` when
      ``rank_bm25`` isn't importable (falls back to substring matching).
    """

    __slots__ = ("entries", "bm25")

    def __init__(self, entries: list[dict[str, Any]], bm25: Any) -> None:
        self.entries = entries
        self.bm25 = bm25


def _default_data_path() -> Path:
    """Resolve ``duckdb_spatial_functions.json`` under the package's ``data/`` dir."""
    env_path = os.environ.get("TRID3NT_SPATIAL_FUNCTIONS_JSON")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # tools/discovery/search_spatial_functions.py -> trid3nt_server/ is parents[2]
    here = Path(__file__).resolve()
    return here.parents[2] / "data" / "duckdb_spatial_functions.json"


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
            str(e.get("function", "")),  # doubled to bias exact-name matches
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
    # Annotations: readOnlyHint=True (in-process BM25 over a vendored static
    # file; no external calls or state mutation), openWorldHint=False,
    # destructiveHint=False, idempotentHint=True (deterministic ranking for
    # the same query + vendored corpus).
)
async def search_spatial_functions(
    query: str,
    top_k: int = 5,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Look up DuckDB ``spatial`` extension SQL functions by free-text ask.

    Use this when: composing or debugging ``spatial_query`` SQL and unfamiliar
    with the exact DuckDB spatial function name/signature - e.g. "distance
    between two points", "buffer a polygon", "reproject coordinates",
    "intersection area", "convert to geojson". Pass either the user's raw ask
    or your own distilled query (whichever names the operation more directly).
    Returns candidate ``ST_*`` functions to call directly in the ``sql``
    parameter of a subsequent ``spatial_query`` call.

    Do NOT use this for: fetching or discovering DATA/tools (use
    ``search_tools`` / ``search_data_catalog``); running the SQL itself (use
    ``spatial_query``); functions outside the DuckDB ``spatial`` extension
    (this corpus is scoped to ``ST_*``  only).

    Params:
        query: free-text ask naming the spatial operation you need
            (required, non-empty).
        top_k: maximum number of functions to return (default 5). Clamped to
            [1, 25].

    Returns:
        A dict shaped::

            {
              "results": [
                {
                  "function": "ST_Distance",
                  "signature": "ST_Distance(geom1 GEOMETRY, geom2 GEOMETRY) -> DOUBLE",
                  "description": "Computes the distance between two geometries."
                },
                ...
              ]
            }

        Empty ``results`` when nothing matches or the vendored data file is
        unavailable - never raises for a routine no-match.
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
