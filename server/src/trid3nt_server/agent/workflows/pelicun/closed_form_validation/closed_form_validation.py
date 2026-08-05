"""Engine template ``pelicun_closed_form_validation`` - does pelicun's Monte-Carlo
match the analytic closed form?

Two self-consistency validation checks on pelicun's real ``assessment.Assessment``
pipeline, selected by the ``check`` knob:

- ``damage_state_probability``: a single component with two sequential lognormal
  capacity limit states under a lognormal drift demand. pelicun's Monte-Carlo
  damage-state probabilities are compared against the analytic lognormal
  closed form ``P(D > C_i) = 1 - Phi((ln m_D - ln m_Ci)/sqrt(beta_D^2+beta_C^2))``.
- ``loss_function_identity``: a single loss-function component with a 1:1 curve
  (loss = EDP). The realized loss distribution must reproduce the input lognormal
  EDP (median + log-dispersion) exactly.

Idealized, domain-free: no fetched hazard raster or asset inventory - the demand,
fragility, and loss models are built in memory. The output is a comparison CHART
(analytic vs Monte-Carlo), not a map. The narrated deltas come from the typed
summary this tool returns - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.agent.workflows.pelicun._validation_common import (
    PelicunValidationError,
    VEGA_LITE_V5_SCHEMA,
    ds_probability_check,
    emit_chart_if_live,
    grouped_bar_spec,
    loss_function_identity_check,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.pelicun.closed_form_validation.closed_form_validation"
)

__all__ = [
    "pelicun_closed_form_validation",
    "build_ds_probability_chart_spec",
    "build_loss_identity_chart_spec",
    "TEMPLATE_CARD",
]

_DS_TOLERANCE = 0.01
_IDENTITY_TOLERANCE = 0.02


TEMPLATE_CARD = TemplateCard(
    question=(
        "does pelicun's Monte-Carlo sampling match the analytic closed form -- "
        "damage-state probabilities for a two-limit-state component, or a 1:1 "
        "loss function reproducing the input EDP distribution"
    ),
    required_inputs=[],
    knobs=(
        "check (damage_state_probability | loss_function_identity), "
        "demand_median, demand_beta, capacity_1_median, capacity_2_median, "
        "capacity_beta, edp_median, edp_beta, sample_size"
    ),
)

_METADATA = AtomicToolMetadata(
    name="pelicun_closed_form_validation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def build_ds_probability_chart_spec(
    p_analytic: list[float], p_montecarlo: list[float]
) -> dict[str, Any]:
    """Grouped-bar spec: analytic vs Monte-Carlo probability per damage state. Pure."""
    labels = ["DS0 none", "DS1 slight", "DS2 complete"]
    rows: list[dict[str, Any]] = []
    for i, lab in enumerate(labels):
        rows.append({"ds": lab, "source": "analytic", "probability": float(p_analytic[i])})
        rows.append({"ds": lab, "source": "monte_carlo", "probability": float(p_montecarlo[i])})
    return grouped_bar_spec(
        rows,
        x_field="ds", y_field="probability", color_field="source",
        x_title="damage state", y_title="probability",
        title="Damage-state probability: analytic vs Monte-Carlo",
    )


def build_loss_identity_chart_spec(
    samples: Any, target_median: float
) -> dict[str, Any]:
    """Loss-sample histogram with a rule at the target median. Pure."""
    import numpy as np

    arr = np.asarray(samples, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    hist, edges = np.histogram(arr, bins=40)
    centers = 0.5 * (edges[:-1] + edges[1:])
    values = [{"loss": float(c), "count": int(n)} for c, n in zip(centers, hist)]
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": "Loss distribution reproduces the input EDP (1:1 loss function)",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "bar", "color": "#4c78a8"},
                "encoding": {
                    "x": {"field": "loss", "type": "quantitative", "title": "loss ratio"},
                    "y": {"field": "count", "type": "quantitative", "title": "count"},
                },
            },
            {
                "data": {"values": [{"m": float(target_median)}]},
                "mark": {"type": "rule", "color": "#d1495b", "strokeDash": [4, 4]},
                "encoding": {"x": {"field": "m", "type": "quantitative"}},
            },
        ],
    }


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_closed_form_validation(
    check: Literal["damage_state_probability", "loss_function_identity"] = "damage_state_probability",
    demand_median: float = 0.015,
    demand_beta: float = 0.60,
    capacity_1_median: float = 0.015,
    capacity_2_median: float = 0.02,
    capacity_beta: float = 0.50,
    edp_median: float = 0.50,
    edp_beta: float = 0.90,
    sample_size: int = 200000,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Validate pelicun Monte-Carlo output against the analytic closed form.

    Fidelity: an IDEALIZED domain-free self-consistency check on pelicun's real
    Assessment pipeline (no fetched hazard/assets; the demand, fragility, and
    loss models are synthetic). Answers "is pelicun's sampling correct" -- a
    calibration/verification anchor, not a site loss estimate. Off-scope: real
    per-asset damage over a hazard raster -> pelicun_damage_assessment.

    Use this when: the user asks whether pelicun's damage-state sampling matches
    the analytic lognormal probability, or whether a 1:1 loss function reproduces
    the input EDP distribution -- a closed-form validation / sanity check.

    Params:
        check: ``damage_state_probability`` (Monte-Carlo DS probabilities for a
            two-sequential-limit-state component vs the analytic lognormal form)
            or ``loss_function_identity`` (a 1:1 loss function reproduces the
            input EDP distribution).
        demand_median, demand_beta: lognormal drift-demand median / dispersion
            (damage_state_probability mode).
        capacity_1_median, capacity_2_median, capacity_beta: the two sequential
            limit-state capacity medians and shared dispersion.
        edp_median, edp_beta: lognormal EDP median / dispersion
            (loss_function_identity mode).
        sample_size: Monte-Carlo realizations (default 200000 -- large enough that
            the sampling error is well below the pass tolerance).

    Returns:
        On success: ``{"status": "ok", "check", ...}``. For
        ``damage_state_probability``: ``p_analytic`` / ``p_montecarlo`` (per-DS)
        + ``max_abs_delta`` + ``tolerance`` + ``passed``. For
        ``loss_function_identity``: ``loss_median`` / ``loss_log_std`` vs
        ``target_median`` / ``target_log_std`` + deltas + ``passed``. A comparison
        chart is emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if int(sample_size) <= 0:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "sample_size must be a positive integer."}
    try:
        if check == "damage_state_probability":
            r = await asyncio.to_thread(
                ds_probability_check,
                demand_median=float(demand_median), demand_beta=float(demand_beta),
                capacity_1_median=float(capacity_1_median),
                capacity_2_median=float(capacity_2_median),
                capacity_beta=float(capacity_beta), sample_size=int(sample_size),
            )
            spec = build_ds_probability_chart_spec(r["p_analytic"], r["p_montecarlo"])
            emitted = await emit_chart_if_live(
                spec, title="Damage-state probability: analytic vs Monte-Carlo",
                caption="pelicun Monte-Carlo damage-state probabilities against the "
                "analytic lognormal closed form.")
            passed = r["max_abs_delta"] <= _DS_TOLERANCE
            logger.info("pelicun_closed_form_validation DS max_abs_delta=%.5f passed=%s",
                        r["max_abs_delta"], passed)
            return {
                "status": "ok", "check": check,
                "p_analytic": [float(x) for x in r["p_analytic"]],
                "p_montecarlo": [float(x) for x in r["p_montecarlo"]],
                "max_abs_delta": float(r["max_abs_delta"]),
                "tolerance": _DS_TOLERANCE, "passed": bool(passed),
                "sample_size": int(sample_size), "chart_emitted": emitted,
            }
        if check == "loss_function_identity":
            r = await asyncio.to_thread(
                loss_function_identity_check,
                edp_median=float(edp_median), edp_beta=float(edp_beta),
                sample_size=int(sample_size),
            )
            spec = build_loss_identity_chart_spec(r["samples"], r["edp_median"])
            emitted = await emit_chart_if_live(
                spec, title="Loss distribution reproduces the input EDP",
                caption="A 1:1 loss function's realized loss distribution against "
                "the input lognormal EDP.")
            d_med = abs(r["loss_median"] - r["edp_median"])
            d_std = abs(r["loss_log_std"] - r["edp_beta"])
            passed = d_med <= _IDENTITY_TOLERANCE and d_std <= _IDENTITY_TOLERANCE
            logger.info("pelicun_closed_form_validation LF d_med=%.4f d_std=%.4f passed=%s",
                        d_med, d_std, passed)
            return {
                "status": "ok", "check": check,
                "loss_median": float(r["loss_median"]),
                "loss_log_std": float(r["loss_log_std"]),
                "target_median": float(r["edp_median"]),
                "target_log_std": float(r["edp_beta"]),
                "delta_median": float(d_med), "delta_log_std": float(d_std),
                "tolerance": _IDENTITY_TOLERANCE, "passed": bool(passed),
                "sample_size": int(sample_size), "chart_emitted": emitted,
            }
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": f"check must be 'damage_state_probability' or "
                f"'loss_function_identity'; got {check!r}."}
    except asyncio.CancelledError:
        raise
    except PelicunValidationError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_closed_form_validation failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_VALIDATION_ERROR",
                "error_message": f"closed-form validation failed: {exc}"}
