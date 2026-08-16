"""Shared composer for the SWMM mechanism-COMPARISON templates.

The comparison templates (``swmm_subcatchment_runoff_comparison``,
``swmm_node_hydraulics_comparison``, ``swmm_wetwell_pump_control_comparison``,
``swmm_lid_performance_comparison``, ``swmm_wq_buildup_washoff_comparison``) are
THIN composers that hand a ``ComparisonBuild`` (authored by the engine core
``swmm_mechanism_compare``) to the single entry point ``run_mechanism_comparison``.
This composer solves every variant through the shared headless solver + continuity
gate, builds ONE OVERLAY chart that visually demonstrates the knob (the compared
series in one figure), emits it, and returns a typed ``SWMMComparisonResult``.

Honesty (loud): the decks are SMALL SYNTHETIC mechanism stubs with schematic
coordinates, so the product is the chart + typed scalars, NEVER a georeferenced
map layer. Every number comes from a real parsed solver output (invariant 1); the
synthetic basis is labeled ``SyntheticInput`` provenance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.swmm_contracts import (
    SWMMComparisonResult,
    SWMMComparisonVariant,
)

from trid3nt_server.mesh.swmm_mechanism_compare import (
    ComparisonBuild,
    SolvedVariant,
    solve_variants,
)
from trid3nt_server.data.processing.charts_common import build_chart_payload
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.mechanism_compare.mechanism_compare"
)

__all__ = ["run_mechanism_comparison"]


def _overlay_chart(build: ComparisonBuild, solved: list[SolvedVariant]) -> dict[str, Any] | None:
    """Build the knob-demonstrating overlay (a multi-series Vega-Lite line chart).

    With >1 variant we plot each variant's PRIMARY (first) charted series, one
    coloured line per variant - the knob is the legend. With a SINGLE variant (the
    diversion scenario) we plot ALL of its series so the split is visible. Honesty
    floor: returns None when fewer than 2 points survive.
    """
    values: list[dict[str, Any]] = []
    if len(solved) > 1:
        for sv in solved:
            slabel = sv.variant.chart[0][0] if sv.variant.chart else ""
            for x, y in sv.series.get(slabel, []):
                values.append({"minute": round(x, 3), "value": round(y, 5),
                               "series": sv.variant.label})
    else:
        sv = solved[0]
        for slabel, _kind, _obj in sv.variant.chart:
            for x, y in sv.series.get(slabel, []):
                values.append({"minute": round(x, 3), "value": round(y, 5),
                               "series": slabel})
    if len(values) < 2:
        return None
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": False},
        "encoding": {
            "x": {"field": "minute", "type": "quantitative", "title": build.x_title},
            "y": {"field": "value", "type": "quantitative", "title": build.y_title},
            "color": {"field": "series", "type": "nominal", "title": build.knob_name},
        },
        "title": build.chart_title,
    }
    return build_chart_payload(
        vega_lite_spec=spec, title=build.chart_title, caption=build.chart_caption
    )


async def run_mechanism_comparison(build: ComparisonBuild) -> SWMMComparisonResult:
    """Solve every variant, build the overlay chart, return the typed result.

    Raises the typed ``SWMMDeckError`` from the shared solver on any continuity
    breach (a silently-wrong variant is never charted). The caller renders the
    honest error frame.
    """
    emitter = current_emitter()
    begin_substeps(emitter, 2)

    async with substep(emitter, "solve_variants"):
        solved = await asyncio.to_thread(solve_variants, build)

    async with substep(emitter, "chart_and_publish"):
        chart = _overlay_chart(build, solved)

    chart_titles: list[str] = []
    if chart is not None:
        try:
            await emit_chart_payloads(chart)
            chart_titles.append(build.chart_title)
        except Exception as exc:  # noqa: BLE001 - never break the solve on an emit miss
            logger.warning("run_mechanism_comparison: chart emit failed (%s)", exc)

    variants = [
        SWMMComparisonVariant(
            label=sv.variant.label,
            continuity_error_pct=sv.result.continuity_error_pct,
            peak_value=round(sv.primary_peak, 5),
            peak_time_min=max(0.0, round(sv.primary_peak_min, 2)),
            total_value=round(sv.total_value, 4),
            max_node_depth=max(0.0, round(sv.result.max_node_depth_m, 4)),
            n_flooded_nodes=sv.result.n_flooded_nodes,
            n_surcharged_conduits=sv.result.n_surcharged_conduits,
            extra=sv.extra,
        )
        for sv in solved
    ]

    peaks = {v.label: v.peak_value for v in variants}
    headline: dict[str, Any] = {
        "peak_by_variant": peaks,
        "knob_demonstrated": len({round(p, 4) for p in peaks.values()}) >= min(2, len(peaks)),
        **dict(build.headline_extra),
    }

    provenance = [
        SyntheticInput(
            param=build.basis_param,
            value=None,
            basis="default_demo",
            real_source_if_any=build.basis_source,
            note=build.basis_note,
        )
    ]

    result = SWMMComparisonResult(
        comparison_kind=build.comparison_kind,
        knob_name=build.knob_name,
        knob_values=list(build.knob_values),
        flow_units=build.flow_units,
        series_units=build.series_units,
        variants=variants,
        headline=headline,
        chart_titles=chart_titles,
        demonstration_note=build.demonstration_note,
        schematic_only=True,
        basis="synthetic",
        synthetic_inputs=provenance,
    )
    logger.info(
        "run_mechanism_comparison kind=%s knob=%s variants=%d demonstrated=%s peaks=%s",
        build.comparison_kind, build.knob_name, len(variants),
        headline["knob_demonstrated"], peaks,
    )
    return result
