"""Unit tests for the engine-output chart producers (task-198).

Wires the three non-raster engine quantities to the product as Vega-Lite
charts emitted from the composer body via the live pipeline emitter:

1. OpenQuake hazard CURVE (PoE vs IML, log-log) + UHS (SA vs period) lines, from
   the ALREADY-parsed ``parse_hazard_curve_csv`` / ``parse_uhs_csv`` arrays.
2. MODFLOW regional water-budget BAR chart, from
   ``BudgetPartitionLayerURI.budget_partition_m3_day`` (real CBC terms).
3. MODFLOW sustainable-yield head-decline LINE chart, from
   ``DrawdownLayerURI.head_decline_timeseries``.

The honesty floor (Invariant 1 / FR-AS-7): every chart's ``data.values`` are
the REAL parsed numbers, never synthesized; an ABSENT series emits NO chart
(the builder returns ``None``). These tests assert both halves:

- each builder returns a valid ``is_chart_emission_result`` payload (envelope
  discriminator + a dict ``vega_lite_spec`` carrying a line/bar mark + inline
  ``data.values`` + a str ``chart_id``) from a small real-shaped input; and
- an absent / degenerate series yields ``None`` (nothing emitted).

Plus the EMITTER seam: ``PipelineEmitter.emit_chart`` sends a ``chart-emission``
frame on the wire AND invokes the server-wired persist hook; the module-level
``emit_chart_payloads`` no-ops cleanly when no emitter is bound.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from trid3nt_contracts import new_ulid

from trid3nt_server.pipeline_emitter import (
    PipelineEmitter,
    emit_chart_payloads,
)
from trid3nt_server.tools.processing.charts_common import build_budget_partition_chart, build_hazard_curve_chart, build_head_decline_chart, build_uhs_chart, is_chart_emission_result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _spec(payload: dict[str, Any]) -> dict[str, Any]:
    assert is_chart_emission_result(payload), payload
    spec = payload["vega_lite_spec"]
    assert isinstance(spec, dict)
    # Contract: the v5 schema is stamped by build_chart_payload.
    assert spec.get("$schema", "").endswith("/v5.json"), spec.get("$schema")
    return spec


def _inline_values(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the single-view OR first-layer inline data.values rows."""
    if "data" in spec:
        return spec["data"]["values"]
    # layered spec (hazard curve) - the line layer is first.
    return spec["layer"][0]["data"]["values"]


# --------------------------------------------------------------------------- #
# 1a. OpenQuake hazard CURVE (log-log PoE vs IML line)
# --------------------------------------------------------------------------- #


def test_hazard_curve_chart_from_real_arrays() -> None:
    payload = build_hazard_curve_chart(
        imls_g=[0.05, 0.1, 0.2, 0.4, 0.8],
        mean_poe=[0.92, 0.55, 0.12, 0.02, 0.002],
        imt="PGA",
        investigation_time_years=50.0,
        n_sites=144,
        source_layer_uri="s3://runs/seismic.tif",
    )
    assert payload is not None
    spec = _spec(payload)
    # A LAYERED line chart (line + design-level rule).
    line_layer = spec["layer"][0]
    assert line_layer["mark"]["type"] == "line"
    # log-log scales on both axes.
    assert line_layer["encoding"]["x"]["scale"]["type"] == "log"
    assert line_layer["encoding"]["y"]["scale"]["type"] == "log"
    rows = _inline_values(spec)
    # Every plotted PoE is a REAL parsed value (no synthesis).
    assert [r["poe"] for r in rows] == [0.92, 0.55, 0.12, 0.02, 0.002]
    assert [r["iml"] for r in rows] == [0.05, 0.1, 0.2, 0.4, 0.8]
    # The 10%-in-50yr design rule is present.
    rule_layer = spec["layer"][1]
    assert rule_layer["mark"]["type"] == "rule"
    assert rule_layer["data"]["values"][0]["poe_level"] == 0.1
    assert "50yr" in rule_layer["data"]["values"][0]["label"]
    assert payload["source_layer_uri"] == "s3://runs/seismic.tif"


def test_hazard_curve_chart_drops_nonpositive_points_for_log() -> None:
    # A log axis rejects <= 0; those points are dropped, the rest plotted.
    payload = build_hazard_curve_chart(
        imls_g=[0.0, 0.1, 0.2],
        mean_poe=[1.0, 0.5, 0.0],
        imt="SA(0.2)",
        investigation_time_years=50.0,
    )
    assert payload is not None
    rows = _inline_values(_spec(payload))
    # Only (0.1, 0.5) survives - both axis values strictly positive.
    assert rows == [{"iml": 0.1, "poe": 0.5}]


