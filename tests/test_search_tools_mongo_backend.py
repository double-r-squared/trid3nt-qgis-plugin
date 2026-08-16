"""search_tools co-occurrence (JSONL-backed) tests.

Telemetry is JSONL-only now (the ``tool_call_telemetry`` Persistence-collection
route was cut). The co-occurrence channel reads the JSONL sink via
``telemetry.load_tool_call_records``; these tests feed a temp
``TRID3NT_TELEMETRY_PATH`` file.

The per-user dynamic hot-set path (``get_dynamic_hot_set``, Mongo-backed,
gated behind ``TRID3NT_DYNAMIC_HOT_SET`` and unset in all live configs) was
cut as feature-creep; its dedicated tests were removed along with the
function.

Coverage:
    1. ``test_co_occurrence_boost_when_jsonl_populated`` — with a populated JSONL
       sink, a frequently-co-called tool ranks no worse than the empty-sink
       3-channel baseline.
    2. ``test_falls_back_to_3_channel_when_no_telemetry`` /
       ``test_malformed_jsonl_does_not_crash`` — an empty/missing/malformed sink
       leaves the co-occurrence channel out; discover still returns results.
    3. ``test_cooccurrence_index_cached_within_5min_window`` — a second call
       within the refresh window reuses the cached index (no second JSONL read);
       past the window the cache is rebuilt.
    4. ``test_existing_unit_tests_still_pass_smoke`` — guards against accidental
       regression of the canonical 3-channel routing answers.
    5. ``test_build_cooccurrence_from_docs_*`` — pure algorithmic correctness.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Trigger full tool surface registration so the index includes a realistic
# universe of candidates (mirrors test_search_tools.py setup).
from trid3nt_server.data import TOOL_REGISTRY  # noqa: F401
from trid3nt_server.data.publish_layer import publish_layer  # noqa: F401 — registration side-effect
from trid3nt_server.data.search.fetch_from_catalog import fetch_from_catalog  # noqa: F401 — registration side-effect
from trid3nt_server.data.search.search_data_catalog import search_data_catalog  # noqa: F401 — registration side-effect
from trid3nt_server.data.search.qgis_discovery import qgis_discovery  # noqa: F401 — registration side-effect
from trid3nt_server.data.search.search_tools import search_tools as discover_module
from trid3nt_server.data.simulation.solver import solver  # noqa: F401 — registration side-effect
from trid3nt_server.workflows.sfincs.flood import flood  # noqa: F401

from trid3nt_server.data.search.search_tools.search_tools import (
    _build_cooccurrence_from_docs,
    _reset_cooccurrence_cache_for_tests,
    _reset_index_for_tests,
    search_tools,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Reset all module-level caches before/after each test."""
    _reset_index_for_tests()
    _reset_cooccurrence_cache_for_tests()
    yield
    _reset_index_for_tests()
    _reset_cooccurrence_cache_for_tests()


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


def _write_telemetry_jsonl(path, pairs: list[tuple[str, str]]) -> None:
    """Write synthetic per-tool-call rows to a JSONL telemetry sink.

    ``pairs`` is ``[(session_id, tool_name), ...]`` in CHRONOLOGICAL order
    (oldest first, as the append-ordered live sink stores them);
    ``telemetry.load_tool_call_records`` reverses to newest-first. Rows carry no
    ``record_type`` so they read as per-tool-call rows (not SHADOW rows).
    """
    import json

    with open(path, "w", encoding="utf-8") as fh:
        for i, (sid, tool) in enumerate(pairs):
            fh.write(
                json.dumps(
                    {
                        "session_id": sid,
                        "ts": f"2026-06-09T00:00:{i % 60:02d}Z",
                        "tool_name": tool,
                        "source": "llm",
                        "args_hash": "0" * 64,
                        "success": True,
                        "latency_ms": 10.0,
                    }
                )
                + "\n"
            )


# ---------------------------------------------------------------------------
# 1. test_co_occurrence_boost_when_jsonl_populated
# ---------------------------------------------------------------------------


def test_co_occurrence_boost_when_jsonl_populated(tmp_path, monkeypatch) -> None:
    """A tool that frequently co-occurs with a query-named tool is boosted.

    Telemetry is JSONL-only now (the Persistence-collection read was cut). With
    a populated sink where ``fetch_dem`` co-occurs with ``compute_hillshade`` in
    5 sessions, the query "fetch_dem terrain" surfaces ``compute_hillshade`` no
    worse than the empty-sink 3-channel baseline.
    """
    # Baseline: empty telemetry sink -> co-occurrence channel drops out.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(empty))
    _reset_cooccurrence_cache_for_tests()
    baseline_result = asyncio.run(
        search_tools("fetch_dem terrain analysis", top_k=15)
    )
    baseline_order = [r["tool_name"] for r in baseline_result["results"]]
    baseline_hillshade_rank = (
        baseline_order.index("compute_hillshade")
        if "compute_hillshade" in baseline_order
        else None
    )

    # Boosted: populate the sink so compute_hillshade co-occurs with fetch_dem in
    # 5 sessions and compute_colored_relief in only 1.
    pairs: list[tuple[str, str]] = []
    for i in range(5):
        sid = f"01SESS{i:020d}"
        pairs.append((sid, "fetch_dem"))
        pairs.append((sid, "compute_hillshade"))
    pairs.append(("01SESS9999999999999999999", "fetch_dem"))
    pairs.append(("01SESS9999999999999999999", "compute_colored_relief"))
    populated = tmp_path / "populated.jsonl"
    _write_telemetry_jsonl(populated, pairs)
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(populated))
    _reset_cooccurrence_cache_for_tests()

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


