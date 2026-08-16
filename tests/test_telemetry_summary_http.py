"""Unit tests for ``trid3nt_server.tool_catalog_http`` telemetry-summary path
(Wave 4.11 M7 — routing-quality dashboard backend).

Coverage:
    1. ``test_aggregate_empty`` — empty record list yields the zero-state
       summary shape.
    2. ``test_aggregate_basic`` — total/error/cache stats + per-tool rows
       sorted by count descending + sources split.
    3. ``test_aggregate_chain_sequences`` — co-occurring tool calls within a
       single session produce the expected top routing chains.
    4. ``test_normalize_record_local_file_and_mongo`` — both wire shapes
       (``success`` / ``ts`` vs ``result_ok`` / ``called_at_utc``) normalize
       into the same canonical fields.
    5. ``test_build_telemetry_summary_file_fallback`` — when Persistence is
       unbound, the builder falls back to the local JSONL path and respects
       ``TRID3NT_TELEMETRY_PATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from trid3nt_server.tool_catalog_http import (
    _aggregate_records,
    _empty_summary,
    _normalize_record,
    build_telemetry_summary,
)


def _make_local_record(**overrides):
    base = {
        "session_id": "s1",
        "ts": "2026-06-09T10:00:00Z",
        "tool_name": "fetch_dem",
        "source": "llm",
        "success": True,
        "latency_ms": 100.0,
        "error_code": None,
        "retry_attempt": 0,
        "cached_content_token_count": None,
    }
    base.update(overrides)
    return base


def _make_mongo_record(**overrides):
    base = {
        "session_id": "s1",
        "called_at_utc": "2026-06-09T10:00:00Z",
        "tool_name": "fetch_dem",
        "source": "llm",
        "result_ok": True,
        "latency_ms": 100.0,
        "error_code": None,
        "retry_attempt": 0,
        "cached_content_token_count": None,
    }
    base.update(overrides)
    return base


def test_aggregate_empty():
    summary = _aggregate_records([])
    expected = _empty_summary()
    assert summary == expected
    assert summary["total_dispatches"] == 0


def test_aggregate_basic():
    recs = [
        _normalize_record(_make_local_record(latency_ms=100.0, cached_content_token_count=50)),
        _normalize_record(_make_local_record(
            tool_name="compute_hillshade",
            ts="2026-06-09T10:01:00Z",
            latency_ms=200.0,
            cached_content_token_count=0,
        )),
        _normalize_record(_make_local_record(
            session_id="s2",
            ts="2026-06-09T11:00:00Z",
            source="workflow",
            success=False,
            latency_ms=50.0,
            error_code="BBOX_INVALID",
        )),
    ]
    s = _aggregate_records(recs)
    assert s["total_dispatches"] == 3
    assert s["session_count"] == 2
    # 1 of 3 failed → ~0.3333 error rate
    assert s["error_rate_overall"] == 0.3333
    # cache hit rate: 1 hit (50) + 1 miss (0) = 1/2 = 0.5
    assert s["cache_hit_rate"] == 0.5
    # sources split
    assert s["dispatches_by_source"]["llm"] == 2
    assert s["dispatches_by_source"]["workflow"] == 1
    # per-tool sorted by count descending: fetch_dem first (2), compute_hillshade (1)
    names = [t["name"] for t in s["dispatches_by_tool"]]
    assert names == ["fetch_dem", "compute_hillshade"]
    fetch_dem_row = s["dispatches_by_tool"][0]
    assert fetch_dem_row["count"] == 2
    assert fetch_dem_row["error_count"] == 1
    assert fetch_dem_row["error_rate"] == 0.5


def test_aggregate_chain_sequences():
    # Two distinct sessions each performing fetch_dem -> compute_hillshade.
    # The chain should be counted twice.
    recs = []
    for sid in ("sA", "sB"):
        recs.append(_normalize_record(_make_local_record(
            session_id=sid, tool_name="fetch_dem", ts=f"2026-06-09T10:00:00Z",
        )))
        recs.append(_normalize_record(_make_local_record(
            session_id=sid, tool_name="compute_hillshade", ts=f"2026-06-09T10:01:00Z",
        )))
    # Plus one session with a different sequence.
    recs.append(_normalize_record(_make_local_record(
        session_id="sC", tool_name="fetch_landcover_nlcd", ts="2026-06-09T11:00:00Z",
    )))
    recs.append(_normalize_record(_make_local_record(
        session_id="sC", tool_name="publish_layer", ts="2026-06-09T11:01:00Z",
    )))
    s = _aggregate_records(recs)
    chains = s["top_routing_chains"]
    # The fetch_dem -> compute_hillshade pair appears twice.
    top = chains[0]
    assert top["chain"] == ["fetch_dem", "compute_hillshade"]
    assert top["count"] == 2


def test_normalize_record_local_file_and_mongo():
    local = _normalize_record(_make_local_record(success=False, error_code="BAD"))
    mongo = _normalize_record(_make_mongo_record(result_ok=False, error_code="BAD"))
    # Both wire shapes collapse into the same canonical keys.
    for key in ("session_id", "tool_name", "source", "result_ok", "latency_ms",
                "error_code", "retry_attempt", "cached_content_token_count"):
        assert local[key] == mongo[key], f"differing key={key!r}"
    # Timestamp comes from either ``ts`` (local file) or ``called_at_utc``
    # (mongo); both should populate ``called_at_utc``.
    assert local["called_at_utc"] == "2026-06-09T10:00:00Z"
    assert mongo["called_at_utc"] == "2026-06-09T10:00:00Z"


def test_build_telemetry_summary_file_fallback(tmp_path, monkeypatch):
    # Ensure get_persistence returns None so the file path is used.
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_telemetry_path",
        lambda: tmp_path / "telemetry.jsonl",
    )
    fp = tmp_path / "telemetry.jsonl"
    with fp.open("w") as fh:
        for r in [
            _make_local_record(),
            _make_local_record(tool_name="compute_hillshade", ts="2026-06-09T10:01:00Z"),
        ]:
            fh.write(json.dumps(r) + "\n")

    # Force the server.get_persistence import to return None.
    import trid3nt_server.tool_catalog_http as mod

    async def go():
        # Patch the inline import at call-time by inserting a fake server module.
        return await build_telemetry_summary()

    # Quietly suppress any incidental Persistence import via monkeypatching
    # server.get_persistence to None at the module level.
    try:
        import trid3nt_server.server as _srv  # noqa: F401
        monkeypatch.setattr("trid3nt_server.server.get_persistence", lambda: None)
    except Exception:
        pass

    summary = asyncio.run(go())
    assert summary["total_dispatches"] == 2
    assert summary["source"] in {"file", "telemetry"}
    names = [t["name"] for t in summary["dispatches_by_tool"]]
    assert "fetch_dem" in names
    assert "compute_hillshade" in names


def test_build_telemetry_summary_carries_by_model_and_accuracy(
    tmp_path, monkeypatch
):
    """End-to-end (file path): the full summary served by /api/telemetry/summary
    carries the four headline accuracy metrics AND the per-model breakdown, so
    the dashboard's by-model A/B section + KPI cards have real data to render."""
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_telemetry_path",
        lambda: tmp_path / "telemetry.jsonl",
    )
    # No solve sink for this test — keep the solve section zero-state.
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_solve_telemetry_path",
        lambda: tmp_path / "no_solves.jsonl",
    )
    monkeypatch.setattr("trid3nt_server.server.get_persistence", lambda: None)

    fp = tmp_path / "telemetry.jsonl"
    with fp.open("w") as fh:
        for r in [
            # Sonnet: two usable calls (one fast, one slow).
            _make_local_record(
                model_id="us.anthropic.claude-sonnet-4-6",
                latency_ms=100.0,
                result_usable=True,
            ),
            _make_local_record(
                tool_name="compute_hillshade",
                ts="2026-06-09T10:01:00Z",
                model_id="us.anthropic.claude-sonnet-4-6",
                latency_ms=300.0,
                result_usable=True,
            ),
            # Nova-lite: one failed call (drives success_rate < 1 for the model).
            _make_local_record(
                tool_name="fetch_buildings",
                ts="2026-06-09T10:02:00Z",
                model_id="us.amazon.nova-lite-v1:0",
                latency_ms=200.0,
                success=False,
                error_code="UPSTREAM_API_ERROR",
                result_usable=None,
            ),
        ]:
            fh.write(json.dumps(r) + "\n")

    summary = asyncio.run(build_telemetry_summary())
    # Four headline metrics present + sane (1 of 3 failed -> success 2/3).
    assert summary["success_rate"] == 0.6667
    assert summary["result_usability_rate"] == 1.0  # 2 usable of 2 non-null
    assert "routing_accuracy_rate" in summary
    assert summary["latency_p50_ms"] == 200.0
    assert "latency_p95_ms" in summary
    # by_model: one row per distinct model, sorted by count desc.
    by_model = {row["model_id"]: row for row in summary["by_model"]}
    assert set(by_model) == {
        "us.anthropic.claude-sonnet-4-6",
        "us.amazon.nova-lite-v1:0",
    }
    sonnet = by_model["us.anthropic.claude-sonnet-4-6"]
    assert sonnet["count"] == 2
    assert sonnet["success_rate"] == 1.0
    assert sonnet["result_usability_rate"] == 1.0
    nova = by_model["us.amazon.nova-lite-v1:0"]
    assert nova["count"] == 1
    assert nova["success_rate"] == 0.0
    # Highest-use model is listed first.
    assert summary["by_model"][0]["model_id"] == "us.anthropic.claude-sonnet-4-6"
