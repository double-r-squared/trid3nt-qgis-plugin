"""Engine template ``sfincs_advanced_numerical_physics_knobs`` -- SFINCS core
numerical/physical solver settings exposed as labeled, opt-in knobs.

Candidate source: the SFINCS User manual (sfincs.readthedocs.io/en/latest/input.html,
Numerical/physical parameters section: theta, alpha, advection, huthresh, wind-drag
cd_wnd/cd_val per Vatvani et al. 2012). This is a KNOB-ONLY template: it adds NO new
data/geometry ingestion. It runs the SAME SFINCS flood pipeline as ``sfincs_flood``
(the composer) with the numerical solver settings surfaced as first-class
parameters + a labeled provenance delta, then returns the SAME ``AssessmentEnvelope``
(peak-depth COG + metrics + the map deliverable ``sfincs_flood`` publishes).

The knobs are validated + range-checked through the shared ``physics_registry``
(engine ``"sfincs"``) -- the SAME resolver ``sfincs_flood.advanced_physics`` uses, so
this template is a strict OPT-IN entry point over an already-plumbed deck surface (a
run with no knob set is BYTE-IDENTICAL to the ``sfincs_flood`` baseline).

FIDELITY / OFF-SCOPE (the honest floor): these are SOLVER-STABILITY / RUNTIME /
result-smoothness tuning levers, NOT a calibration or accuracy-improvement surface --
the cited manual publishes NO expected-output figure, so this template EMITS NO
fabricated chart; the deliverable is the flood-depth map plus the labeled
solver-settings delta (invariant 1: the applied delta is computed from the resolved
knobs, never narrated free-hand). ``viscosity``/``nuvisc`` and the ``friction2d``
toggle from the manual are NOT yet plumbed into the SFINCS deck builder and are
therefore NOT exposed here (a named residual, not a silent drop). For the flood
scenario itself (pluvial/coastal/riverine, forcing, structures) use ``sfincs_flood``.

Determinism boundary (invariant 1): every solver setting the agent narrates comes
from the typed resolved-physics delta this composer computed, never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.envelope import AssessmentEnvelope
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.workflows.sfincs._template_card import TemplateCard
from trid3nt_server.agent.workflows.shared.physics_registry import (
    PhysicsRegistryError,
    applied_physics_delta,
    validate_and_resolve_physics,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.sfincs.numerical_physics.numerical_physics"
)

__all__ = [
    "sfincs_advanced_numerical_physics_knobs",
    "TEMPLATE_CARD",
]

#: The SFINCS numerical/physical knobs this template exposes -> the physics_registry
#: key each maps to (all validated + range-checked there). ``viscosity``/``friction2d``
#: are intentionally ABSENT (not yet plumbed into the deck builder -- a named residual).
_KNOB_KEYS: tuple[str, ...] = (
    "theta",
    "alpha",
    "advection",
    "huthresh",
    "wind_drag",
    "coriolis_latitude",
)

#: The LOUD tuning-surface honesty floor.
_PHYSICS_NOTE: str = (
    "SOLVER-SETTINGS TUNING SURFACE: theta/alpha/advection/huthresh/wind-drag are "
    "numerical-stability / runtime / smoothness levers, NOT an accuracy-calibration "
    "surface. No published expected-output exists for these knobs (the SFINCS manual "
    "documents them without a reference figure), so no chart is emitted; the "
    "deliverable is the flood-depth map + the labeled settings delta. viscosity and "
    "the friction2d toggle are not yet plumbed and are omitted."
)


TEMPLATE_CARD = TemplateCard(
    question=(
        "the SAME SFINCS flood run as sfincs_flood, but with the CORE NUMERICAL "
        "SOLVER SETTINGS (theta implicitness, alpha CFL limiter, advection scheme, "
        "huthresh wet/dry threshold, wind drag, Coriolis latitude) exposed as "
        "labeled opt-in knobs for a stability/runtime sensitivity study -- a "
        "flood-depth map + the resolved solver-settings delta"
    ),
    required_inputs=["location_query (or bbox)"],
    knobs="theta, alpha, advection, huthresh, wind_drag, coriolis_latitude, return_period_yr, duration_hr, input_mode",
)


_SFINCS_PHYSICS_METADATA = AtomicToolMetadata(
    name="sfincs_advanced_numerical_physics_knobs",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="sfincs",
    tier="template",
)


@register_tool(
    _SFINCS_PHYSICS_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def sfincs_advanced_numerical_physics_knobs(
    location_query: str | None = None,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    theta: float | None = None,
    alpha: float | None = None,
    advection: int | None = None,
    huthresh: float | None = None,
    wind_drag: float | None = None,
    coriolis_latitude: float | None = None,
    return_period_yr: int = 100,
    duration_hr: int = 24,
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> AssessmentEnvelope | dict[str, Any]:
    """Run a SFINCS flood with the CORE NUMERICAL SOLVER SETTINGS exposed as opt-in knobs.

    Fidelity: SFINCS (fast reduced-physics flood screening) -- the SAME solver +
    pipeline as ``sfincs_flood``, with the numerical/physical solver settings
    surfaced as first-class parameters instead of buried in an ``advanced_physics``
    dict. These knobs are STABILITY / RUNTIME / SMOOTHNESS levers (a sensitivity
    surface), NOT an accuracy-calibration surface -- the cited SFINCS manual
    publishes no expected-output figure for them, so this template emits NO chart;
    the deliverable is the peak flood-depth map + the labeled settings delta.

    THE tool for "how sensitive is the SFINCS flood to the numerical scheme", "run
    SFINCS with advection off / a different theta / a smaller CFL alpha", "tune the
    SFINCS solver settings (huthresh wet/dry threshold, wind drag, theta, alpha)",
    "SFINCS numerical stability / solver-settings study". A run with NO knob set is
    byte-identical to the ``sfincs_flood`` baseline.

    Do NOT use this for:
        - The flood scenario itself (pluvial/coastal/riverine/compound forcing,
          structures, buildings, infiltration) -- use ``sfincs_flood``.
        - Refinement-grade riverine (``hecras_riverine_flood``), urban drainage
          (``swmm_urban_flood``), or coastal-tidal (``schism_tidal_hydro``).

    Params:
        location_query / bbox: the AOI (a place name geocoded to a bbox, or an
            explicit EPSG:4326 ``[min_lon, min_lat, max_lon, max_lat]``).
        theta: semi-implicit time-integration weighting (manual default 1.0 = no
            smoothing; range 0.8-1.0). Lower = more smoothing.
        alpha: CFL-based time-step safety factor (manual default 0.5; range
            0.1-0.75). Lower = smaller dt = stabler + slower.
        advection: momentum advection scheme -- ``0`` = SFINCS-LIE local-inertial,
            ``1`` = SFINCS-SSWE advection-on (default; recommended). No value 2.
        huthresh: wet/dry threshold water depth (m) for momentum (manual default
            0.05; range 0.001-0.1).
        wind_drag: constant wind-drag coefficient override (range 0-0.01; 0 keeps
            the SFINCS default drag formula). Only meaningful with wind forcing.
        coriolis_latitude: constant-plane Coriolis latitude (deg; 0 = no Coriolis)
            for a large-domain surge run.
        return_period_yr / duration_hr: the design-storm forcing (as ``sfincs_flood``).
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` reviews the resolved
            solver settings before the solve; ``"auto"`` (default) proceeds labeled.

    Returns:
        On success: the ``sfincs_flood`` ``AssessmentEnvelope`` (peak-depth COG +
        metrics + map deliverable), its provenance carrying the applied physics
        delta.
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``
        (an out-of-range/unknown knob returns ``ADVANCED_PHYSICS_INVALID``).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    # --- Build the overrides dict from the non-None knobs. -------------------- #
    raw_overrides: dict[str, Any] = {}
    for key in _KNOB_KEYS:
        val = locals().get(key)
        if val is not None:
            raw_overrides[key] = val

    # --- Validate + range-check through the shared resolver (the SAME one the --- #
    # --- sfincs_flood deck uses). A typed error, never a silent clamp. -------- #
    try:
        resolved = validate_and_resolve_physics("sfincs", raw_overrides or None)
    except PhysicsRegistryError as exc:
        logger.warning("sfincs_advanced_numerical_physics_knobs: invalid knob (%s)", exc)
        return {
            "status": "error",
            "error_code": "ADVANCED_PHYSICS_INVALID",
            "error_message": str(exc),
        }

    delta = applied_physics_delta("sfincs", resolved)
    bbox_t = tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else None
    logger.info(
        "sfincs_advanced_numerical_physics_knobs location=%s bbox=%s resolved=%s mode=%s",
        location_query, bbox_t, resolved, input_mode,
    )

    # --- The input-review gate (ADR 0107): the solver-settings delta is the ---- #
    # --- physically-dominant, otherwise-buried input. Surface it as structured -- #
    # --- provenance so a numerical-scheme choice is never mistaken for the base. -#
    if resolved:
        review_entries = [
            SyntheticInput(
                param=key,
                value=info["to"],
                basis="user",
                note=f"{info['doc']} (deck default {info['from']}; -> {info['deck_target']})",
            )
            for key, info in delta.items()
        ]
    else:
        review_entries = [
            SyntheticInput(
                param="solver_settings",
                value="SFINCS defaults (no override)",
                basis="default_demo",
                note="no numerical knob was set -> byte-identical to the sfincs_flood baseline",
            )
        ]
    review_entries.append(
        SyntheticInput(
            param="scope", value="numerical-tuning surface", basis="default_demo",
            note=_PHYSICS_NOTE,
        )
    )
    review = await gate_input_review(
        tool_name="sfincs_advanced_numerical_physics_knobs",
        mode=input_mode,
        entries=review_entries,
        params={"return_period_yr": return_period_yr, "duration_hr": duration_hr},
    )
    if not review.proceed:
        return {
            "status": "error",
            "error_code": "SFINCS_INPUT_REVIEW_CANCELLED",
            "error_message": review.cancel_reason or "input review not approved; the solver did not run",
        }

    # --- Delegate to the SFINCS flood pipeline with the resolved settings. ----- #
    from trid3nt_server.agent.workflows.sfincs.flood.flood import model_flood_scenario

    try:
        envelope = await model_flood_scenario(
            bbox=bbox_t,
            location_query=location_query,
            return_period_yr=return_period_yr,
            duration_hr=duration_hr,
            compute_class=compute_class,
            advanced_physics=(resolved or None),
        )
        logger.info(
            "sfincs_advanced_numerical_physics_knobs complete envelope_id=%s knobs=%s",
            getattr(envelope, "envelope_id", None), sorted(resolved),
        )
        return envelope
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("sfincs_advanced_numerical_physics_knobs unexpected failure")
        return {
            "status": "error",
            "error_code": "SFINCS_INTERNAL_ERROR",
            "error_message": str(exc),
        }
