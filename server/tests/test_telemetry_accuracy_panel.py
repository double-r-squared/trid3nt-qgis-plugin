"""Unit tests for the tool-accuracy panel + live big-sim telemetry
(NATE 2026-06-17).

Covers the AGENT side of the shared wire contract:

    1. ``result_usable`` classification at the dispatch chokepoint
       (``adapter.classify_result_usable``) — incl. the headline
       status=ok-but-no-layer case asserting ``result_usable=False``.
    2. The new aggregation fields on ``_aggregate_records``: ``success_rate``,
       ``result_usability_rate``, ``routing_accuracy_rate``, ``latency_p50_ms``,
       ``latency_p95_ms`` (per_tool + top-level), and the all-zero empty shape.
    3. The routing-accuracy heuristic (failed+superseded -> routed_ok=False).
    4. ``solve_telemetry`` aggregation (recent[] + wall_clock p50/p95) folded
       into the summary, and the zero-state when no solves are recorded.
    5. The LIVE solve-progress envelope shape
       (``telemetry.build_live_solve_progress`` + ``ws.SolveProgressPayload``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from trid3nt_server.adapter import classify_result_usable, summarize_tool_result
from trid3nt_server.telemetry import (
    build_live_solve_progress,
    build_solve_telemetry_record,
)
from trid3nt_server.tool_catalog_http import (
    _aggregate_records,
    _aggregate_solve_telemetry,
    _empty_solve_telemetry,
    _empty_summary,
    _normalize_record,
    _percentile,
    build_telemetry_summary,
)
from trid3nt_contracts.ws import SolveProgressPayload


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_record(**overrides):
    base = {
        "session_id": "s1",
        "ts": "2026-06-17T10:00:00Z",
        "tool_name": "fetch_dem",
        "source": "llm",
        "success": True,
        "latency_ms": 100.0,
        "error_code": None,
        "retry_attempt": 0,
        "cached_content_token_count": None,
        "result_usable": None,
        "routed_ok": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. result_usable classification
# --------------------------------------------------------------------------- #


def test_classify_result_usable_renderable_layer_uri():
    class _LayerURI:
        layer_id = "flood-1"
        uri = "s3://bucket/flood.tif"

    res = _LayerURI()
    summary = summarize_tool_result("publish_layer", {"layer_uri": "s3://b/x.tif"})
    assert classify_result_usable("publish_layer", res, summary) is True


def test_classify_result_usable_modeled_empty_layers_is_false():
    """The HEADLINE honesty-floor case: a modeled envelope with status=ok but an
    EMPTY layers list is success=True yet result_usable=False."""
    # A solve-completed-but-render-dropped modeled envelope (NOT failure-tagged):
    # carries metrics but no layers. summarize_tool_result stamps it
    # status=error + NO_RENDERABLE_LAYER (honesty floor).
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",
        "layers": [],
        "flood": {"metrics": {"flooded_area_km2": 12.0, "max_depth_m": 2.3}},
    }
    summary = summarize_tool_result("run_model_flood_scenario", result)
    assert summary["status"] == "error"
    assert summary["error_code"] == "NO_RENDERABLE_LAYER"
    assert classify_result_usable("run_model_flood_scenario", result, summary) is False


def test_classify_result_usable_failure_tagged_modeled_is_false():
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario:FAILED:SOLVER_TIMEOUT",
        "layers": [],
        "flood": {"metrics": {"solver_version": "failed:SOLVER_TIMEOUT"}},
    }
    summary = summarize_tool_result("run_model_flood_scenario", result)
    assert classify_result_usable("run_model_flood_scenario", result, summary) is False


def test_classify_result_usable_modeled_with_layers_is_true():
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",
        "layers": [{"layer_id": "flood-1", "uri": "s3://b/flood.tif"}],
    }
    summary = summarize_tool_result("run_model_flood_scenario", result)
    assert classify_result_usable("run_model_flood_scenario", result, summary) is True


def test_classify_result_usable_data_payload_is_true():
    """A non-layer data tool returning a populated dict is a usable result."""
    result = {"count": 42, "mean_depth_m": 1.7}
    summary = summarize_tool_result("spatial_query", result)
    assert classify_result_usable("spatial_query", result, summary) is True


def test_classify_result_usable_none_result_is_none():
    """A meta/no-result path (None) has no usability notion -> None."""
    summary = summarize_tool_result("some_meta_tool", None)
    assert classify_result_usable("some_meta_tool", None, summary) is None


def test_classify_result_usable_empty_layer_key_is_false():
    """A layer-shaped dict whose only layer key is empty -> False (even with no
    envelope_type)."""
    result = {"layers": []}
    summary = summarize_tool_result("fetch_something", result)
    assert classify_result_usable("fetch_something", result, summary) is False


# --------------------------------------------------------------------------- #
# 2 + 3. aggregation: success/usability/routing/p50/p95
# --------------------------------------------------------------------------- #


def test_empty_summary_has_zeroed_accuracy_fields():
    s = _empty_summary()
    assert s["total_dispatches"] == 0
    assert s["success_rate"] == 0.0
    assert s["result_usability_rate"] is None
    assert s["routing_accuracy_rate"] is None
    assert s["latency_p50_ms"] == 0.0
    assert s["latency_p95_ms"] == 0.0
    assert s["solve_telemetry"] == _empty_solve_telemetry()
    # Empty aggregate equals the empty summary verbatim.
    assert _aggregate_records([]) == s


def test_percentile_linear_interpolation():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([10.0], 0.95) == 10.0
    # p50 of 1..5 is 3; p95 interpolates toward 5.
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == 4.8


def test_aggregate_success_and_usability_rates():
    recs = [
        # usable layer result
        _normalize_record(_make_record(latency_ms=100.0, result_usable=True)),
        # success but NOT usable (the no-layer modeled case)
        _normalize_record(
            _make_record(ts="2026-06-17T10:01:00Z", latency_ms=300.0, result_usable=False)
        ),
        # failed call
        _normalize_record(
            _make_record(
                ts="2026-06-17T10:02:00Z",
                success=False,
                error_code="UPSTREAM_API_ERROR",
                latency_ms=200.0,
                result_usable=None,
            )
        ),
    ]
    s = _aggregate_records(recs)
    assert s["total_dispatches"] == 3
    # 1 of 3 failed -> success_rate 2/3.
    assert s["error_rate_overall"] == 0.3333
    assert s["success_rate"] == 0.6667
    # result_usability_rate over the non-None records: 1 True of 2 -> 0.5.
    assert s["result_usability_rate"] == 0.5
    # latency p50/p95 over [100, 200, 300].
    assert s["latency_p50_ms"] == 200.0
    assert s["latency_p95_ms"] == 290.0
    # per_tool row carries the same fields.
    row = s["dispatches_by_tool"][0]
    assert row["name"] == "fetch_dem"
    assert row["success_rate"] == 0.6667
    assert row["result_usability_rate"] == 0.5
    assert row["latency_p50_ms"] == 200.0
    assert row["latency_p95_ms"] == 290.0


def test_aggregate_usability_rate_none_when_all_meta():
    """When every record's result_usable is None (all meta tools), the rate is
    an honest null, not 0.0."""
    recs = [
        _normalize_record(_make_record(result_usable=None)),
        _normalize_record(_make_record(ts="2026-06-17T10:01:00Z", result_usable=None)),
    ]
    s = _aggregate_records(recs)
    assert s["result_usability_rate"] is None
    assert s["dispatches_by_tool"][0]["result_usability_rate"] is None


def test_aggregate_routing_accuracy_failed_then_superseded():
    """A FAILED call immediately followed by a DIFFERENT tool in the same
    session is the mis-route signal -> routed_ok=False for that call."""
    recs = [
        # fetch_dem fails ...
        _normalize_record(
            _make_record(
                tool_name="fetch_dem",
                ts="2026-06-17T10:00:00Z",
                success=False,
                error_code="UPSTREAM_API_ERROR",
            )
        ),
        # ... then the model reaches for a DIFFERENT tool (supersession).
        _normalize_record(
            _make_record(tool_name="fetch_srtm", ts="2026-06-17T10:00:05Z")
        ),
    ]
    s = _aggregate_records(recs)
    # fetch_dem was mis-routed (failed + superseded); fetch_srtm was fine.
    # routing_accuracy_rate over both = 1 ok of 2 = 0.5.
    assert s["routing_accuracy_rate"] == 0.5
    by_tool = {t["name"]: t for t in s["dispatches_by_tool"]}
    assert by_tool["fetch_dem"]["routing_accuracy_rate"] == 0.0
    assert by_tool["fetch_srtm"]["routing_accuracy_rate"] == 1.0


def test_aggregate_routing_accuracy_failed_then_retry_same_tool_is_ok():
    """A failed call followed by the SAME tool (a corrected-args retry) is NOT a
    mis-route — routed_ok stays True."""
    recs = [
        _normalize_record(
            _make_record(
                tool_name="fetch_dem",
                ts="2026-06-17T10:00:00Z",
                success=False,
                error_code="TOOL_PARAMS_INVALID",
            )
        ),
        _normalize_record(
            _make_record(tool_name="fetch_dem", ts="2026-06-17T10:00:05Z", retry_attempt=1)
        ),
    ]
    s = _aggregate_records(recs)
    assert s["routing_accuracy_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 4. solve_telemetry aggregation
# --------------------------------------------------------------------------- #


def test_aggregate_solve_telemetry_empty():
    assert _aggregate_solve_telemetry([]) == _empty_solve_telemetry()


def test_aggregate_solve_telemetry_recent_and_percentiles():
    recs = [
        build_solve_telemetry_record(
            run_id="r1",
            backend="aws-batch",
            active_cell_count=100_000,
            grid_resolution_m=30.0,
            vcpus=8,
            wall_clock_seconds=120.0,
            aoi_km2=50.0,
            solver="sfincs",
            ts="2026-06-17T10:00:00.000Z",
        ),
        build_solve_telemetry_record(
            run_id="r2",
            backend="local-docker",
            active_cell_count=250_000,
            grid_resolution_m=20.0,
            vcpus=16,
            wall_clock_seconds=600.0,
            aoi_km2=120.0,
            solver="sfincs",
            ts="2026-06-17T10:10:00.000Z",
        ),
    ]
    sec = _aggregate_solve_telemetry(recs)
    # recent is newest-first.
    assert [r["run_id"] for r in sec["recent"]] == ["r2", "r1"]
    first = sec["recent"][0]
    assert first["solver"] == "sfincs"
    assert first["grid_resolution_m"] == 20.0
    assert first["active_cell_count"] == 250_000
    assert first["vcpus"] == 16
    assert first["wall_clock_seconds"] == 600.0
    assert first["backend"] == "local-docker"
    assert first["aoi_km2"] == 120.0
    # wall-clock percentiles over [120, 600].
    assert sec["wall_clock_p50_s"] == 360.0
    assert sec["wall_clock_p95_s"] == 576.0


def test_build_telemetry_summary_folds_solve_section(tmp_path, monkeypatch):
    # Pin both sinks into the tmp dir so the test is hermetic.
    tel_path = tmp_path / "tel.jsonl"
    solve_path = tmp_path / "solve.jsonl"
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_telemetry_path", lambda: tel_path
    )
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_solve_telemetry_path", lambda: solve_path
    )
    monkeypatch.setattr("trid3nt_server.server.get_persistence", lambda: None)

    with tel_path.open("w") as fh:
        fh.write(json.dumps(_make_record(result_usable=True)) + "\n")
    with solve_path.open("w") as fh:
        rec = build_solve_telemetry_record(
            run_id="r1",
            backend="aws-batch",
            active_cell_count=100_000,
            grid_resolution_m=30.0,
            vcpus=8,
            wall_clock_seconds=240.0,
            aoi_km2=50.0,
            ts="2026-06-17T10:00:00.000Z",
        )
        fh.write(json.dumps(rec) + "\n")

    summary = asyncio.run(build_telemetry_summary())
    assert summary["total_dispatches"] == 1
    assert summary["success_rate"] == 1.0
    assert summary["solve_telemetry"]["recent"][0]["run_id"] == "r1"
    assert summary["solve_telemetry"]["wall_clock_p50_s"] == 240.0


def test_build_telemetry_summary_solve_zero_state(tmp_path, monkeypatch):
    """No solve sink -> the solve_telemetry section is the zero-state."""
    tel_path = tmp_path / "tel.jsonl"
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_telemetry_path", lambda: tel_path
    )
    monkeypatch.setattr(
        "trid3nt_server.tool_catalog_http._get_solve_telemetry_path",
        lambda: tmp_path / "no_solves.jsonl",
    )
    monkeypatch.setattr("trid3nt_server.server.get_persistence", lambda: None)
    with tel_path.open("w") as fh:
        fh.write(json.dumps(_make_record()) + "\n")

    summary = asyncio.run(build_telemetry_summary())
    assert summary["solve_telemetry"] == _empty_solve_telemetry()


# --------------------------------------------------------------------------- #
# 5. LIVE solve-progress envelope shape
# --------------------------------------------------------------------------- #


def test_build_live_solve_progress_shape():
    p = build_live_solve_progress(
        run_id="r1",
        solver="sfincs",
        grid_resolution_m=30.0,
        active_cell_count=100_000,
        vcpus=8,
        elapsed_seconds=42.5,
        eta_seconds=300.0,
    )
    assert set(p.keys()) == {
        "run_id",
        "solver",
        "grid_resolution_m",
        "active_cell_count",
        "vcpus",
        "elapsed_seconds",
        "eta_seconds",
    }
    # Validates against the wire contract.
    payload = SolveProgressPayload(**p)
    assert payload.MESSAGE_TYPE == "solve-progress"
    assert payload.run_id == "r1"
    assert payload.eta_seconds == 300.0


def test_build_live_solve_progress_null_eta():
    p = build_live_solve_progress(
        run_id="r2",
        solver="sfincs",
        grid_resolution_m=None,
        active_cell_count=None,
        vcpus=None,
        elapsed_seconds=10.0,
        eta_seconds=None,
    )
    assert p["eta_seconds"] is None
    payload = SolveProgressPayload(**p)
    assert payload.eta_seconds is None
    assert payload.grid_resolution_m is None
