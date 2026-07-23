"""Wave 4.11 M5 — search_tools Mongo-backed co-occurrence + hot-set tests.

Coverage:
    1. ``test_co_occurrence_boost_when_mongo_bound`` — with synthetic telemetry
       wired into a mock Persistence, a frequently-co-called tool gets ranked
       higher than it would under the 3-channel baseline.
    2. ``test_falls_back_to_3_channel_when_mongo_unavailable`` — when
       Persistence is unbound, ``search_tools`` produces results without
       crashing and the co-occurrence channel silently drops out.
    3. ``test_index_refresh_within_5min_window`` — a second call within the
       refresh window reuses the cached co-occurrence index (no second Mongo
       round-trip); past the window the cache is rebuilt.
    4. ``test_get_dynamic_hot_set_returns_top_k`` — with mocked telemetry,
       ``get_dynamic_hot_set`` returns the top-K by dispatch frequency.
    5. ``test_get_dynamic_hot_set_falls_back_to_static`` — when Persistence
       is unbound the static ``HOT_SET_TOOLS`` is returned.
    6. ``test_existing_unit_tests_still_pass_smoke`` — guards against
       accidental regression of the 17 Wave 4.10 B7 tests by importing and
       sanity-checking a few key shapes.  The full suite continues to live in
       ``test_search_tools.py``; this smoke just verifies the import path
       still works alongside the new module-level state.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Trigger full tool surface registration so the index includes a realistic
# universe of candidates (mirrors test_search_tools.py setup).
from trid3nt_server.tools import (  # noqa: F401 — registration side-effect
    TOOL_REGISTRY,
    publish_layer,
)
from trid3nt_server.tools.discovery import (  # noqa: F401 — registration side-effect
    fetch_from_catalog,
    search_data_catalog,
    qgis_discovery,
)
from trid3nt_server.tools.discovery import search_tools as discover_module
from trid3nt_server.tools.simulation import solver  # noqa: F401 — registration side-effect
from trid3nt_server.workflows import model_flood_scenario  # noqa: F401

from trid3nt_server.tools.discovery.search_tools import (
    _build_cooccurrence_from_docs,
    _reset_cooccurrence_cache_for_tests,
    _reset_hot_set_cache_for_tests,
    _reset_index_for_tests,
    search_tools,
    get_dynamic_hot_set,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Reset all module-level caches before/after each test."""
    _reset_index_for_tests()
    _reset_cooccurrence_cache_for_tests()
    _reset_hot_set_cache_for_tests()
    yield
    _reset_index_for_tests()
    _reset_cooccurrence_cache_for_tests()
    _reset_hot_set_cache_for_tests()


def _make_telemetry_docs(
    pairs: list[tuple[str, str]],
    *,
    base_session: str = "01SESS",
) -> list[dict[str, Any]]:
    """Build a list of synthetic ``tool_call_telemetry`` rows.

    Each entry in ``pairs`` is ``(session_id, tool_name)``.  Returned list
    is ordered newest-first (the same order the live ``find … sort {_id:-1}``
    query produces).
    """
    docs: list[dict[str, Any]] = []
    for i, (sid, tool) in enumerate(pairs):
        docs.append(
            {
                "_id": f"01ULID{i:020d}",
                "session_id": sid,
                "tool_name": tool,
                "source": "llm",
                "args_hash": "0" * 64,
                "result_ok": True,
                "latency_ms": 10.0,
                "called_at_utc": "2026-06-09T00:00:00Z",
            }
        )
    return list(reversed(docs))  # newest-first


