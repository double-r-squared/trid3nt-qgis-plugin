"""Engine template ``pelicun_hazus_lifeline_seismic_dl_run`` - run a HAZUS
earthquake damage-and-loss assessment for one LIFELINE NETWORK asset class
(transportation bridges, potable-water pipes, electric-power substations) by
auto-populating the HAZUS component from AIM attributes.

Drives pelicun's real ``DL_calculation`` pipeline (the shared ``_dl_calculation``
harness) exactly as the building template ``pelicun_hazus_seismic_dl_run`` does,
but over the three bundled DamageAndLossModelLibrary lifeline fragility datasets:

- ``transportation`` -> ``Hazus Earthquake - Transportation`` (Hazus v5.1); a
  HwyBridge AIM (BridgeClass/StateCode/YearBuilt/NumOfSpans/MaxSpanLength/Skew/
  DeckWidth/StructureLength) is classified into an HWB bridge type; SA(1.0)
  ground shaking + optional PGD ground failure drive a full repair-cost /
  repair-time loss summary.
- ``potable_water`` -> ``Hazus Earthquake - Potable Water`` (Hazus v6.1); a buried
  Pipe AIM (Diam/Len/material) is segmented into 20 ft reaches; PGV ground shaking
  + optional PGD ground failure drive a repair-rate (leaks + breaks) count. Hazus
  potable-water fragility carries NO repair-cost consequence, so the product is a
  damage-state / expected-repair distribution, not a cost.
- ``electric_power`` -> ``Hazus Earthquake - Electric Power`` (Hazus v5.1); a
  Substation AIM (Voltage/Anchored) is classified into an EP.S component; PGA
  drives a damage-state distribution (functional / slight / moderate / extensive /
  complete). No repair-cost consequence either -> a damage-state distribution.

The single ``lifeline_class`` knob selects the DL_Method, the assetType, and the
per-class AIM builder. Every asset attribute and every ground-motion intensity is
a labeled knob -- this is a methodology / coverage anchor that evaluates the HAZUS
lifeline fragilities at a stated shaking level (mirroring the building template's
use of a fixed bundled demand), NOT a per-asset assessment over a fetched hazard
field and a fetched inventory. Off-scope (documented in the return): the National
Bridge Inventory attributes a per-city HwyBridge fetch needs, and buried
water-main geometry (neither is carried by our OSM/HIFLD fetchers), plus the
OpenQuake scenario-GMF wiring for a live per-asset demand.

Every reported figure is a pelicun DL_calculation output - never free-generated
(Invariant 1). A fixed seed makes each run reproducible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.pelicun._dl_calculation import (
    DLCalculationError,
    run_dl_calculation,
)
from trid3nt_server.agent.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.agent.workflows.pelicun._validation_common import (
    VEGA_LITE_V5_SCHEMA,
    emit_chart_if_live,
)
from trid3nt_server.agent.workflows.pelicun.hazus_seismic_dl_run.hazus_seismic_dl_run import (
    build_loss_exceedance_chart_spec,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.pelicun.hazus_lifeline_seismic_dl_run"
    ".hazus_lifeline_seismic_dl_run"
)

__all__ = [
    "pelicun_hazus_lifeline_seismic_dl_run",
    "build_lifeline_aim",
    "build_lifeline_demand_csv",
    "build_damage_state_chart_spec",
    "TEMPLATE_CARD",
]

#: DL_Method + assetType + demand IMTs per lifeline class. ``imts`` maps a demand
#: column tag to the intensity-knob name that supplies its constant level. The
#: ground-failure PGD column is appended only when ``ground_failure`` is on.
_CLASS_SPEC: dict[str, dict[str, Any]] = {
    "transportation": {
        "dl_method": "Hazus Earthquake - Transportation",
        "asset_type": "TransportationNetwork",
        "asset_subtype": "HwyBridge",
        # SA_0.3 feeds the HAZUS bridge slight-damage spectral-shape modifier for
        # the bridge classes that define one; SA_1.0 is the primary structural EDP.
        "imts": {"SA_1.0": "sa_1_0_g", "SA_0.3": "sa_0_3_g", "PGA": "pga_g"},
        "gf_imt": ("PGD", "pgd_inch"),
        "has_repair_cost": True,
        "primary_component_prefix": "HWB.GS",
    },
    "potable_water": {
        "dl_method": "Hazus Earthquake - Potable Water",
        "asset_type": "WaterDistributionNetwork",
        "asset_subtype": "Pipe",
        # The HAZUS potable-water auto-pop ALWAYS builds the ground-failure (PGD)
        # pipe component + its damage process, independent of the ground_failure
        # flag, so PGV (ground shaking) AND PGD (ground failure) are both mandatory.
        "imts": {"PGV": "pgv_cmps", "PGD": "pgd_inch"},
        "gf_imt": None,
        "has_repair_cost": False,
        "primary_component_prefix": "aggregate",
    },
    "electric_power": {
        "dl_method": "Hazus Earthquake - Electric Power",
        "asset_type": "PowerNetwork",
        "asset_subtype": "Substation",
        "imts": {"PGA": "pga_g"},
        "gf_imt": None,  # HAZUS electric-power fragility is PGA-only (no ground failure)
        "has_repair_cost": False,
        "primary_component_prefix": "EP.S",
    },
}

#: Human-readable HAZUS damage-state ladder (index -> label). Bridges + substations
#: use the standard structural ladder; potable-water pipes use the HAZUS pipe
#: leak/break repair taxonomy (DS1 = leak, DS2 = break) per segment.
_DS_LABELS_STRUCTURAL = {
    0: "None", 1: "Slight", 2: "Moderate", 3: "Extensive", 4: "Complete"}
_DS_LABELS_PIPE = {0: "None", 1: "Leak", 2: "Break"}


def _ds_labels(lifeline_class: str) -> dict[int, str]:
    """Damage-state label map for the class (pipe leak/break vs structural ladder)."""
    return (_DS_LABELS_PIPE if lifeline_class == "potable_water"
            else _DS_LABELS_STRUCTURAL)


TEMPLATE_CARD = TemplateCard(
    question=(
        "run a HAZUS earthquake damage-and-loss assessment for a lifeline network "
        "asset class (transportation bridge, potable-water pipe, or electric-power "
        "substation) by auto-populating the HAZUS component from AIM attributes and "
        "a ground-motion demand"
    ),
    required_inputs=[],
    knobs=(
        "lifeline_class {transportation|potable_water|electric_power}, per-class "
        "asset attributes, ground-motion intensities (sa_1_0_g/sa_0_3_g/pga_g/"
        "pgv_cmps/pgd_inch), ground_failure, realizations, seed"
    ),
)

_METADATA = AtomicToolMetadata(
    name="pelicun_hazus_lifeline_seismic_dl_run",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def build_lifeline_aim(
    *,
    lifeline_class: str,
    ground_failure: bool,
    bridge_class: int,
    state_code: int,
    year_built: int,
    num_spans: int,
    max_span_length_m: float,
    skew_deg: int,
    deck_width_m: float,
    structure_length_m: float,
    pipe_diameter_m: float,
    pipe_length_m: float,
    pipe_material: str,
    substation_voltage: str,
    substation_anchored: bool,
) -> dict[str, Any]:
    """Assemble a HAZUS lifeline AIM config for ``lifeline_class``. Pure.

    The ``GeneralInformation`` keys are the exact spellings the bundled
    ``pelicun_config.py`` auto-population scripts consume: ``assetSubtype`` +
    BridgeClass/StateCode/... for HwyBridge, ``type='Pipe'`` + Diam/Len/material
    for a buried water main, ``type='Substation'`` + Voltage/Anchored for a
    substation. ``Applications/DL`` selects the class DL_Method.
    """
    spec = _CLASS_SPEC[lifeline_class]
    units = {"force": "kips", "length": "m", "time": "sec"}
    if lifeline_class == "transportation":
        gi: dict[str, Any] = {
            "AIM_id": "1",
            "assetSubtype": "HwyBridge",
            "BridgeClass": int(bridge_class),
            "StateCode": int(state_code),
            "YearBuilt": int(year_built),
            "NumOfSpans": int(num_spans),
            "MaxSpanLength": float(max_span_length_m),
            "Skew": int(skew_deg),
            "DeckWidth": float(deck_width_m),
            "StructureLength": float(structure_length_m),
            "ConstructType": "",
            "units": units,
        }
    elif lifeline_class == "potable_water":
        gi = {
            "AIM_id": "1",
            "type": "Pipe",
            "Diam": float(pipe_diameter_m),
            "Len": float(pipe_length_m),
            "material": str(pipe_material),
            "year": int(year_built),
            "units": units,
        }
    else:  # electric_power
        gi = {
            "AIM_id": "1",
            "type": "Substation",
            "Voltage": str(substation_voltage),
            "Anchored": bool(substation_anchored),
            "units": units,
        }
    return {
        "GeneralInformation": gi,
        "assetType": spec["asset_type"],
        "Applications": {
            "DL": {
                "ApplicationData": {
                    "DL_Method": spec["dl_method"],
                    "ground_failure": bool(ground_failure),
                    "coupled_EDP": True,
                }
            }
        },
    }


def build_lifeline_demand_csv(
    *,
    lifeline_class: str,
    ground_failure: bool,
    realizations: int,
    intensities: dict[str, float],
    out_path: str,
) -> str:
    """Write a coupled-EDP demand CSV of the class's IMTs, each a constant level.

    Every fragility EDP the class's components read is written as a
    ``1-<TAG>-1-1`` column of ``realizations`` identical rows at the stated
    intensity; the HAZUS fragility's own lognormal capacity dispersion then drives
    the damage-state spread. This is the methodology-anchor demand (a stated
    shaking level), the lifeline analogue of the building template's bundled EDP
    series. Returns ``out_path``.
    """
    import csv

    spec = _CLASS_SPEC[lifeline_class]
    columns: dict[str, float] = {}
    for tag, knob in spec["imts"].items():
        columns[f"1-{tag}-1-1"] = float(intensities[knob])
    if ground_failure and spec["gf_imt"] is not None:
        tag, knob = spec["gf_imt"]
        columns[f"1-{tag}-1-1"] = float(intensities[knob])

    n = int(realizations)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["", *columns.keys()])
        for i in range(n):
            writer.writerow([i, *[f"{v:.6f}" for v in columns.values()]])
    return out_path


def _primary_component(
    damage_state_probs: dict[str, dict[int, float]], prefix: str
) -> str | None:
    """Pick the component whose id starts with ``prefix`` (the structural asset,
    not the ``collapse`` bookkeeping component)."""
    for cmp_id in damage_state_probs:
        if str(cmp_id).startswith(prefix):
            return cmp_id
    return None


def build_damage_state_chart_spec(
    ds_probs: dict[int, float], *, title: str, labels: dict[int, str]
) -> dict[str, Any]:
    """HAZUS damage-state probability bar chart (P[asset in DS] vs DS label). Pure.

    Used for the water / power classes, whose HAZUS fragility carries no
    repair-cost consequence -> the damage-state distribution is the product.
    """
    rows = [
        {"damage_state": labels.get(ds, str(ds)),
         "order": int(ds),
         "probability": round(float(prob), 5)}
        for ds, prob in sorted(ds_probs.items())
    ]
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": title,
        "data": {"values": rows},
        "mark": {"type": "bar", "color": "#e45756"},
        "encoding": {
            "x": {"field": "damage_state", "type": "nominal",
                  "title": "HAZUS damage state",
                  "sort": [labels[i] for i in sorted(labels)]},
            "y": {"field": "probability", "type": "quantitative",
                  "title": "probability", "scale": {"domain": [0, 1]}},
            "tooltip": [
                {"field": "damage_state", "type": "nominal"},
                {"field": "probability", "type": "quantitative", "format": ".3f"},
            ],
        },
    }


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_hazus_lifeline_seismic_dl_run(
    lifeline_class: Literal[
        "transportation", "potable_water", "electric_power"
    ] = "transportation",
    ground_failure: bool = False,
    # transportation (HwyBridge) attributes
    bridge_class: int = 502,
    state_code: int = 39,
    year_built: int = 1965,
    num_spans: int = 3,
    max_span_length_m: float = 30.0,
    skew_deg: int = 20,
    deck_width_m: float = 12.0,
    structure_length_m: float = 90.0,
    # potable_water (Pipe) attributes
    pipe_diameter_m: float = 0.3,
    pipe_length_m: float = 60.0,
    pipe_material: str = "DI",
    # electric_power (Substation) attributes
    substation_voltage: str = "low",
    substation_anchored: bool = False,
    # ground-motion intensities (constant EDP levels; labeled methodology inputs).
    # Defaults are a moderate-to-strong earthquake demand -- chosen so each HAZUS
    # lifeline fragility resolves a mixed (non-saturated) damage-state spread.
    sa_1_0_g: float = 0.8,
    sa_0_3_g: float = 1.2,
    pga_g: float = 0.6,
    pgv_cmps: float = 6.0,
    pgd_inch: float = 0.3,
    realizations: int = 100,
    seed: int = 42,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Run a HAZUS earthquake damage-and-loss assessment for one lifeline asset class.

    Fidelity: pelicun's real DL_calculation pipeline on the bundled HAZUS lifeline
    fragility/consequence datasets (transportation v5.1, potable-water v6.1, power
    v5.1), auto-populating the HAZUS component from AIM attributes. The demand is a
    stated constant ground-motion level per EDP; the HAZUS fragility dispersion
    drives the damage-state spread. A reproducible (seeded) Monte-Carlo run -- a
    methodology / coverage anchor, not a per-asset estimate over a fetched hazard
    field + fetched inventory. Off-scope: a live per-city HwyBridge fetch needs
    National Bridge Inventory structural attributes (span count/length, skew, deck
    width) that our OSM/HIFLD fetchers do not carry; buried water mains are not in
    OSM -> pipe geometry must be user-provided; the OpenQuake scenario-GMF per-asset
    demand wiring is follow-on work.

    Use this when: the user asks to run a HAZUS earthquake lifeline (bridge / water
    pipe / power substation) damage assessment, to see the seismic damage-state or
    repair-cost distribution for a bridge/pipe/substation class, or to exercise the
    HAZUS transportation / potable-water / electric-power fragility libraries.

    Params:
        lifeline_class: "transportation" (HwyBridge) | "potable_water" (Pipe) |
            "electric_power" (Substation). Selects the DL_Method + AIM builder.
        ground_failure: add the permanent-ground-deformation (PGD) failure
            component (bridges + pipes; ignored for power). Default False.
        bridge_class: NBI structure-type code for the HwyBridge (e.g. 502).
        state_code: FIPS state code (drives the HAZUS seismic-design cutoff).
        year_built: construction year (design-era cutoff; also pipe vintage).
        num_spans, max_span_length_m, skew_deg, deck_width_m, structure_length_m:
            HwyBridge geometry the HAZUS bridge classifier + K_skew/K_3D/PGD
            modifiers consume.
        pipe_diameter_m, pipe_length_m, pipe_material: buried-pipe attributes
            (material one of CI/DI/AC/RCC/PVC/DS/BS/ST; drives brittle/ductile
            flexibility).
        substation_voltage: "low" | "medium" | "high" (or a kV number as a string).
        substation_anchored: seismic-anchoring flag.
        sa_1_0_g, sa_0_3_g, pga_g, pgv_cmps, pgd_inch: constant ground-motion EDP
            levels (only the class's EDPs are used).
        realizations: Monte-Carlo sample size. Default 100.
        seed: Monte-Carlo seed (fixed -> reproducible). Default 42.

    Returns:
        On success: ``{"status": "ok", "lifeline_class", "asset_subtype",
        "auto_populated_component", "component_database", "demand_levels",
        "damage_state_probabilities" (primary component, DS label -> prob),
        "expected_repairs" (water only), "loss_summary" (transportation only),
        "seed", "realizations", "chart_emitted"}``. A loss-exceedance chart
        (transportation) or a damage-state bar chart (water/power) is emitted when
        a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if lifeline_class not in _CLASS_SPEC:
        return {"status": "error",
                "error_code": "PELICUN_LIFELINE_INVALID_CLASS",
                "error_message": (
                    f"lifeline_class must be one of {sorted(_CLASS_SPEC)}; "
                    f"got {lifeline_class!r}.")}
    if int(realizations) <= 0:
        return {"status": "error",
                "error_code": "PELICUN_DL_CALCULATION_INVALID",
                "error_message": "realizations must be a positive integer."}

    spec = _CLASS_SPEC[lifeline_class]
    intensities = {
        "sa_1_0_g": sa_1_0_g, "sa_0_3_g": sa_0_3_g, "pga_g": pga_g,
        "pgv_cmps": pgv_cmps, "pgd_inch": pgd_inch,
    }
    tmp_dir = tempfile.mkdtemp(prefix="trid3nt_lifeline_dl_")
    demand_path = os.path.join(tmp_dir, "demand.csv")
    try:
        build_lifeline_demand_csv(
            lifeline_class=lifeline_class, ground_failure=bool(ground_failure),
            realizations=int(realizations), intensities=intensities,
            out_path=demand_path,
        )
        aim = build_lifeline_aim(
            lifeline_class=lifeline_class, ground_failure=bool(ground_failure),
            bridge_class=bridge_class, state_code=state_code, year_built=year_built,
            num_spans=num_spans, max_span_length_m=max_span_length_m,
            skew_deg=skew_deg, deck_width_m=deck_width_m,
            structure_length_m=structure_length_m,
            pipe_diameter_m=pipe_diameter_m, pipe_length_m=pipe_length_m,
            pipe_material=pipe_material, substation_voltage=substation_voltage,
            substation_anchored=substation_anchored,
        )
        result = await asyncio.to_thread(
            run_dl_calculation,
            aim_config=aim, demand_csv_path=demand_path,
            realizations=int(realizations), seed=int(seed), coupled_edp=True,
            detailed_results=True,
        )
    except DLCalculationError as exc:
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_hazus_lifeline_seismic_dl_run failed: %s", exc)
        return {"status": "error",
                "error_code": "PELICUN_DL_CALCULATION_ERROR",
                "error_message": f"HAZUS lifeline DL run failed: {exc}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    labels = _ds_labels(lifeline_class)
    primary = _primary_component(
        result.damage_state_probs, spec["primary_component_prefix"])
    ds_probs_raw = result.damage_state_probs.get(primary, {}) if primary else {}
    damage_state_probabilities = {
        labels.get(ds, str(ds)): round(float(p), 5)
        for ds, p in sorted(ds_probs_raw.items())
    }
    demand_levels = {
        tag: float(intensities[knob]) for tag, knob in spec["imts"].items()
    }
    if ground_failure and spec["gf_imt"] is not None:
        tag, knob = spec["gf_imt"]
        demand_levels[tag] = float(intensities[knob])

    out: dict[str, Any] = {
        "status": "ok",
        "lifeline_class": lifeline_class,
        "asset_subtype": spec["asset_subtype"],
        "auto_populated_component": result.component_assignment,
        "component_database": spec["dl_method"],
        "demand_levels": demand_levels,
        "damage_state_probabilities": damage_state_probabilities,
        "seed": int(seed),
        "realizations": int(realizations),
    }

    if spec["has_repair_cost"]:
        summary = result.dl_summary
        stats = result.dl_summary_stats
        cost_col = next(
            (c for c in summary.columns if str(c).startswith("repair_cost")), None)
        time_col = next(
            (c for c in summary.columns if str(c).startswith("repair_time")), None)
        coll_col = "collapse" if "collapse" in summary.columns else None

        def _stat(col: str | None, row: str) -> float | None:
            if col is None:
                return None
            try:
                return float(stats.loc[row, col])
            except Exception:  # noqa: BLE001
                return None

        out["loss_summary"] = {
            "mean_repair_cost_ratio": _stat(cost_col, "mean"),
            "median_repair_cost_ratio": _stat(cost_col, "50%"),
            "p90_repair_cost_ratio": _stat(cost_col, "90%"),
            "mean_repair_time_days": _stat(time_col, "mean"),
            "collapse_probability": _stat(coll_col, "mean"),
        }
        spec_chart = build_loss_exceedance_chart_spec(
            summary[cost_col].to_numpy() if cost_col else [])
        chart_title = "Repair-cost loss exceedance (HAZUS transportation bridge)"
        emitted = await emit_chart_if_live(
            spec_chart, title=chart_title,
            caption="Monte-Carlo repair-cost loss-exceedance curve from pelicun's "
            "DL_calculation auto-populated HAZUS transportation-bridge run.")
    else:
        # water / power: no repair-cost consequence -> damage-state distribution.
        # For pipes the distribution is per-20ft-segment, averaged over the main
        # (DS1 = leak, DS2 = break); pelicun encodes the HAZUS repair rate as
        # discrete per-segment leak/break states, so the reported figure is the
        # honest segment-level damage split, not an unvalidated repairs/km count.
        chart_title = (
            f"HAZUS {lifeline_class.replace('_', ' ')} damage-state distribution")
        spec_chart = build_damage_state_chart_spec(
            ds_probs_raw, title=chart_title, labels=labels)
        emitted = await emit_chart_if_live(
            spec_chart, title=chart_title,
            caption="Monte-Carlo HAZUS damage-state probabilities from pelicun's "
            f"DL_calculation auto-populated {lifeline_class} run.")

    out["chart_emitted"] = emitted
    logger.info(
        "pelicun_hazus_lifeline_seismic_dl_run class=%s component=%s ds=%s",
        lifeline_class, result.component_assignment, damage_state_probabilities)
    return out
