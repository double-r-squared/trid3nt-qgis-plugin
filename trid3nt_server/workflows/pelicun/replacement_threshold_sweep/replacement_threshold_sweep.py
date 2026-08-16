"""Engine template ``pelicun_replacement_threshold_override_sweep`` - at what
residual-drift threshold does the irreparable-damage override switch a building's
loss from summed repairs to full replacement, and how does moving that threshold
bend the portfolio loss curve?

A drift demand ``PID`` drives a residual inter-story drift ``RID`` - either
inferred from PID via pelicun's FEMA P-58 ``estimate_RID`` model (no separate RID
demand file) or held at a fixed value. An ``excessiveRID`` fragility whose median
capacity is the swept threshold triggers, through the damage process, an
``irreparable`` state that the loss map redirects to full ``replacement`` (loss
ratio 1.0); a ``collapse`` branch also maps to replacement. Sweeping the threshold
across a ladder produces the fraction-replaced (== mean loss ratio) portfolio
curve.

Idealized, domain-free: the demand, fragility, and consequence models are built
in memory - no fetched hazard raster or asset inventory. The output is the
loss-vs-threshold sweep CHART, not a map. Every narrated number reads from the
typed summary this tool returns.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.workflows.pelicun._validation_common import (
    PelicunValidationError,
    emit_chart_if_live,
    multi_series_line_spec,
    replacement_threshold_point,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.pelicun.replacement_threshold_sweep."
    "replacement_threshold_sweep"
)

__all__ = [
    "pelicun_replacement_threshold_override_sweep",
    "build_threshold_sweep_chart_spec",
    "TEMPLATE_CARD",
]


TEMPLATE_CARD = TemplateCard(
    question=(
        "at what residual-drift threshold does the irreparable-damage override "
        "switch a building to full replacement, and how does sweeping that "
        "threshold change the portfolio loss curve (with RID inferred from PID)"
    ),
    required_inputs=[],
    knobs=(
        "rid_threshold_min, rid_threshold_max, n_threshold_steps, rid_source "
        "(inferred | fixed), fixed_rid, sample_size"
    ),
)

_METADATA = AtomicToolMetadata(
    name="pelicun_replacement_threshold_override_sweep",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def build_threshold_sweep_chart_spec(sweep: list[dict[str, float]]) -> dict[str, Any]:
    """Fraction-replaced and mean-loss vs RID threshold, two series. Pure."""
    rows: list[dict[str, Any]] = []
    for p in sweep:
        rows.append({"rid_threshold": float(p["rid_threshold"]),
                     "value": float(p["frac_replaced"]), "metric": "fraction_replaced"})
        rows.append({"rid_threshold": float(p["rid_threshold"]),
                     "value": float(p["mean_loss"]), "metric": "mean_loss_ratio"})
    return multi_series_line_spec(
        rows, x_field="rid_threshold", y_field="value", color_field="metric",
        x_title="excessiveRID capacity median (rad)",
        y_title="portfolio value (loss ratio)",
        title="Irreparable-override sweep: portfolio loss vs RID threshold",
    )


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_replacement_threshold_override_sweep(
    rid_threshold_min: float = 0.006,
    rid_threshold_max: float = 0.020,
    n_threshold_steps: int = 5,
    rid_source: Literal["inferred", "fixed"] = "inferred",
    fixed_rid: float = 0.006,
    sample_size: int = 10000,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sweep the irreparable-override RID threshold and trace the portfolio loss curve.

    Fidelity: an IDEALIZED domain-free pelicun Assessment (synthetic demand,
    fragility, and consequence models; no fetched hazard/assets). Answers "where
    does the residual-drift irreparable override cut over to full replacement, and
    how does moving that threshold reshape the portfolio loss" -- a methodology /
    sensitivity demonstration, not a site loss estimate. With ``rid_source=
    "inferred"`` the residual drift is derived from PID via pelicun's FEMA P-58
    RID model, so no separate RID demand file is needed. Off-scope: real per-asset
    damage over a hazard raster -> pelicun_damage_assessment.

    Use this when: the user asks about the irreparable-damage / replacement-cost
    override threshold, residual-drift-triggered replacement, RID inferred from
    PID, or how the replacement cutover bends the portfolio loss curve.

    Params:
        rid_threshold_min, rid_threshold_max: ends of the swept excessiveRID
            capacity-median ladder (radians; default 0.006 .. 0.020).
        n_threshold_steps: number of thresholds swept (default 5; each is one
            pelicun run).
        rid_source: ``inferred`` (RID from PID via estimate_RID) or ``fixed``.
        fixed_rid: the residual drift used when ``rid_source="fixed"`` (default
            0.006).
        sample_size: Monte-Carlo realizations per threshold (default 10000).

    Returns:
        On success: ``{"status": "ok", "rid_source", "sweep": [{"rid_threshold",
        "frac_replaced", "mean_loss"}...], "summary": {"min_threshold",
        "max_threshold", "frac_replaced_at_min", "frac_replaced_at_max"},
        "chart_emitted"}``. A loss-vs-threshold sweep chart is emitted when a live
        emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    n = int(n_threshold_steps)
    if n < 1:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "n_threshold_steps must be >= 1."}
    if int(sample_size) <= 0:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "sample_size must be a positive integer."}
    lo, hi = float(rid_threshold_min), float(rid_threshold_max)
    thresholds = [hi] if n == 1 else [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    try:
        sweep: list[dict[str, float]] = []
        for th in thresholds:
            r = await asyncio.to_thread(
                replacement_threshold_point,
                rid_threshold=float(th), sample_size=int(sample_size),
                rid_source=str(rid_source), fixed_rid=float(fixed_rid),
            )
            sweep.append({"rid_threshold": float(th),
                          "frac_replaced": float(r["frac_replaced"]),
                          "mean_loss": float(r["mean_loss"])})
        spec = build_threshold_sweep_chart_spec(sweep)
        emitted = await emit_chart_if_live(
            spec, title="Irreparable-override sweep: portfolio loss vs RID threshold",
            caption="Fraction of realizations pushed to full replacement (and the "
            "mean portfolio loss ratio) as the excessiveRID capacity threshold is "
            "swept.")
        logger.info(
            "pelicun_replacement_threshold_override_sweep rid_source=%s min=%.4g->%.3f max=%.4g->%.3f",
            rid_source, thresholds[0], sweep[0]["frac_replaced"],
            thresholds[-1], sweep[-1]["frac_replaced"])
        return {
            "status": "ok", "rid_source": str(rid_source), "sweep": sweep,
            "summary": {
                "min_threshold": float(thresholds[0]),
                "max_threshold": float(thresholds[-1]),
                "frac_replaced_at_min": float(sweep[0]["frac_replaced"]),
                "frac_replaced_at_max": float(sweep[-1]["frac_replaced"]),
            },
            "sample_size": int(sample_size), "chart_emitted": emitted,
        }
    except asyncio.CancelledError:
        raise
    except PelicunValidationError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_replacement_threshold_override_sweep failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_VALIDATION_ERROR",
                "error_message": f"replacement-threshold sweep failed: {exc}"}