def _make_mock_persistence(
    find_result_docs: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Mock Persistence whose _mcp.call_tool returns ``{"documents": [...]}``."""
    persistence = MagicMock()
    persistence._mcp = MagicMock()
    docs = find_result_docs or []
    persistence._mcp.call_tool = AsyncMock(return_value={"documents": docs})
    return persistence


# ---------------------------------------------------------------------------
# 1. test_co_occurrence_boost_when_mongo_bound
# ---------------------------------------------------------------------------


def test_co_occurrence_boost_when_mongo_bound() -> None:
    """A tool that frequently co-occurs with a query-named tool is boosted.

    Synthetic telemetry: in many sessions, ``fetch_dem`` was followed by
    ``compute_hillshade``.  When the user queries "fetch_dem terrain", the
    co-occurrence channel surfaces ``compute_hillshade`` higher than the
    3-channel baseline (which would rank it on docstring overlap alone).
    """
    # Build telemetry where compute_hillshade co-occurs with fetch_dem in 5
    # sessions and compute_colored_relief co-occurs in only 1 session.
    pairs: list[tuple[str, str]] = []
    for i in range(5):
        sid = f"01SESS{i:020d}"
        pairs.append((sid, "fetch_dem"))
        pairs.append((sid, "compute_hillshade"))
    # One session that pairs fetch_dem with a different tool.
    pairs.append(("01SESS9999999999999999999", "fetch_dem"))
    pairs.append(("01SESS9999999999999999999", "compute_colored_relief"))

    docs = _make_telemetry_docs(pairs)

    # Baseline: no telemetry → rank under the 3-channel path.
    baseline_result = asyncio.run(
        search_tools("fetch_dem terrain analysis", top_k=15)
    )
    baseline_order = [r["tool_name"] for r in baseline_result["results"]]
    baseline_hillshade_rank = (
        baseline_order.index("compute_hillshade")
        if "compute_hillshade" in baseline_order
        else None
    )

    # Reset between runs so the index rebuilds (the dense matrix is OK to
    # reuse, but the co-occurrence cache must be fresh).
    _reset_cooccurrence_cache_for_tests()

    persistence = _make_mock_persistence(docs)
    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        boosted_result = asyncio.run(
            search_tools("fetch_dem terrain analysis", top_k=15)
        )

    boosted_order = [r["tool_name"] for r in boosted_result["results"]]
    assert (
        "compute_hillshade" in boosted_order
    ), f"compute_hillshade missing from boosted top-15: {boosted_order!r}"
    boosted_hillshade_rank = boosted_order.index("compute_hillshade")

    # The boosted run must rank compute_hillshade no WORSE than the baseline;
    # in practice the co-occurrence boost moves it up.
    if baseline_hillshade_rank is not None:
        assert boosted_hillshade_rank <= baseline_hillshade_rank, (
            f"co-occurrence boost expected to improve compute_hillshade rank; "
            f"baseline={baseline_hillshade_rank} boosted={boosted_hillshade_rank}"
        )
    # Verify the MCP find call was issued with the right shape.
    persistence._mcp.call_tool.assert_awaited()
    name, args = persistence._mcp.call_tool.call_args[0]
    assert name == "find"
    assert args["collection"] == "tool_call_telemetry"


# ---------------------------------------------------------------------------
# 2. test_falls_back_to_3_channel_when_mongo_unavailable
# ---------------------------------------------------------------------------


def test_falls_back_to_3_channel_when_mongo_unavailable() -> None:
    """No Persistence → search_tools returns 3-channel results, no crash."""
    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=None,
    ):
        result = asyncio.run(search_tools("show me flood zones", top_k=5))
    assert "results" in result
    names = [r["tool_name"] for r in result["results"]]
    # Canonical 3-channel expectation from Wave 4.10 B7.
    assert "fetch_fema_nfhl_zones" in names[:3]


def test_falls_back_when_persistence_mcp_raises() -> None:
    """A Persistence whose _mcp.call_tool raises must not crash discover."""
    persistence = MagicMock()
    persistence._mcp = MagicMock()
    persistence._mcp.call_tool = AsyncMock(side_effect=RuntimeError("conn refused"))
    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        result = asyncio.run(search_tools("flood zones", top_k=3))
    assert "results" in result
    # 3-channel path still surfaces the canonical answer.
    names = [r["tool_name"] for r in result["results"]]
    assert "fetch_fema_nfhl_zones" in names


# ---------------------------------------------------------------------------
# 3. test_index_refresh_within_5min_window
# ---------------------------------------------------------------------------


def test_index_refresh_within_5min_window() -> None:
    """Two search_tools calls within 5 min reuse the cached cooc index."""
    docs = _make_telemetry_docs(
        [
            ("01SESS00000000000000000001", "fetch_dem"),
            ("01SESS00000000000000000001", "compute_hillshade"),
        ]
    )
    persistence = _make_mock_persistence(docs)

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        asyncio.run(search_tools("fetch_dem terrain", top_k=5))
        find_calls_after_first = sum(
            1
            for c in persistence._mcp.call_tool.call_args_list
            if c[0][0] == "find"
        )
        asyncio.run(search_tools("fetch_dem terrain", top_k=5))
        find_calls_after_second = sum(
            1
            for c in persistence._mcp.call_tool.call_args_list
            if c[0][0] == "find"
        )

    # Second call within window should NOT trigger another Mongo find.
    assert find_calls_after_second == find_calls_after_first, (
        f"expected cached index reuse within 5-min window; "
        f"calls after first={find_calls_after_first}, after second={find_calls_after_second}"
    )

    # Past the window: simulate by manually setting the cache's built_at far
    # in the past, then verify a third call DOES refresh.
    from trid3nt_server.tools.discovery import search_tools as discover_mod

    with discover_mod._COOCCURRENCE_LOCK:
        cached = discover_mod._COOCCURRENCE_INDEX
    assert cached is not None
    # Backdate the cached index by 10 minutes.
    cached.built_at -= 10 * 60

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        asyncio.run(search_tools("fetch_dem terrain", top_k=5))
        find_calls_after_third = sum(
            1
            for c in persistence._mcp.call_tool.call_args_list
            if c[0][0] == "find"
        )
    assert find_calls_after_third > find_calls_after_second, (
        "expected refresh past 5-min window to re-hit Mongo"
    )


# ---------------------------------------------------------------------------
# 4. test_get_dynamic_hot_set_returns_top_k
# ---------------------------------------------------------------------------


def test_get_dynamic_hot_set_returns_top_k() -> None:
    """With mocked telemetry, ``get_dynamic_hot_set`` returns the top-K by count."""
    pairs: list[tuple[str, str]] = []
    # fetch_dem dispatched 10 times across various sessions.
    for i in range(10):
        pairs.append((f"01SES{i:021d}", "fetch_dem"))
    # compute_hillshade 5 times.
    for i in range(5):
        pairs.append((f"01SES{i:021d}", "compute_hillshade"))
    # geocode_location 3 times.
    for i in range(3):
        pairs.append((f"01SES{i:021d}", "geocode_location"))
    # Various single-call tools.
    for i, tool in enumerate(
        [
            "fetch_nws_alerts_conus",
            "compute_slope",
            "compute_aspect",
            "clip_raster_to_bbox",
            "fetch_fema_nfhl_zones",
            "fetch_wdpa_protected_areas",
        ]
    ):
        pairs.append((f"01XSES{i:020d}", tool))

    docs = _make_telemetry_docs(pairs)
    persistence = _make_mock_persistence(docs)

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        hot_set = asyncio.run(get_dynamic_hot_set(top_k=3))

    assert isinstance(hot_set, frozenset)
    assert hot_set == frozenset({"fetch_dem", "compute_hillshade", "geocode_location"})


def test_get_dynamic_hot_set_filters_by_user_id() -> None:
    """``user_id`` is passed through to the find filter."""
    docs = _make_telemetry_docs(
        [("01SESS00000000000000000001", "fetch_dem")]
    )
    persistence = _make_mock_persistence(docs)

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        asyncio.run(get_dynamic_hot_set(user_id="01USR0000000000000000000XX", top_k=5))

    # First call should be the find.
    name, args = persistence._mcp.call_tool.call_args_list[0][0]
    assert name == "find"
    assert args["filter"].get("user_id") == "01USR0000000000000000000XX"


# ---------------------------------------------------------------------------
# 5. test_get_dynamic_hot_set_falls_back_to_static
# ---------------------------------------------------------------------------


def test_get_dynamic_hot_set_falls_back_to_static() -> None:
    """Persistence unbound → static HOT_SET_TOOLS is returned verbatim."""
    from trid3nt_server.categories import HOT_SET_TOOLS as STATIC

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=None,
    ):
        result = asyncio.run(get_dynamic_hot_set())
    assert result == STATIC


def test_get_dynamic_hot_set_falls_back_when_mcp_raises() -> None:
    """An MCP find error falls through to the static set, not an exception."""
    from trid3nt_server.categories import HOT_SET_TOOLS as STATIC

    persistence = MagicMock()
    persistence._mcp = MagicMock()
    persistence._mcp.call_tool = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        result = asyncio.run(get_dynamic_hot_set())
    assert result == STATIC


def test_get_dynamic_hot_set_falls_back_when_no_telemetry_rows() -> None:
    """Persistence bound but the find returns no rows → static fallback."""
    from trid3nt_server.categories import HOT_SET_TOOLS as STATIC

    persistence = _make_mock_persistence([])  # empty docs
    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=persistence,
    ):
        result = asyncio.run(get_dynamic_hot_set())
    assert result == STATIC


# ---------------------------------------------------------------------------
# 6. test_existing_unit_tests_still_pass — smoke
# ---------------------------------------------------------------------------


def test_existing_unit_tests_still_pass_smoke() -> None:
    """Spot-check that the 3-channel shape from Wave 4.10 B7 still holds.

    The full 17-test suite lives in ``test_search_tools.py``; this is a
    smoke that the new co-occurrence module-level state doesn't break the
    canonical routing answers when no Persistence is bound.
    """
    # Empty / non-string query handling.
    assert asyncio.run(search_tools("", top_k=5)) == {"results": []}
    assert asyncio.run(search_tools(None, top_k=5)) == {"results": []}  # type: ignore[arg-type]

    # Canonical routing answer (no Persistence; pure 3-channel path).
    with patch(
        "trid3nt_server.tools.discovery.search_tools._get_persistence_safe",
        return_value=None,
    ):
        out = asyncio.run(search_tools("show me flood zones", top_k=5))
    names = [r["tool_name"] for r in out["results"]]
    assert "fetch_fema_nfhl_zones" in names[:3]

    # Result-shape contract preserved.
    for r in out["results"]:
        assert "tool_name" in r
        assert "score" in r
        assert "description_snippet" in r
        assert "matched_queries" in r


# ---------------------------------------------------------------------------
# 7. Build-cooccurrence-from-docs algorithmic correctness
# ---------------------------------------------------------------------------


def test_build_cooccurrence_from_docs_pair_count_per_session() -> None:
    """Two tools called multiple times in one session count as one pair."""
    docs = _make_telemetry_docs(
        [
            ("01SESS01", "fetch_dem"),
            ("01SESS01", "fetch_dem"),  # duplicate within session
            ("01SESS01", "compute_hillshade"),
            ("01SESS02", "fetch_dem"),
            ("01SESS02", "compute_hillshade"),
        ]
    )
    idx = _build_cooccurrence_from_docs(docs)
    # 2 sessions; each contributes 1 pair (fetch_dem, compute_hillshade).
    assert idx.cooccurrence["fetch_dem"]["compute_hillshade"] == 2
    assert idx.cooccurrence["compute_hillshade"]["fetch_dem"] == 2
    # Call counts ARE per-call though.
    assert idx.call_counts["fetch_dem"] == 3
    assert idx.call_counts["compute_hillshade"] == 2
    assert idx.session_count == 2


def test_build_cooccurrence_from_docs_respects_session_cap() -> None:
    """When more than ``session_cap`` sessions appear, only the newest count."""
    pairs: list[tuple[str, str]] = []
    for i in range(50):
        pairs.append((f"01SES{i:021d}", "fetch_dem"))
        pairs.append((f"01SES{i:021d}", "compute_slope"))
    docs = _make_telemetry_docs(pairs)
    idx = _build_cooccurrence_from_docs(docs, session_cap=10)
    assert idx.session_count == 10
