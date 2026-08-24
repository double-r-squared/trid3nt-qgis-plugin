"""Engine template ``pelicun_hazus_seismic_dl_run`` - run a HAZUS earthquake
building damage-and-loss assessment by auto-populating the building type from AIM
attributes.

Drives pelicun's real ``DL_calculation`` pipeline (the CLI harness in
``_dl_calculation``): an AIM (building attributes -- structure type, height class,
design level, occupancy) plus a peak-ground-response demand time series are pushed
through auto-population (``--auto_script``), which selects the bundled HAZUS
earthquake fragility/consequence dataset, assigns the matching building-type
component, and runs the Monte-Carlo damage + repair-cost/-time assessment.

The default inputs are pelicun's checked-in DL Calculation Example 1 (e1): a
Pre-Code low-rise C1 lifeline building (EDU1 occupancy) under a PGA demand. The
building attributes are knobs -- overriding them re-drives auto-population to a
different HAZUS building type. A fixed seed makes the run reproducible.

The output is a repair-cost loss-exceedance CHART plus the deterministic
auto-populated component ID, the DL output-file manifest (checked against e1's
reference set), and the coupled-EDP demand reproduction. Every reported figure is
a pelicun DL_calculation output - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.pelicun._dl_calculation import (
    DLCalculationError,
    run_dl_calculation,
)
from trid3nt_server.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.workflows.pelicun._validation_common import (
    VEGA_LITE_V5_SCHEMA,
    emit_chart_if_live,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.pelicun.hazus_seismic_dl_run.hazus_seismic_dl_run"
)

__all__ = [
    "pelicun_hazus_seismic_dl_run",
    "build_seismic_aim",
    "build_loss_exceedance_chart_spec",
    "TEMPLATE_CARD",
]

#: e1's checked-in DL output manifest (its test asserts these 20 files exist;
#: the input AIM name maps to ``AIM.json`` / ``AIM_ap.json`` in this harness).
_REFERENCE_MANIFEST = frozenset({
    "AIM.json", "AIM_ap.json", "CMP_QNT.csv", "CMP_sample.json", "DEM_sample.json",
    "DL_summary.csv", "DL_summary.json", "DL_summary_stats.csv",
    "DL_summary_stats.json", "DMG_grp.json", "DMG_grp_stats.json",
    "DV_repair_agg.json", "DV_repair_agg_stats.json", "DV_repair_grp.json",
    "DV_repair_grp_stats.json", "DV_repair_sample.json", "DV_repair_stats.json",
    "pelicun_log.txt", "pelicun_log_warnings.txt", "response.csv",
})


TEMPLATE_CARD = TemplateCard(
    question=(
        "run a HAZUS earthquake building damage-and-loss assessment by "
        "auto-populating the building type from AIM attributes (structure type, "
        "height class, design level, occupancy) and a ground-response demand"
    ),
    required_inputs=[],
    knobs=(
        "structure_type, height_class, design_level, occupancy_class, "
        "number_of_stories, lifeline_facility, realizations, seed"
    ),
)

_METADATA = AtomicToolMetadata(
    name="pelicun_hazus_seismic_dl_run",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def _e1_demand_path() -> str:
    """Absolute path to pelicun's bundled e1 PGA demand (``response.csv``)."""
    try:
        import pelicun
    except ImportError as exc:
        raise DLCalculationError(
            "pelicun is not installed; cannot locate the bundled demand."
        ) from exc
    path = os.path.join(
        os.path.dirname(pelicun.__file__),
        "tests", "dl_calculation", "e1", "response.csv",
    )
    if not os.path.isfile(path):
        raise DLCalculationError(
            f"bundled pelicun e1 demand not found at {path!r}."
        )
    return path


def build_seismic_aim(
    *,
    structure_type: str,
    height_class: str,
    design_level: str,
    occupancy_class: str,
    number_of_stories: int,
    lifeline_facility: bool,
) -> dict[str, Any]:
    """Assemble a HAZUS-earthquake AIM config from building attributes. Pure.

    Mirrors pelicun's e1 example schema: ``GeneralInformation`` carries the
    building-type attributes the auto-population maps to a HAZUS component;
    ``Applications/DL`` selects the ``Hazus Earthquake - Buildings`` method.
    """
    return {
        "GeneralInformation": {
            "HeightClass": height_class,
            "DesignLevel": design_level,
            "NumberOfStories": int(number_of_stories),
            "YearBuilt": 1900,
            "StructureType": structure_type,
            "OccupancyClass": occupancy_class,
            "units": {"force": "kips", "length": "ft", "time": "sec"},
        },
        "assetType": "Buildings",
        "Applications": {
            "DL": {
                "ApplicationData": {
                    "DL_Method": "Hazus Earthquake - Buildings",
                    "ground_failure": False,
                    "lifeline_facility": bool(lifeline_facility),
                }
            }
        },
    }