# ---------------------------------------------------------------------------
# 2. test_falls_back_to_3_channel_when_no_telemetry
# ---------------------------------------------------------------------------


def test_falls_back_to_3_channel_when_no_telemetry(tmp_path, monkeypatch) -> None:
    """Empty/missing JSONL sink → search_tools returns 3-channel results, no crash."""
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(tmp_path / "does-not-exist.jsonl"))
    _reset_cooccurrence_cache_for_tests()
    result = asyncio.run(search_tools("show me flood zones", top_k=5))
    assert "results" in result
    names = [r["tool_name"] for r in result["results"]]
    # Canonical 3-channel expectation from Wave 4.10 B7.
    assert "fetch_fema_nfhl_zones" in names[:3]


def test_malformed_jsonl_does_not_crash(tmp_path, monkeypatch) -> None:
    """Malformed telemetry lines are skipped; discover still returns results."""
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n{ partial json\n\n", encoding="utf-8")
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(bad))
    _reset_cooccurrence_cache_for_tests()
    result = asyncio.run(search_tools("flood zones", top_k=3))
    assert "results" in result
    # 3-channel path still surfaces the canonical answer.
    names = [r["tool_name"] for r in result["results"]]
    assert "fetch_fema_nfhl_zones" in names


# ---------------------------------------------------------------------------
# 3. test_cooccurrence_index_cached_within_5min_window
# ---------------------------------------------------------------------------


def test_cooccurrence_index_cached_within_5min_window(tmp_path, monkeypatch) -> None:
    """Two search_tools calls within 5 min reuse the cached cooc index (one read)."""
    pairs = [
        ("01SESS00000000000000000001", "fetch_dem"),
        ("01SESS00000000000000000001", "compute_hillshade"),
    ]
    populated = tmp_path / "populated.jsonl"
    _write_telemetry_jsonl(populated, pairs)
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(populated))
    _reset_cooccurrence_cache_for_tests()

    # Spy on the JSONL reader (imported lazily inside _fetch_recent_telemetry_docs).
    import trid3nt_server.telemetry as _tel

    reads = {"n": 0}
    _real = _tel.load_tool_call_records

    def _counting(*a, **k):
        reads["n"] += 1
        return _real(*a, **k)

    monkeypatch.setattr(_tel, "load_tool_call_records", _counting)

    asyncio.run(search_tools("fetch_dem terrain", top_k=5))
    reads_after_first = reads["n"]
    asyncio.run(search_tools("fetch_dem terrain", top_k=5))
    reads_after_second = reads["n"]

    # Second call within window should NOT re-read the sink.
    assert reads_after_second == reads_after_first, (
        f"expected cached index reuse within 5-min window; "
        f"reads after first={reads_after_first}, after second={reads_after_second}"
    )

    # Past the window: backdate the cached index, then verify a third call refreshes.
    from trid3nt_server.data.search.search_tools import search_tools as discover_mod

    with discover_mod._COOCCURRENCE_LOCK:
        cached = discover_mod._COOCCURRENCE_INDEX
    assert cached is not None
    cached.built_at -= 10 * 60

    asyncio.run(search_tools("fetch_dem terrain", top_k=5))
    reads_after_third = reads["n"]
    assert reads_after_third > reads_after_second, (
        "expected refresh past 5-min window to re-read the JSONL sink"
    )


# ---------------------------------------------------------------------------
# 4. test_existing_unit_tests_still_pass — smoke
# ---------------------------------------------------------------------------


def test_existing_unit_tests_still_pass_smoke(tmp_path, monkeypatch) -> None:
    """Spot-check that the 3-channel shape from Wave 4.10 B7 still holds.

    The full 17-test suite lives in ``test_search_tools.py``; this is a
    smoke that the co-occurrence module-level state doesn't break the
    canonical routing answers with an empty telemetry sink.
    """
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(tmp_path / "empty.jsonl"))
    _reset_cooccurrence_cache_for_tests()

    # Empty / non-string query handling.
    assert asyncio.run(search_tools("", top_k=5)) == {"results": []}
    assert asyncio.run(search_tools(None, top_k=5)) == {"results": []}  # type: ignore[arg-type]

    # Canonical routing answer (empty sink; pure 3-channel path).
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
# 5. Build-cooccurrence-from-docs algorithmic correctness
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
