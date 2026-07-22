"""Unit tests for the ``discover_dataset`` atomic tool (Wave 4.10 job-B7).

Coverage:
1. Registration: tool present in ``TOOL_REGISTRY`` with the expected
   metadata (``cacheable=False``, ``ttl_class="live-no-cache"``,
   ``supports_global_query=False``).
2. Top-3 routing fidelity for the kickoff's five canonical queries:
   - "weather alerts" → ``fetch_nws_alerts_conus``
   - "show flood zones" → ``fetch_fema_nfhl_zones``
   - "national parks polygons" → ``fetch_wdpa_protected_areas``
   - "elevation Grand Canyon" → ``fetch_dem``
   - "model flooding" → ``run_model_flood_scenario``
3. ``top_k`` is honored (returns at most ``top_k`` results).
4. Empty / whitespace query does not crash and returns ``{"results": []}``.
5. Tokenizer round-trip (whitespace + lowercase + underscore preservation).
6. ``_reciprocal_rank_fusion`` is rank-aware and interleaved
   (a higher-ranked doc in EITHER ranking outscores a lower-ranked one).
7. Description snippets are present + truncated to ≤240 chars.
8. ``matched_queries`` is populated for queries that match synthetic corpus.

These tests import the agent's full tool surface (data_fetch + solver +
publish_layer + qgis_discovery + catalog + workflows) so the routing index
contains the same tools the agent server exposes at runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Force the full tool + workflow surface to register before the index builds.
from trid3nt_server.tools import (  # noqa: F401 — registration side-effect
    TOOL_REGISTRY,
    publish_layer,
)
from trid3nt_server.tools.discovery import (  # noqa: F401 — registration side-effect
    catalog_fetch,
    catalog_search,
    qgis_discovery,
)
from trid3nt_server.tools.discovery import discover_dataset as discover_module
from trid3nt_server.tools.simulation import solver  # noqa: F401 — registration side-effect
from trid3nt_server.workflows import model_flood_scenario  # noqa: F401 — registration side-effect

from trid3nt_server.tools.discovery.discover_dataset import (
    _close_vocab_matches,
    _default_corpus_path,
    _expand_query_tokens,
    _reciprocal_rank_fusion,
    _reset_index_for_tests,
    _tokenize,
    discover_dataset,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_index():
    """Reset the cached index before each test so changes to TOOL_REGISTRY
    (or to the corpus YAML via env override) are reflected immediately.
    """
    _reset_index_for_tests()
    yield
    _reset_index_for_tests()


# ---------------------------------------------------------------------------
# 1. Registration.
# ---------------------------------------------------------------------------


def test_discover_dataset_registered():
    """``discover_dataset`` is present in TOOL_REGISTRY with the right shape."""
    assert "discover_dataset" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["discover_dataset"]
    md = entry.metadata
    assert md.name == "discover_dataset"
    # Per FR-DC-6 enumeration: routing call is uncacheable.
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    # Hot-set hook (B5 will pick this up).
    assert md.supports_global_query is False


# ---------------------------------------------------------------------------
# 2. Top-3 routing fidelity.
# ---------------------------------------------------------------------------


def _run_top_k(query: str, k: int = 5) -> list[str]:
    """Helper: run the async tool and return the ranked tool-name list."""
    result = asyncio.run(discover_dataset(query, top_k=k))
    assert "results" in result
    return [r["tool_name"] for r in result["results"]]


@pytest.mark.parametrize(
    "query,expected_tool",
    [
        ("weather alerts", "fetch_nws_alerts_conus"),
        ("show flood zones", "fetch_fema_nfhl_zones"),
        ("national parks polygons", "fetch_wdpa_protected_areas"),
        ("elevation Grand Canyon", "fetch_dem"),
        ("model flooding", "run_model_flood_scenario"),
    ],
)
def test_discover_dataset_routes_canonical_queries(query: str, expected_tool: str):
    """Each kickoff-canonical query surfaces its target tool in the top 3."""
    top = _run_top_k(query, k=5)
    assert expected_tool in top[:3], (
        f"expected {expected_tool!r} in top-3 for query={query!r}; got top={top}"
    )


# ---------------------------------------------------------------------------
# 3. top_k respected + clamped.
# ---------------------------------------------------------------------------


def test_top_k_respected():
    """Asking for top_k=N returns at most N results."""
    for n in (1, 3, 5, 10):
        out = asyncio.run(discover_dataset("flood depth modeling", top_k=n))
        assert len(out["results"]) <= n


def test_top_k_clamped_to_safe_range():
    """top_k is clamped to [1, 25] — extreme values still produce a sane result."""
    out_lo = asyncio.run(discover_dataset("flood depth", top_k=0))
    assert 1 <= len(out_lo["results"]) <= 25
    out_hi = asyncio.run(discover_dataset("flood depth", top_k=10_000))
    assert len(out_hi["results"]) <= 25


def test_top_k_non_numeric_falls_back():
    """A non-numeric top_k coerces to the default rather than raising."""
    out = asyncio.run(discover_dataset("flood depth", top_k="not-an-int"))
    assert "results" in out


# ---------------------------------------------------------------------------
# 4. Empty / degenerate query handling.
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_results():
    """Empty string query returns empty result, no exception."""
    out = asyncio.run(discover_dataset("", top_k=5))
    assert out == {"results": []}


def test_whitespace_query_returns_empty_results():
    """Whitespace-only query is treated as empty (no crash)."""
    out = asyncio.run(discover_dataset("   \t\n  ", top_k=5))
    assert out == {"results": []}


def test_non_string_query_does_not_crash():
    """A non-string query (e.g. None or int) returns empty rather than raising."""
    out_none = asyncio.run(discover_dataset(None, top_k=5))  # type: ignore[arg-type]
    assert out_none == {"results": []}


# ---------------------------------------------------------------------------
# 5. Tokenizer.
# ---------------------------------------------------------------------------


def test_tokenize_basic():
    """Tokenizer lowercases and splits on non-alphanumerics, preserves
    underscores."""
    assert _tokenize("Show Me Flood-Zones in Lee County") == [
        "show",
        "me",
        "flood",
        "zones",
        "in",
        "lee",
        "county",
    ]
    assert _tokenize("fetch_nws_alerts_conus") == ["fetch_nws_alerts_conus"]


def test_tokenize_handles_none_and_non_str():
    """Tokenizer is safe against ``None`` / non-string input (returns []
    rather than raising)."""
    assert _tokenize(None) == []  # type: ignore[arg-type]
    assert _tokenize(123) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6. Reciprocal Rank Fusion properties.
# ---------------------------------------------------------------------------


def test_rrf_rank_aware_interleaving():
    """A doc that ranks high in BOTH lists outscores either list's individual
    leader that only ranks high in one."""
    # bm25 ranks doc 5 first, doc 2 second, doc 0 third.
    bm25 = [5, 2, 0, 7, 9]
    # dense ranks doc 2 first, doc 9 second, doc 0 third.
    dense = [2, 9, 0, 5, 7]
    fused = _reciprocal_rank_fusion([bm25, dense], k=60)
    # Doc 2 is rank-1 in dense and rank-2 in bm25 → highest fused score.
    # Doc 5 is rank-1 in bm25 but rank-4 in dense → lower fused than doc 2.
    fused_dict = dict(fused)
    assert fused_dict[2] > fused_dict[5]
    assert fused[0][0] == 2


def test_rrf_empty_inputs():
    """RRF with no rankings returns an empty list (no exception)."""
    assert _reciprocal_rank_fusion([], k=60) == []
    assert _reciprocal_rank_fusion([[]], k=60) == []


def test_rrf_single_ranking_preserves_order():
    """A single ranking (no duplicates — the production shape) fed through RRF
    preserves the original document order."""
    ranking = [3, 1, 4, 5, 9, 2, 6, 8]
    fused = _reciprocal_rank_fusion([ranking], k=60)
    fused_order = [d for d, _ in fused]
    assert fused_order == ranking


# ---------------------------------------------------------------------------
# 7. Description snippets present and bounded.
# ---------------------------------------------------------------------------


def test_description_snippet_bounded_length():
    """Each returned description_snippet is ≤240 chars."""
    out = asyncio.run(discover_dataset("flood zones in Lee County, FL", top_k=10))
    for r in out["results"]:
        snippet = r.get("description_snippet", "")
        assert isinstance(snippet, str)
        assert len(snippet) <= 240


def test_result_shape_is_complete():
    """Every result carries the four required fields."""
    out = asyncio.run(discover_dataset("flood zones", top_k=3))
    assert out["results"], "expected at least 1 result for 'flood zones'"
    for r in out["results"]:
        assert "tool_name" in r and isinstance(r["tool_name"], str)
        assert "score" in r and isinstance(r["score"], (int, float))
        assert "description_snippet" in r and isinstance(r["description_snippet"], str)
        assert "matched_queries" in r and isinstance(r["matched_queries"], list)


# ---------------------------------------------------------------------------
# 8. matched_queries populated when synthetic corpus overlaps.
# ---------------------------------------------------------------------------


def test_matched_queries_populated_for_corpus_hit():
    """A query that lexically overlaps a synthetic-corpus entry surfaces it
    via ``matched_queries`` (diagnostic for the LLM)."""
    out = asyncio.run(discover_dataset("show me national parks", top_k=3))
    wdpa = [r for r in out["results"] if r["tool_name"] == "fetch_wdpa_protected_areas"]
    assert wdpa, "expected fetch_wdpa_protected_areas in top results"
    matched = wdpa[0]["matched_queries"]
    assert isinstance(matched, list) and len(matched) > 0


# ---------------------------------------------------------------------------
# 9. Ignores extra kwargs (FR-AS-3 robustness against LLM-invented args).
# ---------------------------------------------------------------------------


def test_extra_kwargs_ignored():
    """``**_extra_ignored`` absorbs LLM-invented kwargs without raising."""
    out = asyncio.run(
        discover_dataset(
            "flood zones",
            top_k=3,
            unexpected="ignored",
            location=(-82.0, 26.5, -81.5, 27.0),
            verbose=True,
        )
    )
    assert "results" in out


# ---------------------------------------------------------------------------
# 10. Typo query expansion (model-free fuzzy correction, stdlib difflib).
#
# Motivating live failure: "can you show me a gradinet relief ..." (typo for
# "gradient") missed compute_colored_relief -- the BM25 channel is exact-token
# and the hashed dense fallback is equally typo-blind. The fix expands
# out-of-vocabulary query tokens with close vocabulary matches at QUERY time
# only (expansion, never replacement); the LLM always sees the raw prompt.
# ---------------------------------------------------------------------------


def test_typo_gradinet_relief_routes_without_exact_corpus_queries(
    tmp_path, monkeypatch
):
    """The typo'd NATE prompt surfaces compute_colored_relief in the top-5
    even with the exact "gradient relief" corpus queries stripped (they were
    added the same day as the live failure -- the test must not depend on
    them). The correct token "gradient" survives in the vocabulary via other
    tools' docstrings + corpus text (e.g. compute_slope), so the fuzzy
    correction "gradinet" -> "gradient" still fires.
    """
    import yaml as _yaml

    corpus = _yaml.safe_load(_default_corpus_path().read_text())
    stripped = [
        q
        for q in corpus.get("compute_colored_relief", [])
        if "gradient relief" not in q.lower()
    ]
    assert stripped != corpus.get("compute_colored_relief", []), (
        "expected to strip at least one 'gradient relief' corpus query"
    )
    corpus["compute_colored_relief"] = stripped
    corpus_file = tmp_path / "corpus_stripped.yaml"
    corpus_file.write_text(_yaml.safe_dump(corpus))
    monkeypatch.setenv("TRID3NT_TOOL_CORPUS_YAML", str(corpus_file))
    _reset_index_for_tests()  # rebuild against the stripped corpus

    # Mechanism: the typo token is out-of-vocab and corrects to "gradient".
    index = discover_module._get_index()
    assert "gradinet" not in index.vocabulary
    assert "gradient" in index.vocabulary
    assert "gradient" in _close_vocab_matches("gradinet", index.vocabulary)

    # Ranking: the raw typo phrase lands the target in the top-5.
    top = _run_top_k("can you show me a gradinet relief over this bbox", k=5)
    assert "compute_colored_relief" in top, (
        f"expected compute_colored_relief in top-5 for typo query; got {top}"
    )


@pytest.mark.parametrize(
    "query,expected_tool",
    [
        # "hillshde" is the discriminating case: WITHOUT expansion it misses
        # the top-5 entirely (verified 2026-07-22); with expansion the
        # correction "hillshade" carries BM25 + name-substring.
        ("hillshde of this terrain", "compute_hillshade"),
        ("floof depth for this neighborhood", "compute_flood_depth_damage"),
    ],
)
def test_typo_queries_route_to_target_tools(query: str, expected_tool: str):
    """Misspelled domain terms still surface their target tool in the top-5."""
    top = _run_top_k(query, k=5)
    assert expected_tool in top, (
        f"expected {expected_tool!r} in top-5 for typo query={query!r}; got {top}"
    )


def test_in_vocab_tokens_are_not_expanded():
    """The correction helper returns () for tokens already in the vocabulary,
    and a fully in-vocabulary token list round-trips unchanged."""
    index = discover_module._get_index()
    vocab = index.vocabulary
    for tok in ("flood", "hillshade", "gradient", "relief", "elevation"):
        assert tok in vocab, f"test precondition: {tok!r} should be in-vocab"
        assert _close_vocab_matches(tok, vocab) == ()
    tokens = ["flood", "depth", "hillshade"]
    assert _expand_query_tokens(tokens, vocab) == tokens


def test_short_and_numeric_tokens_never_expand():
    """Tokens shorter than 4 chars and purely numeric tokens are never
    fuzzy-corrected, whether in-vocab or not."""
    index = discover_module._get_index()
    vocab = index.vocabulary
    # Short out-of-vocab tokens (len < 4): no expansion.
    for tok in ("xzq", "dme", "flo"):
        assert _close_vocab_matches(tok, vocab) == ()
    # Purely numeric tokens: no expansion, even when len >= 4 and out-of-vocab.
    for tok in ("987654", "20261", "1234"):
        assert tok.isdigit()
        assert _close_vocab_matches(tok, vocab) == ()
    assert _expand_query_tokens(["xzq", "987654"], vocab) == ["xzq", "987654"]


def test_expansion_appends_never_replaces():
    """Original tokens stay first (order + content intact); corrections are
    appended after them."""
    index = discover_module._get_index()
    tokens = _tokenize("gradinet relief over this bbox")
    expanded = _expand_query_tokens(tokens, index.vocabulary)
    assert expanded[: len(tokens)] == tokens
    assert "gradinet" in expanded  # the typo itself is NOT removed
    assert "gradient" in expanded[len(tokens):]


def test_typo_query_ranking_is_deterministic():
    """Same typo query -> byte-identical results, within one index build and
    across an index rebuild (same registry + corpus)."""
    query = "can you show me a gradinet relief over this bbox"
    out_1 = asyncio.run(discover_dataset(query, top_k=10))
    out_2 = asyncio.run(discover_dataset(query, top_k=10))
    assert out_1 == out_2
    _reset_index_for_tests()
    out_3 = asyncio.run(discover_dataset(query, top_k=10))
    assert out_1 == out_3