def build_loss_exceedance_chart_spec(repair_cost_ratios: Any) -> dict[str, Any]:
    """Repair-cost loss-exceedance curve (P[loss ratio >= x] vs x). Pure.

    Emitted as a QUANTITATIVE-x line so the interpreter draws a clean numeric axis
    -- the loss ratios never become rotated category labels.
    """
    import numpy as np

    arr = np.asarray(repair_cost_ratios, dtype=float)
    arr = arr[np.isfinite(arr)]
    arr = np.sort(arr)
    n = arr.size
    rows: list[dict[str, Any]] = []
    if n:
        # complementary CDF: exceedance probability at each sorted loss ratio
        for i, x in enumerate(arr):
            rows.append({
                "loss_ratio": round(float(x), 5),
                "exceedance": round(float((n - i) / n), 5),
            })
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": "Repair-cost loss exceedance (HAZUS earthquake DL run)",
        "data": {"values": rows},
        "mark": {"type": "line", "interpolate": "step-after", "color": "#4c78a8"},
        "encoding": {
            "x": {"field": "loss_ratio", "type": "quantitative",
                  "title": "repair-cost loss ratio"},
            "y": {"field": "exceedance", "type": "quantitative",
                  "title": "exceedance probability",
                  "scale": {"domain": [0, 1]}},
            "tooltip": [
                {"field": "loss_ratio", "type": "quantitative", "format": ".4g"},
                {"field": "exceedance", "type": "quantitative", "format": ".3f"},
            ],
        },
    }


