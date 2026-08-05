"""Engine template ``pelicun_mixed_fragility_loss_assessment`` - how do
fragility-driven and direct loss-function components combine, and how does the
EDP correlation structure change the portfolio loss spread?

One assessment mixes a drift-driven fragility component (discrete damage state ->
consequence) and an acceleration-driven direct loss-function component, then
aggregates their repair-cost decision variable. The run is repeated under a
``perfect`` and an ``independent`` EDP correlation structure so the effect of the
demand copula on the aggregate-loss dispersion is visible. The economy-of-scale
aggregation options (``across_floors`` / ``across_damage_states``) are knobs.

Idealized, domain-free: the demand, fragility, and loss models are built in
memory - no fetched hazard raster or asset inventory. The output is a loss
cumulative-distribution CHART (one curve per correlation regime), not a map.
Every narrated number reads from the typed summary this tool returns.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.agent.workflows.pelicun._validation_common import (
    PelicunValidationError,
    emit_chart_if_live,
    mixed_assessment_losses,
    multi_series_line_spec,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.pelicun.mixed_fragility_loss.mixed_fragility_loss"
)

__all__ = [
    "pelicun_mixed_fragility_loss_assessment",
    "build_loss_cdf_chart_spec",
    "TEMPLATE_CARD",
]

_REGIMES = ("independent", "perfect")


TEMPLATE_CARD = TemplateCard(
    question=(
        "how fragility-driven damage consequences and direct loss functions "
        "combine in one pelicun assessment, and how a perfect vs independent EDP "
        "correlation structure changes the portfolio repair-cost spread"
    ),
    required_inputs=[],
    knobs="sample_size, across_floors, across_damage_states",
)

_METADATA = AtomicToolMetadata(
    name="pelicun_mixed_fragility_loss_assessment",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def _summarize(v: np.ndarray) -> dict[str, float]:
    return {
        "mean": float(v.mean()), "std": float(v.std()),
        "p10": float(np.percentile(v, 10)), "p50": float(np.percentile(v, 50)),
        "p90": float(np.percentile(v, 90)),
        "spread_p90_p10": float(np.percentile(v, 90) - np.percentile(v, 10)),
    }


def build_loss_cdf_chart_spec(regime_samples: dict[str, np.ndarray]) -> dict[str, Any]:
    """Empirical loss-CDF spec, one line per correlation regime. Pure."""
    rows: list[dict[str, Any]] = []
    for regime, v in regime_samples.items():
        arr = np.sort(np.asarray(v, dtype=float))
        n = len(arr)
        if n == 0:
            continue
        # thin to <=60 quantile points per series (wire-size + readability)
        qs = np.linspace(0.0, 1.0, min(60, n))
        for q in qs:
            rows.append({
                "loss": float(np.quantile(arr, q)),
                "cumulative_probability": float(q),
                "correlation": regime,
            })
    return multi_series_line_spec(
        rows, x_field="loss", y_field="cumulative_probability",
        color_field="correlation", x_title="aggregate repair-cost (loss ratio)",
        y_title="cumulative probability",
        title="Portfolio loss CDF: perfect vs independent EDP correlation",
    )


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_mixed_fragility_loss_assessment(
    sample_size: int = 10000,
    across_floors: bool = True,
    across_damage_states: bool = True,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Mixed fragility + loss-function assessment across two EDP correlation regimes.

    Fidelity: an IDEALIZED domain-free pelicun Assessment (synthetic demand,
    fragility, and loss models; no fetched hazard/assets). Answers "how do the two
    consequence pathways combine and how sensitive is the aggregate-loss spread to
    the demand correlation structure" -- a methodology demonstration, not a site
    loss estimate. Off-scope: real per-asset damage over a hazard raster ->
    pelicun_damage_assessment.

    Use this when: the user asks about mixing fragility-driven and loss-function
    components in one run, about the AcrossFloors / AcrossDamageStates aggregation
    settings, or how perfect vs independent EDP correlation changes the loss
    uncertainty / spread.

    Params:
        sample_size: Monte-Carlo realizations per regime (default 10000).
        across_floors: economy-of-scale aggregation across floors (default True).
        across_damage_states: economy-of-scale aggregation across damage states
            (default True).

    Returns:
        On success: ``{"status": "ok", "regimes": {"independent": {mean, std,
        p10, p50, p90, spread_p90_p10}, "perfect": {...}}, "spread_ratio"
        (perfect/independent p90-p10), "chart_emitted"}``. A loss-CDF chart (one
        curve per regime) is emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if int(sample_size) <= 0:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "sample_size must be a positive integer."}
    try:
        samples: dict[str, np.ndarray] = {}
        summary: dict[str, Any] = {}
        for regime in _REGIMES:
            v = await asyncio.to_thread(
                mixed_assessment_losses,
                correlation=regime, sample_size=int(sample_size),
                across_floors=bool(across_floors),
                across_damage_states=bool(across_damage_states),
            )
            if v.size == 0:
                raise PelicunValidationError(
                    f"regime {regime!r} produced no finite loss realizations")
            samples[regime] = v
            summary[regime] = _summarize(v)
        spec = build_loss_cdf_chart_spec(samples)
        emitted = await emit_chart_if_live(
            spec, title="Portfolio loss CDF: perfect vs independent EDP correlation",
            caption="Aggregate repair-cost cumulative distribution from a mixed "
            "fragility + loss-function pelicun assessment under two demand "
            "correlation structures.")
        ind_spread = summary["independent"]["spread_p90_p10"]
        perf_spread = summary["perfect"]["spread_p90_p10"]
        spread_ratio = float(perf_spread / ind_spread) if ind_spread > 0 else float("nan")
        logger.info(
            "pelicun_mixed_fragility_loss_assessment ind_spread=%.4g perf_spread=%.4g ratio=%.3f",
            ind_spread, perf_spread, spread_ratio)
        return {
            "status": "ok", "regimes": summary, "spread_ratio": spread_ratio,
            "sample_size": int(sample_size), "chart_emitted": emitted,
        }
    except asyncio.CancelledError:
        raise
    except PelicunValidationError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_mixed_fragility_loss_assessment failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_VALIDATION_ERROR",
                "error_message": f"mixed fragility+loss assessment failed: {exc}"}