def test_hazard_curve_chart_absent_series_emits_nothing() -> None:
    assert (
        build_hazard_curve_chart(
            imls_g=[], mean_poe=[], imt="PGA", investigation_time_years=50.0
        )
        is None
    )
    # mismatched lengths -> None
    assert (
        build_hazard_curve_chart(
            imls_g=[0.1, 0.2],
            mean_poe=[0.5],
            imt="PGA",
            investigation_time_years=50.0,
        )
        is None
    )
    # all non-positive -> nothing plottable on a log axis -> None
    assert (
        build_hazard_curve_chart(
            imls_g=[0.0, -1.0],
            mean_poe=[0.0, 0.0],
            imt="PGA",
            investigation_time_years=50.0,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# 1b. OpenQuake UHS (SA vs period line)
# --------------------------------------------------------------------------- #


def test_uhs_chart_from_real_arrays() -> None:
    payload = build_uhs_chart(
        periods_s=[0.5, 0.0, 0.2, 1.0, 0.1],
        mean_sa_g=[0.30, 0.45, 0.55, 0.18, 0.50],
        poe=0.1,
        n_sites=144,
    )
    assert payload is not None
    spec = _spec(payload)
    assert spec["mark"]["type"] == "line"
    rows = _inline_values(spec)
    # Rows are SORTED by period (PGA at 0.0 first) for a left-to-right spectrum.
    assert [r["period"] for r in rows] == [0.0, 0.1, 0.2, 0.5, 1.0]
    # Values follow their periods (real parsed SA, never invented).
    assert rows[0]["sa"] == 0.45  # period 0.0 (PGA)
    assert rows[2]["sa"] == 0.55  # period 0.2


def test_uhs_chart_absent_series_emits_nothing() -> None:
    assert build_uhs_chart(periods_s=[], mean_sa_g=[]) is None
    assert build_uhs_chart(periods_s=[0.1, 0.2], mean_sa_g=[0.3]) is None


# --------------------------------------------------------------------------- #
# 2. MODFLOW budget partition (signed inflow/outflow bars)
# --------------------------------------------------------------------------- #


def test_budget_partition_chart_from_real_terms() -> None:
    payload = build_budget_partition_chart(
        budget_partition_m3_day={
            "upgradient_chd_in": 1200.0,
            "downgradient_chd_out": -1180.0,
            "wel": -50.0,
        },
        source_layer_uri="s3://runs/budget.tif",
    )
    assert payload is not None
    spec = _spec(payload)
    assert spec["mark"]["type"] == "bar"
    rows = _inline_values(spec)
    assert len(rows) == 3
    by_term = {r["term"]: r for r in rows}
    # Signs preserved verbatim (extraction negative); direction tagged by sign.
    assert by_term["upgradient_chd_in"]["flow_m3_day"] == 1200.0
    assert by_term["upgradient_chd_in"]["direction"] == "inflow"
    assert by_term["downgradient_chd_out"]["flow_m3_day"] == -1180.0
    assert by_term["downgradient_chd_out"]["direction"] == "outflow"
    assert by_term["wel"]["direction"] == "outflow"


def test_budget_partition_chart_empty_emits_nothing() -> None:
    assert build_budget_partition_chart(budget_partition_m3_day={}) is None


# --------------------------------------------------------------------------- #
# 3. MODFLOW head-decline (drawdown vs time line)
# --------------------------------------------------------------------------- #


def test_head_decline_chart_from_real_series() -> None:
    payload = build_head_decline_chart(
        head_decline_timeseries=[0.0, 1.1, 2.4, 3.7, 4.2],
        days_per_step=30.0,
        source_layer_uri="s3://runs/drawdown.tif",
    )
    assert payload is not None
    spec = _spec(payload)
    assert spec["mark"]["type"] == "line"
    rows = _inline_values(spec)
    # x is elapsed days when days_per_step is supplied; y is the REAL decline.
    assert rows[0] == {"x": 0.0, "decline_m": 0.0}
    assert rows[1] == {"x": 30.0, "decline_m": 1.1}
    assert rows[4] == {"x": 120.0, "decline_m": 4.2}


def test_head_decline_chart_timestep_x_without_days() -> None:
    payload = build_head_decline_chart(head_decline_timeseries=[0.0, 2.0, 5.0])
    assert payload is not None
    rows = _inline_values(_spec(payload))
    # No days_per_step -> bare timestep index.
    assert [r["x"] for r in rows] == [0, 1, 2]


def test_head_decline_chart_absent_or_single_point_emits_nothing() -> None:
    assert build_head_decline_chart(head_decline_timeseries=None) is None
    assert build_head_decline_chart(head_decline_timeseries=[]) is None
    # A single point is not a trend line.
    assert build_head_decline_chart(head_decline_timeseries=[1.0]) is None


# --------------------------------------------------------------------------- #
# 4. Emitter seam - emit_chart sends the wire frame + invokes the persist hook
# --------------------------------------------------------------------------- #


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.mark.asyncio
async def test_emit_chart_sends_frame_and_persists() -> None:
    sink = _CapturingSink()
    persisted: list[dict[str, Any]] = []

    async def _persist(payload: dict) -> None:
        persisted.append(payload)

    emitter = PipelineEmitter(
        session_id=new_ulid(), sink=sink, chart_persist=_persist
    )
    payload = build_budget_partition_chart(
        budget_partition_m3_day={"chd_in": 1063.4, "chd_out": -1063.4}
    )
    assert payload is not None

    await emitter.emit_chart(payload)

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    assert len(chart_frames) == 1
    wire = chart_frames[0]["payload"]
    assert wire["envelope_type"] == "chart-emission"
    assert wire["vega_lite_spec"]["mark"]["type"] == "bar"
    # created_turn_id stamped (the session id here, no open pipeline).
    assert wire.get("created_turn_id")
    # Persist hook fired exactly once with the same payload.
    assert len(persisted) == 1
    assert persisted[0]["chart_id"] == payload["chart_id"]


@pytest.mark.asyncio
async def test_emit_chart_persist_failure_does_not_raise() -> None:
    sink = _CapturingSink()

    async def _persist(_payload: dict) -> None:
        raise RuntimeError("atlas wobble")

    emitter = PipelineEmitter(
        session_id=new_ulid(), sink=sink, chart_persist=_persist
    )
    payload = build_head_decline_chart(head_decline_timeseries=[0.0, 1.0, 2.0])
    assert payload is not None
    # A persistence failure is swallowed - the frame still went out.
    await emitter.emit_chart(payload)
    assert any(f["type"] == "chart-emission" for f in sink.frames)


@pytest.mark.asyncio
async def test_emit_chart_payloads_noop_without_emitter() -> None:
    # No current_emitter bound (direct/verify/CI path) -> clean no-op, no raise.
    payload = build_head_decline_chart(head_decline_timeseries=[0.0, 1.0, 2.0])
    await emit_chart_payloads(payload)
    await emit_chart_payloads([payload, None])
    await emit_chart_payloads(None)


@pytest.mark.asyncio
async def test_emit_chart_payloads_emits_each_via_current_emitter() -> None:
    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def workflow() -> str:
        # Inside emit_tool_call -> current_emitter() is bound to `emitter`.
        c1 = build_head_decline_chart(head_decline_timeseries=[0.0, 1.0])
        c2 = build_budget_partition_chart(
            budget_partition_m3_day={"chd_in": 1.0, "chd_out": -1.0}
        )
        # A None entry (absent series) is skipped - the honesty floor.
        await emit_chart_payloads([c1, None, c2])
        return "ok"

    await emitter.emit_tool_call(
        name="Model sustainable yield",
        tool_name="run_model_sustainable_yield_scenario",
        invoke=workflow,
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    # Exactly the two non-None payloads were emitted.
    assert len(chart_frames) == 2
    marks = {f["payload"]["vega_lite_spec"]["mark"]["type"] for f in chart_frames}
    assert marks == {"line", "bar"}


# --------------------------------------------------------------------------- #
# 5. Real-composer integration - the MODFLOW composers side-emit the chart
#    through the bound emitter (proves the wiring, not just the builders).
# --------------------------------------------------------------------------- #


def _patch_archetype_run(monkeypatch: Any, layer: Any) -> None:
    """Stub the archetype run-tool the MODFLOW composers dispatch to (no solver)."""
    import trid3nt_server.tools.simulation.run_modflow_archetype_tool as run_tool

    async def _fake_run(run_args, *, compute_class="standard"):  # noqa: ANN001
        return layer

    monkeypatch.setattr(run_tool, "run_modflow_archetype_job", _fake_run)


@pytest.mark.asyncio
async def test_regional_water_budget_composer_emits_budget_bar(monkeypatch) -> None:
    from trid3nt_contracts.modflow_contracts import BudgetPartitionLayerURI

    from trid3nt_server.workflows import (
        model_regional_water_budget_scenario as mod,
    )

    layer = BudgetPartitionLayerURI(
        layer_id="budget-RUN9",
        name="Regional Water Budget",
        layer_type="raster",
        uri="s3://b/RUN9/water_table_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m^3/day",
        budget_partition_m3_day={"chd_in": 1063.4, "chd_out": -1063.4},
    )
    _patch_archetype_run(monkeypatch, layer)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> Any:
        return await mod.model_regional_water_budget_scenario(
            aoi_latlon=(40.0, -100.0)
        )

    # emit_tool_call binds current_emitter() so the composer's side-emit fires.
    await emitter.emit_tool_call(
        name="Model regional water budget",
        tool_name="run_model_regional_water_budget_scenario",
        invoke=run,
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    assert len(chart_frames) == 1
    spec = chart_frames[0]["payload"]["vega_lite_spec"]
    assert spec["mark"]["type"] == "bar"
    rows = spec["data"]["values"]
    # The REAL CBC terms (signs preserved) are the inline data.
    by_term = {r["term"]: r["flow_m3_day"] for r in rows}
    assert by_term["chd_in"] == 1063.4
    assert by_term["chd_out"] == -1063.4


@pytest.mark.asyncio
async def test_sustainable_yield_composer_emits_head_decline(monkeypatch) -> None:
    from trid3nt_contracts.modflow_contracts import DrawdownLayerURI

    from trid3nt_server.workflows import model_sustainable_yield_scenario as mod

    layer = DrawdownLayerURI(
        layer_id="drawdown-RUN9",
        name="Pumping Drawdown",
        layer_type="raster",
        uri="s3://b/RUN9/drawdown_4326.tif",
        style_preset="continuous_drawdown_m",
        role="primary",
        units="m",
        max_drawdown_m=4.2,
        head_decline_timeseries=[0.0, 1.1, 2.4, 3.7, 4.2],
    )
    _patch_archetype_run(monkeypatch, layer)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> Any:
        return await mod.model_sustainable_yield_scenario(
            aoi_latlon=(40.0, -100.0),
            well_location_latlon=(40.01, -100.01),
            pumping_rate_m3_day=500.0,
            sim_years=1.0,
            n_periods=4,
        )

    await emitter.emit_tool_call(
        name="Model sustainable yield",
        tool_name="run_model_sustainable_yield_scenario",
        invoke=run,
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    assert len(chart_frames) == 1
    spec = chart_frames[0]["payload"]["vega_lite_spec"]
    assert spec["mark"]["type"] == "line"
    rows = spec["data"]["values"]
    # The REAL head-decline series is the inline data (5 steps).
    assert [r["decline_m"] for r in rows] == [0.0, 1.1, 2.4, 3.7, 4.2]


@pytest.mark.asyncio
async def test_sustainable_yield_no_series_emits_no_chart(monkeypatch) -> None:
    """The honesty floor end-to-end: a None head-decline series emits NO chart."""
    from trid3nt_contracts.modflow_contracts import DrawdownLayerURI

    from trid3nt_server.workflows import model_sustainable_yield_scenario as mod

    layer = DrawdownLayerURI(
        layer_id="drawdown-RUN0",
        name="Pumping Drawdown",
        layer_type="raster",
        uri="s3://b/RUN0/drawdown_4326.tif",
        style_preset="continuous_drawdown_m",
        role="primary",
        units="m",
        max_drawdown_m=4.2,
        head_decline_timeseries=None,  # absent series
    )
    _patch_archetype_run(monkeypatch, layer)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> Any:
        return await mod.model_sustainable_yield_scenario(
            aoi_latlon=(40.0, -100.0),
            well_location_latlon=(40.01, -100.01),
            pumping_rate_m3_day=500.0,
        )

    await emitter.emit_tool_call(
        name="Model sustainable yield",
        tool_name="run_model_sustainable_yield_scenario",
        invoke=run,
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    assert chart_frames == []


@pytest.mark.asyncio
async def test_seismic_composer_emits_curve_and_uhs(monkeypatch) -> None:
    """The OpenQuake follow-up: _emit_oq_curve_charts parses the REAL curve /
    UHS CSV text and side-emits both line charts through the bound emitter."""
    from trid3nt_server.workflows import model_seismic_hazard_scenario as mod

    # Real-shaped OpenQuake CSV exports (the leading '#' banner + poe-/SA columns).
    curve_csv = (
        "# generated by OpenQuake\n"
        "lon,lat,depth,poe-0.1,poe-0.2,poe-0.5\n"
        "-122.4,37.4,0.0,0.8,0.5,0.1\n"
        "-122.3,37.3,0.0,0.6,0.3,0.05\n"
    )
    uhs_csv = (
        "# generated by OpenQuake\n"
        "lon,lat,depth,0.1~PGA,0.1~SA(0.2),0.1~SA(1.0)\n"
        "-122.4,37.4,0.0,0.45,0.55,0.18\n"
        "-122.3,37.3,0.0,0.40,0.50,0.16\n"
    )

    def _fake_download(_run_id):  # noqa: ANN001
        return curve_csv, uhs_csv

    monkeypatch.setattr(mod, "_download_batch_curve_csvs", _fake_download)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> None:
        await mod._emit_oq_curve_charts(
            "RUN9",
            imt="PGA",
            poe=0.1,
            investigation_time_years=50.0,
            source_layer_uri="s3://b/RUN9/seismic_hazard_4326.tif",
        )

    await emitter.emit_tool_call(
        name="Model seismic hazard",
        tool_name="run_seismic_hazard_psha",
        invoke=run,
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    # Both the hazard CURVE (layered line) + UHS (single line) emit.
    assert len(chart_frames) == 2
    titles = {f["payload"]["title"] for f in chart_frames}
    assert any("hazard curve" in t.lower() for t in titles)
    assert any("uniform hazard spectrum" in t.lower() for t in titles)
    # The curve mean-PoE is the REAL across-site mean (poe-0.1 col: (0.8+0.6)/2).
    curve = next(
        f for f in chart_frames if "curve" in f["payload"]["title"].lower()
    )
    curve_rows = curve["payload"]["vega_lite_spec"]["layer"][0]["data"]["values"]
    poe_at_first_iml = next(r["poe"] for r in curve_rows if r["iml"] == 0.1)
    assert poe_at_first_iml == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_seismic_composer_classical_only_emits_curve_only(monkeypatch) -> None:
    """A classical-only run (no UHS export) emits ONLY the curve chart."""
    from trid3nt_server.workflows import model_seismic_hazard_scenario as mod

    curve_csv = (
        "# generated by OpenQuake\n"
        "lon,lat,depth,poe-0.1,poe-0.2\n"
        "-122.4,37.4,0.0,0.8,0.5\n"
    )

    def _fake_download(_run_id):  # noqa: ANN001
        return curve_csv, None  # no UHS

    monkeypatch.setattr(mod, "_download_batch_curve_csvs", _fake_download)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> None:
        await mod._emit_oq_curve_charts(
            "RUN0",
            imt="PGA",
            poe=0.1,
            investigation_time_years=50.0,
            source_layer_uri=None,
        )

    await emitter.emit_tool_call(
        name="Model seismic hazard", tool_name="run_seismic_hazard_psha", invoke=run
    )

    chart_frames = [f for f in sink.frames if f["type"] == "chart-emission"]
    assert len(chart_frames) == 1
    assert "hazard curve" in chart_frames[0]["payload"]["title"].lower()


@pytest.mark.asyncio
async def test_seismic_composer_no_curve_csv_emits_nothing(monkeypatch) -> None:
    """No curve / UHS CSV exported (or download failed) -> NO chart (honesty)."""
    from trid3nt_server.workflows import model_seismic_hazard_scenario as mod

    def _fake_download(_run_id):  # noqa: ANN001
        return None, None

    monkeypatch.setattr(mod, "_download_batch_curve_csvs", _fake_download)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def run() -> None:
        await mod._emit_oq_curve_charts(
            "RUNX",
            imt="PGA",
            poe=0.1,
            investigation_time_years=50.0,
            source_layer_uri=None,
        )

    await emitter.emit_tool_call(
        name="Model seismic hazard", tool_name="run_seismic_hazard_psha", invoke=run
    )

    assert [f for f in sink.frames if f["type"] == "chart-emission"] == []