def _coupled_demand_delta(result: Any, demand_path: str) -> float | None:
    """Max abs delta between the realized coupled demand and the input demand.

    With ``coupled_edp`` the DEM sample is the input EDP series used directly, so
    this is ~0. Returns None if the shapes/keys do not line up (never raises).
    """
    import numpy as np
    import pandas as pd

    try:
        inp = pd.read_csv(demand_path, index_col=0)
        input_vals = np.sort(inp.iloc[:, 0].to_numpy(dtype=float))
        dem = result.demand_sample
        # DEM_sample.json: {col -> [values], "Units": {col -> unit}}; each EDP
        # column is a list of per-realization values.
        edp_cols = [k for k in dem if k != "Units"]
        best = None
        for col in edp_cols:
            raw = dem[col]
            seq = raw.values() if isinstance(raw, dict) else raw
            vals = np.array([float(v) for v in seq], dtype=float)
            if vals.size != input_vals.size:
                continue
            # min over columns picks the driven EDP (the constant ONE column has a
            # large delta vs the input series and is discarded by the minimum)
            d = float(np.max(np.abs(np.sort(vals) - input_vals)))
            best = d if best is None else min(best, d)
        return best
    except Exception:  # noqa: BLE001
        return None


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_hazus_seismic_dl_run(
    structure_type: str = "C1",
    height_class: str = "Low-Rise",
    design_level: str = "Pre-Code",
    occupancy_class: str = "EDU1",
    number_of_stories: int = 1,
    lifeline_facility: bool = True,
    realizations: int = 100,
    seed: int = 42,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Run a HAZUS earthquake building damage-and-loss assessment (auto-populated).

    Fidelity: pelicun's real DL_calculation pipeline on the bundled HAZUS v6.1
    earthquake building dataset, auto-populating the building type from AIM
    attributes. The demand is pelicun's checked-in e1 PGA response series; the
    building attributes are knobs. A reproducible (seeded) Monte-Carlo run -- a
    methodology/coverage anchor, not a site-specific estimate over a fetched hazard
    raster. Off-scope: real per-asset damage over a fetched hazard field + asset
    inventory -> pelicun_damage_assessment.

    Use this when: the user asks to run a HAZUS earthquake building loss
    assessment, to auto-populate/assign a HAZUS building type from structure/height/
    design-level attributes, or to see the repair-cost / collapse distribution for
    a building class under a ground-motion demand.

    Params:
        structure_type: HAZUS structure type (e.g. C1, W1, S1, URM). Default C1.
        height_class: "Low-Rise" | "Mid-Rise" | "High-Rise". Default Low-Rise.
        design_level: "Pre-Code" | "Low-Code" | "Moderate-Code" | "High-Code".
            Default Pre-Code.
        occupancy_class: HAZUS occupancy (e.g. EDU1, RES1, COM1). Default EDU1.
        number_of_stories: story count. Default 1.
        lifeline_facility: essential-facility flag (drives the lifeline fragility
            variant). Default True.
        realizations: Monte-Carlo sample size. Default 100.
        seed: Monte-Carlo seed (fixed -> reproducible run). Default 42.

    Returns:
        On success: ``{"status": "ok", "auto_populated_component",
        "component_database", "output_file_count", "manifest_matches_reference",
        "manifest_delta", "coupled_demand_max_abs_delta", "loss_summary": {mean/
        median/p90 repair_cost, collapse_probability, mean_repair_time},
        "seed", "realizations", "chart_emitted"}``. A repair-cost loss-exceedance
        chart is emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if int(realizations) <= 0:
        return {"status": "error", "error_code": "PELICUN_DL_CALCULATION_INVALID",
                "error_message": "realizations must be a positive integer."}
    try:
        demand_path = _e1_demand_path()
        aim = build_seismic_aim(
            structure_type=str(structure_type), height_class=str(height_class),
            design_level=str(design_level), occupancy_class=str(occupancy_class),
            number_of_stories=int(number_of_stories),
            lifeline_facility=bool(lifeline_facility),
        )
        result = await asyncio.to_thread(
            run_dl_calculation,
            aim_config=aim, demand_csv_path=demand_path,
            realizations=int(realizations), seed=int(seed), coupled_edp=True,
        )
    except DLCalculationError as exc:
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_hazus_seismic_dl_run failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_DL_CALCULATION_ERROR",
                "error_message": f"HAZUS seismic DL run failed: {exc}"}

    summary = result.dl_summary
    stats = result.dl_summary_stats
    cost_col = next((c for c in summary.columns if str(c).startswith("repair_cost")), None)
    coll_col = "collapse" if "collapse" in summary.columns else None
    time_col = next(
        (c for c in summary.columns if str(c).startswith("repair_time")), None)

    def _stat(col: str, row: str) -> float | None:
        try:
            return float(stats.loc[row, col])
        except Exception:  # noqa: BLE001
            return None

    loss_summary = {
        "mean_repair_cost_ratio": _stat(cost_col, "mean") if cost_col else None,
        "median_repair_cost_ratio": _stat(cost_col, "50%") if cost_col else None,
        "p90_repair_cost_ratio": _stat(cost_col, "90%") if cost_col else None,
        "collapse_probability": _stat(coll_col, "mean") if coll_col else None,
        "mean_repair_time": _stat(time_col, "mean") if time_col else None,
    }

    manifest = set(result.output_files)
    manifest_matches = manifest == set(_REFERENCE_MANIFEST)
    manifest_delta = {
        "missing_vs_reference": sorted(set(_REFERENCE_MANIFEST) - manifest),
        "extra_vs_reference": sorted(manifest - set(_REFERENCE_MANIFEST)),
    }
    coupled_delta = _coupled_demand_delta(result, demand_path)

    spec = build_loss_exceedance_chart_spec(
        summary[cost_col].to_numpy() if cost_col else [])
    emitted = await emit_chart_if_live(
        spec, title="Repair-cost loss exceedance (HAZUS earthquake DL run)",
        caption="Monte-Carlo repair-cost loss-exceedance curve from pelicun's "
        "DL_calculation auto-populated HAZUS earthquake building run.")

    logger.info(
        "pelicun_hazus_seismic_dl_run component=%s manifest_ok=%s mean_cost=%s coll=%s",
        result.component_assignment, manifest_matches,
        loss_summary["mean_repair_cost_ratio"], loss_summary["collapse_probability"])

    db = result.auto_populated_config.get("DL", {}).get("Asset", {}).get(
        "ComponentDatabase")
    return {
        "status": "ok",
        "auto_populated_component": result.component_assignment,
        "component_database": db,
        "output_file_count": len(result.output_files),
        "manifest_matches_reference": bool(manifest_matches),
        "manifest_delta": manifest_delta,
        "coupled_demand_max_abs_delta": coupled_delta,
        "loss_summary": loss_summary,
        "seed": int(seed),
        "realizations": int(realizations),
        "chart_emitted": emitted,
    }
