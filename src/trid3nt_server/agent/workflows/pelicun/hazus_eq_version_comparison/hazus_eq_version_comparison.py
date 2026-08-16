"""Engine template ``pelicun_hazus_eq_version_comparison`` - how do the HAZUS
earthquake building damage/loss models differ between dataset versions v5.1 and
v6.1?

pelicun's resource map exposes only a v6.1 alias for earthquake buildings ("Hazus
Earthquake - Buildings"); the v5.1 portfolio ships on disk but has no alias. This
template loads the v5.1 and v6.1 fragility + consequence CSVs BY PATH and runs the
same building type through pelicun's real ``assessment.Assessment`` damage + loss
pipeline under an identical synthetic demand and seed, then reports the shift in
damage-state probabilities and mean repair cost between the two dataset versions.

It also reports the coverage delta: which components v6.1 adds over v5.1 (the
concrete change between the two releases for the shared building portfolio).

Idealized, domain-free: an in-memory lognormal demand on the fragility's own EDP
type, one building-type component, no fetched hazard/inventory. The output is a
damage-state-probability comparison CHART (v5.1 vs v6.1). Every plotted value is a
pelicun Assessment output - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.agent.workflows.pelicun._validation_common import (
    PelicunValidationError,
    emit_chart_if_live,
    grouped_bar_spec,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.pelicun.hazus_eq_version_comparison."
    "hazus_eq_version_comparison"
)

__all__ = [
    "pelicun_hazus_eq_version_comparison",
    "single_component_version_assessment",
    "coverage_delta",
    "build_ds_shift_chart_spec",
    "TEMPLATE_CARD",
]

_VERSIONS = ("v5.1", "v6.1")


TEMPLATE_CARD = TemplateCard(
    question=(
        "how the HAZUS earthquake building damage-state probabilities and mean "
        "repair cost shift between dataset versions v5.1 and v6.1 for one building "
        "type under the same demand"
    ),
    required_inputs=[],
    knobs=(
        "building_type (fragility ID), occupancy (consequence base), "
        "demand_median, demand_beta, sample_size, seed"
    ),
)

_METADATA = AtomicToolMetadata(
    name="pelicun_hazus_eq_version_comparison",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def _portfolio_dir(version: str) -> str:
    """Absolute path to a bundled HAZUS seismic building portfolio version dir."""
    try:
        import pelicun
    except ImportError as exc:
        raise PelicunValidationError(
            "pelicun is not installed; cannot load HAZUS earthquake datasets."
        ) from exc
    path = os.path.join(
        os.path.dirname(pelicun.__file__), "resources", "DamageAndLossModelLibrary",
        "seismic", "building", "portfolio", f"Hazus {version}",
    )
    if not os.path.isdir(path):
        raise PelicunValidationError(
            f"HAZUS seismic building portfolio {version!r} not found at {path!r}.")
    return path


def single_component_version_assessment(
    *,
    version: str,
    fragility_id: str,
    consequence_base: str,
    demand_median: float,
    demand_beta: float,
    sample_size: int,
    seed: int,
) -> dict[str, Any]:
    """Run one HAZUS building type through damage + loss for one dataset version.

    Loads the ``fragility_id`` row from ``Hazus {version}/fragility.csv`` and the
    ``{consequence_base}-Cost`` row from ``consequence_repair.csv`` (both by path),
    drives a synthetic lognormal demand on the fragility's own EDP type, and runs
    pelicun's damage + loss pipeline. Returns per-damage-state probabilities and
    the mean aggregate repair cost. Raises ``PelicunValidationError`` (loud, typed)
    if the building type is absent in this version.
    """
    import numpy as np
    import pandas as pd
    from pelicun import assessment, file_io
    from pelicun.base import EDP_to_demand_type

    fdir = _portfolio_dir(version)
    frag = pd.read_csv(os.path.join(fdir, "fragility.csv")).set_index("ID")
    cons = pd.read_csv(os.path.join(fdir, "consequence_repair.csv")).set_index("ID")
    if fragility_id not in frag.index:
        raise PelicunValidationError(
            f"building type {fragility_id!r} is absent in HAZUS {version} "
            "(introduced in a later release; no equivalent in this version).")
    cons_id = f"{consequence_base}-Cost"
    if cons_id not in cons.index:
        raise PelicunValidationError(
            f"consequence {cons_id!r} is absent in HAZUS {version}.")

    frow = frag.loc[[fragility_id]].reset_index()
    demand_full = str(frow["Demand-Type"].iloc[0])
    demand_type = EDP_to_demand_type.get(demand_full, demand_full)
    demand_unit = str(frow["Demand-Unit"].iloc[0])

    a = assessment.Assessment({"PrintLog": False, "Seed": int(seed)})
    dem = pd.DataFrame(
        {"Theta_0": [demand_median], "Theta_1": [demand_beta],
         "Family": ["lognormal"], "Units": [demand_unit]},
        index=pd.MultiIndex.from_tuples([(demand_type, "1", "1")]),
    )
    a.demand.load_model({"marginals": dem})
    a.demand.generate_sample({"SampleSize": int(sample_size)})
    a.stories = 1
    cmp = pd.DataFrame(
        {"Units": ["ea"], "Location": [1], "Direction": [1], "Theta_0": [1],
         "Blocks": [1]},
        index=[fragility_id],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(int(sample_size))

    def _load_by_path(row_df: pd.DataFrame) -> Any:
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="trid3nt_hazus_")
        os.close(fd)
        try:
            row_df.set_index("ID").to_csv(path)
            return file_io.load_data(
                path, reindex=False,
                unit_conversion_factors=a.unit_conversion_factors)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    dmg_db = _load_by_path(frow)
    a.damage.load_model_parameters(
        [dmg_db], set(a.asset.list_unique_component_ids()))
    a.damage.calculate()
    probs = a.damage.ds_model.probabilities()
    ds_probs = [float(probs.iloc[0, i]) for i in range(probs.shape[1])]

    crow = cons.loc[[cons_id]].reset_index()
    cons_db = _load_by_path(crow)
    a.loss.decision_variables = ("Cost",)
    a.loss.add_loss_map(
        pd.DataFrame([consequence_base], columns=["Repair"], index=[fragility_id]),
        loss_map_policy="fill")
    a.loss.load_model_parameters([cons_db])
    a.loss.calculate()
    agg, _ = a.loss.aggregate_losses(future=True)
    v = agg["repair_cost"].to_numpy()
    v = v[np.isfinite(v)]
    return {
        "version": version, "ds_probs": ds_probs,
        "mean_repair_cost": float(v.mean()) if v.size else float("nan"),
        "demand_type": demand_type,
    }


def coverage_delta() -> dict[str, Any]:
    """Which building-type components v6.1 adds over v5.1 (fragility ID set diff)."""
    import pandas as pd

    v5 = set(pd.read_csv(
        os.path.join(_portfolio_dir("v5.1"), "fragility.csv"))["ID"].astype(str))
    v6 = set(pd.read_csv(
        os.path.join(_portfolio_dir("v6.1"), "fragility.csv"))["ID"].astype(str))
    only_v6 = sorted(v6 - v5)
    only_v5 = sorted(v5 - v6)
    return {
        "v5_component_count": len(v5), "v6_component_count": len(v6),
        "added_in_v6_count": len(only_v6), "removed_in_v6_count": len(only_v5),
        "added_in_v6_examples": only_v6[:8],
    }


def build_ds_shift_chart_spec(
    ds_probs_v5: list[float], ds_probs_v6: list[float]
) -> dict[str, Any]:
    """Grouped-bar spec: damage-state probability, v5.1 vs v6.1. Pure."""
    n = max(len(ds_probs_v5), len(ds_probs_v6))
    rows: list[dict[str, Any]] = []
    for i in range(n):
        ds = f"DS{i}"
        if i < len(ds_probs_v5):
            rows.append({"ds": ds, "version": "Hazus v5.1",
                         "probability": float(ds_probs_v5[i])})
        if i < len(ds_probs_v6):
            rows.append({"ds": ds, "version": "Hazus v6.1",
                         "probability": float(ds_probs_v6[i])})
    return grouped_bar_spec(
        rows, x_field="ds", y_field="probability", color_field="version",
        x_title="damage state", y_title="probability",
        title="HAZUS earthquake damage-state probability: v5.1 vs v6.1")


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_hazus_eq_version_comparison(
    building_type: str = "STR.C1.L.PC",
    occupancy: str = "STR.RES1",
    demand_median: float = 0.02,
    demand_beta: float = 0.5,
    sample_size: int = 20000,
    seed: int = 42,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Compare HAZUS earthquake building damage/loss between dataset versions v5.1 and v6.1.

    Fidelity: loads pelicun's bundled HAZUS v5.1 and v6.1 earthquake building
    fragility + consequence tables BY PATH (v5.1 has no resource alias) and runs
    the same building type through pelicun's real damage + loss pipeline under an
    identical synthetic lognormal demand and seed. Answers "how much does the
    dataset version change the damage-state probabilities and mean repair cost" -- a
    data-version sensitivity comparison, not a site estimate. Off-scope: real
    per-asset damage over a fetched hazard raster -> pelicun_damage_assessment.

    Use this when: the user asks how HAZUS earthquake v6.1 differs from v5.1, which
    HAZUS dataset version to use, or how the building fragility/repair-cost model
    changed between releases.

    Params:
        building_type: HAZUS fragility component ID (e.g. "STR.C1.L.PC" -- concrete
            moment-frame, low-rise, pre-code). A v6.1-only type raises a typed error
            for the v5.1 branch.
        occupancy: consequence base ID (e.g. "STR.RES1") whose ``-Cost`` row maps
            damage states to repair-cost ratio.
        demand_median, demand_beta: lognormal demand median / dispersion on the
            fragility's own EDP type.
        sample_size: Monte-Carlo realizations. Default 20000.
        seed: Monte-Carlo seed (identical across both versions -> a pure dataset
            comparison). Default 42.

    Returns:
        On success: ``{"status": "ok", "building_type", "occupancy",
        "ds_probs": {v5.1, v6.1}, "max_ds_probability_shift",
        "mean_repair_cost": {v5.1, v6.1}, "mean_repair_cost_shift",
        "coverage": {v5/v6 counts + added-in-v6}, "chart_emitted"}``. A damage-state
        comparison chart is emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if int(sample_size) <= 0:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "sample_size must be a positive integer."}
    try:
        results = {}
        for version in _VERSIONS:
            results[version] = await asyncio.to_thread(
                single_component_version_assessment,
                version=version, fragility_id=str(building_type),
                consequence_base=str(occupancy),
                demand_median=float(demand_median), demand_beta=float(demand_beta),
                sample_size=int(sample_size), seed=int(seed),
            )
        cov = await asyncio.to_thread(coverage_delta)
    except PelicunValidationError as exc:
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_hazus_eq_version_comparison failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_VALIDATION_ERROR",
                "error_message": f"HAZUS version comparison failed: {exc}"}

    p5 = results["v5.1"]["ds_probs"]
    p6 = results["v6.1"]["ds_probs"]
    k = min(len(p5), len(p6))
    max_ds_shift = max((abs(p5[i] - p6[i]) for i in range(k)), default=0.0)
    c5 = results["v5.1"]["mean_repair_cost"]
    c6 = results["v6.1"]["mean_repair_cost"]

    spec = build_ds_shift_chart_spec(p5, p6)
    emitted = await emit_chart_if_live(
        spec, title="HAZUS earthquake damage-state probability: v5.1 vs v6.1",
        caption=(
            f"Damage-state probabilities for {building_type} under the same demand; "
            f"mean-repair-cost shift {abs(c6 - c5):.4g} (v5.1 {c5:.4g} -> v6.1 "
            f"{c6:.4g}); v6.1 adds {cov['added_in_v6_count']} components over v5.1."))

    logger.info(
        "pelicun_hazus_eq_version_comparison %s max_ds_shift=%.5f cost_shift=%.5f "
        "added_v6=%d", building_type, max_ds_shift, abs(c6 - c5),
        cov["added_in_v6_count"])
    return {
        "status": "ok", "building_type": str(building_type),
        "occupancy": str(occupancy),
        "ds_probs": {"v5.1": p5, "v6.1": p6},
        "max_ds_probability_shift": float(max_ds_shift),
        "mean_repair_cost": {"v5.1": float(c5), "v6.1": float(c6)},
        "mean_repair_cost_shift": float(abs(c6 - c5)),
        "coverage": cov,
        "chart_emitted": emitted,
    }
