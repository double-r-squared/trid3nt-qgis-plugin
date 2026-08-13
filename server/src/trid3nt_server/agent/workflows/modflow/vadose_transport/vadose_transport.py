"""the composer - MODFLOW UZF+UZT vadose-transport composer (ADR 0228).

The end-to-end higher-order workflow for the MODFLOW ``vadose_transport``
archetype: it turns a surface spill site (a place name or an AOI point) into a
rendered tracer-breakthrough chart (concentration at the base of the vadose
column vs. time) + a spill-site context POINT layer. The question is a purely-
advective vertical travel-time problem: "a contaminant is spilled at the LAND
SURFACE -- how long until it reaches the water table, and at what concentration?"

Canonical real-world pipeline mirrored here (a UZF+UZT unsaturated-zone solute-
travel analysis, matching modflow6-examples ex-gwt-uzt-2d):

    resolve the spill point (geocode a place, or take an explicit lat/lon)
        -> assemble MODFLOWRunArgs(archetype='vadose_transport',
                                   spill_location_latlon=(...),
                                   vadose_thickness_m=..., Brooks-Corey/infiltration
                                   demo-defaulted behind the input-review gate)
        -> run_modflow_archetype_job (dual GWF+GWT UZF+UZT column, dual IMS,
           GWF-first -> mf6 LOCAL -> postprocess_vadose)
        -> VadoseBreakthroughLayerURI (vector: a FlatGeobuf spill-site POINT
           carrying arrival time + peak concentration) + the breakthrough chart
           via ``_chart_payload``

PRIMARY product (design: CHART-PRIMARY -- 1D-column physics): a Vega-Lite
breakthrough concentration-vs-time line chart (x = elapsed days, y = base-of-
column concentration, + a half-source threshold rule). The chart is built by
``postprocess_vadose`` (no second read here) and stashed as ``_chart_payload`` on
the returned ``VadoseBreakthroughLayerURI``; the composer reads it via
``getattr(layer, '_chart_payload', None)`` and emits it through
``emit_chart_payloads``.

MAP element (thin): a FlatGeobuf VECTOR point at the spill ``(lat, lon)`` that
geolocates WHERE the 1D vadose column was evaluated. It is loaded by the shared
``_run_archetype`` seam (``add_loaded_layer``) exactly as saltwater_intrusion
loads its transect -- so a "modeled" result NEVER reads zero-layers (the
zero-layers hole is avoided by construction: the spill point is always emitted).

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** The spill LOCATION is a user input -- a run
  with neither ``location`` nor ``aoi_latlon`` returns a typed
  ``USER_INPUT_REQUIRED`` failure (never invented). The Brooks-Corey / infiltration
  soil hydraulics are LABELED DEMO DEFAULTS routed through the input-review gate
  (there is no site soil-hydraulics fetcher in v1), narrated as demo assumptions.
- **10. Minimal parameter surface: preserves.** Intent (the spill site + the depth
  to water table) is exposed; the column discretization + the transport horizon
  are derived, not user-supplied.

PRECISION CAVEAT (Invariant 1): the arrival time is a qualitative screening
estimate on a 1D advective UZF+UZT column with DEMO soil hydraulics (Brooks-Corey
water content, infiltration flux, unsaturated K), NOT a calibrated contaminant-
transport forecast. The agent must narrate this caveat when presenting the chart.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_VADOSE_EPS,
    DEFAULT_VADOSE_INFILTRATION_CONC,
    DEFAULT_VADOSE_INFILTRATION_RATE_M_DAY,
    DEFAULT_VADOSE_THICKNESS_M,
    DEFAULT_VADOSE_THTR,
    DEFAULT_VADOSE_THTS,
    DEFAULT_VADOSE_VKS_M_DAY,
    MODFLOWRunArgs,
    VadoseBreakthroughLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.workflows.modflow._input_review import (
    gate_and_stamp_modflow_inputs,
    vadose_soil_review_entries,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter, emit_chart_payloads
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.modflow._template_card import TemplateCard
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes).
from trid3nt_server.agent.workflows.modflow.sustainable_yield.sustainable_yield import (
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.modflow.vadose_transport.vadose_transport")

__all__ = [
    "VadoseTransportResult",
    "model_vadose_transport_scenario",
    "modflow_vadose_transport",
    "VadoseTransportScenarioError",
    "VadoseTransportInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class VadoseTransportResult(GraceModel):
    """Return type for the composer.

    Bundles the breakthrough layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field -- ``breakthrough_layer``
    carries ``breakthrough_time_days`` + ``peak_concentration`` +
    ``vadose_thickness_m`` + the ``concentration_series`` / ``time_series_days``.
    """

    schema_version: str = "v1"

    breakthrough_layer: VadoseBreakthroughLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class VadoseTransportScenarioError(RuntimeError):
    """Base class for the composer failures."""

    error_code: str = "VADOSE_TRANSPORT_SCENARIO_ERROR"
    retryable: bool = False


class VadoseTransportInputError(VadoseTransportScenarioError):
    """Caller supplied invalid / missing spill-site or soil input (honesty gate).

    Invariant 9: the spill LOCATION is never fabricated. A run with neither a
    ``location`` nor an ``aoi_latlon`` raises this so the agent asks the user for
    the real spill site.
    """

    error_code = "VADOSE_TRANSPORT_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_vadose_transport_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    vadose_thickness_m: float | None = None,
    vadose_thtr: float | None = None,
    vadose_thts: float | None = None,
    vadose_eps: float | None = None,
    vadose_infiltration_conc: float | None = None,
    vadose_infiltration_rate_m_day: float | None = None,
    vadose_vks_m_day: float | None = None,
    contaminant: str = "tracer",
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> VadoseTransportResult:
    """Compose a spill site -> MODFLOW UZF+UZT -> VadoseBreakthroughLayerURI.

    Args:
        location: a place name (geocoded to the spill point). Supply this OR
            ``aoi_latlon`` -- exactly one.
        aoi_latlon: an explicit ``(lat, lon)`` spill point.
        vadose_thickness_m: depth to the water table at the spill (the unsaturated-
            column thickness), m. The ARRIVAL TIME scales with it (the headline
            physics). When None a ~4 m demo default is applied (narrated as a demo
            assumption; a real run reads it from a depth-to-water source).
        vadose_thtr / vadose_thts / vadose_eps: Brooks-Corey residual / saturated
            water content + exponent of the unsaturated medium (demo soil hydraulics).
        vadose_infiltration_conc: tracer concentration in the surface infiltration
            (the breakthrough curve is a fraction of this). Demo default 1.0 (unit).
        vadose_infiltration_rate_m_day: surface infiltration flux, m/day (faster ->
            earlier arrival). Demo default 0.01 m/day.
        vadose_vks_m_day: saturated vertical K of the unsaturated medium, m/day.
        contaminant: the solute label for narration (the transport is a conservative
            advective tracer regardless).
        compute_class: FR-CE-3 compute class. vadose_transport is LOCAL-ONLY.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``VadoseTransportResult`` with the ``VadoseBreakthroughLayerURI`` (arrival
        time + peak concentration + the breakthrough series) + derived args + a
        narration summary. The breakthrough chart is emitted as a side effect.

    Raises:
        VadoseTransportInputError: missing/invalid spill site or soil input
            (Invariant 9 gate).
        VadoseTransportScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Validate scalar soil inputs (typed, before the geocode) --------------
    try:
        if vadose_thickness_m is not None and float(vadose_thickness_m) <= 0.0:
            raise ValueError("vadose_thickness_m must be > 0")
        _thtr = float(vadose_thtr) if vadose_thtr is not None else DEFAULT_VADOSE_THTR
        _thts = float(vadose_thts) if vadose_thts is not None else DEFAULT_VADOSE_THTS
        if not (_thts > _thtr):
            raise ValueError(
                f"vadose_thts ({_thts}) must exceed vadose_thtr ({_thtr}) (Brooks-Corey)"
            )
        if vadose_infiltration_rate_m_day is not None and float(vadose_infiltration_rate_m_day) <= 0.0:
            raise ValueError("vadose_infiltration_rate_m_day must be > 0")
    except VadoseTransportInputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VadoseTransportInputError(
            f"invalid vadose_transport parameter: {exc}"
        ) from exc

    # declare the planned internal-tool count up front: geocode (only when a place
    # string was supplied) + run_modflow_archetype_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    # Honesty gate (Invariant 9): the spill site is never fabricated. This raises a
    # SustainableYieldInputError from the shared resolver when neither location nor
    # aoi_latlon is supplied; re-map it to the vadose input error.
    try:
        lat, lon, location_name = await _resolve_aoi_point(
            location, aoi_latlon, pipeline_emitter=pipeline_emitter
        )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "SustainableYieldInputError":
            raise VadoseTransportInputError(
                "vadose_transport requires a spill site: supply exactly one of "
                "location (a place name) or aoi_latlon (an explicit lat/lon). The "
                "spill site is a user input and is never invented; ask the user."
            ) from exc
        raise

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant=str(contaminant or "tracer"),
            release_rate_kg_s=1.0,  # ignored: UZT infiltration-driven, not a SRC
            duration_days=1.0,      # ignored when archetype is set (deck sets TDIS)
            archetype="vadose_transport",
            vadose_thickness_m=vadose_thickness_m,
            vadose_thtr=(vadose_thtr if vadose_thtr is not None else DEFAULT_VADOSE_THTR),
            vadose_thts=(vadose_thts if vadose_thts is not None else DEFAULT_VADOSE_THTS),
            vadose_eps=(vadose_eps if vadose_eps is not None else DEFAULT_VADOSE_EPS),
            vadose_infiltration_conc=(
                vadose_infiltration_conc
                if vadose_infiltration_conc is not None
                else DEFAULT_VADOSE_INFILTRATION_CONC
            ),
            vadose_infiltration_rate_m_day=(
                vadose_infiltration_rate_m_day
                if vadose_infiltration_rate_m_day is not None
                else DEFAULT_VADOSE_INFILTRATION_RATE_M_DAY
            ),
            vadose_vks_m_day=(
                vadose_vks_m_day if vadose_vks_m_day is not None else DEFAULT_VADOSE_VKS_M_DAY
            ),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise VadoseTransportInputError(
            f"invalid vadose_transport run arguments: {exc}"
        ) from exc

    thickness_label = (
        f"{float(vadose_thickness_m):g} m"
        if vadose_thickness_m is not None
        else f"{DEFAULT_VADOSE_THICKNESS_M:g} m (demo)"
    )
    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model vadose-zone breakthrough [{thickness_label} to water table]",
        expected_type=VadoseBreakthroughLayerURI,
        error_code="VADOSE_TRANSPORT_RUN_FAILED",
        scenario_error=VadoseTransportScenarioError,
    )

    # Emit the breakthrough chart that postprocess built + stashed (best-effort).
    chart = getattr(layer, "_chart_payload", None)
    if chart is not None:
        await emit_chart_payloads(chart)

    arrival = getattr(layer, "breakthrough_time_days", 0.0)
    peak = getattr(layer, "peak_concentration", 0.0)
    thickness_m = getattr(layer, "vadose_thickness_m", 0.0)
    infil_conc = (
        float(vadose_infiltration_conc)
        if vadose_infiltration_conc is not None
        else DEFAULT_VADOSE_INFILTRATION_CONC
    )
    arrived = bool(peak >= 0.5 * infil_conc and arrival > 0.0)

    caveat = (
        "The vadose thickness (depth to water table), the Brooks-Corey water-content "
        "parameters, the infiltration flux, and the unsaturated vertical K are DEMO "
        "defaults (no site soil-hydraulics fetcher in v1). The arrival time is a "
        "qualitative screening estimate on a 1D purely-advective UZF+UZT column "
        "(MF6 has no unsaturated dispersion), NOT a calibrated transport forecast."
    )
    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "contaminant": run_args.contaminant,
        "vadose_thickness_m": (
            float(vadose_thickness_m) if vadose_thickness_m is not None else None
        ),
        "vadose_infiltration_rate_m_day": run_args.vadose_infiltration_rate_m_day,
        "vadose_infiltration_conc": run_args.vadose_infiltration_conc,
    }
    summary = {
        "location_name": location_name,
        "breakthrough_time_days": arrival,
        "peak_concentration": peak,
        "vadose_thickness_m": thickness_m,
        "reached_water_table": arrived,
        "breakthrough_note": (
            f"tracer crossed half the source concentration at {arrival:g} days"
            if arrived
            else "tracer did NOT cross half the source concentration within the "
                 "simulated horizon (a slow / deep column)"
        ),
        "demo_soil_caveat": caveat,
    }
    logger.info(
        "vadose_transport scenario complete location=%r arrival_days=%.4g peak=%.4g "
        "thickness_m=%.4g arrived=%s",
        location_name, arrival, peak, thickness_m, arrived,
    )
    # ADR 0223: structured soil-hydraulics provenance through gate_input_review,
    # stamped onto the layer envelope (the prose caveat stays on the summary).
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_vadose_transport", layer=layer,
        entries=vadose_soil_review_entries(
            thickness_m=thickness_m or DEFAULT_VADOSE_THICKNESS_M,
            thickness_user_supplied=(vadose_thickness_m is not None),
            thtr=run_args.vadose_thtr,
            thts=run_args.vadose_thts,
            eps=run_args.vadose_eps,
            infiltration_rate_m_day=run_args.vadose_infiltration_rate_m_day,
            infiltration_conc=run_args.vadose_infiltration_conc,
            vks_m_day=run_args.vadose_vks_m_day,
            note=caveat,
        ),
        params={"vadose_thickness_m": vadose_thickness_m},
    )
    if _review.cancelled:
        raise VadoseTransportScenarioError(
            f"vadose-transport input review {_review.cancel_reason or 'not approved'}"
        )
    return VadoseTransportResult(
        breakthrough_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question='how long a surface spill takes to reach the water table + the breakthrough concentration (unsaturated-zone UZF+UZT travel)',
    required_inputs=['location (or aoi_latlon)'],
    knobs='vadose_thickness_m, vadose_infiltration_rate_m_day, vadose_infiltration_conc, vadose_thtr, vadose_thts, vadose_eps',
)


_METADATA = AtomicToolMetadata(
    name="modflow_vadose_transport",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_vadose_transport(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    vadose_thickness_m: float | None = None,
    vadose_infiltration_rate_m_day: float | None = None,
    vadose_infiltration_conc: float | None = None,
    vadose_thtr: float | None = None,
    vadose_thts: float | None = None,
    vadose_eps: float | None = None,
    vadose_vks_m_day: float | None = None,
    contaminant: str = "tracer",
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model how long a surface spill takes to reach the water table (UZF+UZT vadose travel).

    Fidelity: MODFLOW 6 local planning-grade UNSATURATED-zone travel-time envelope
    (soil hydraulics + depth-to-water default to narrated demo values unless
    supplied), not a calibrated contaminant-transport delineation. Off-scope:
    a SATURATED plume footprint spreading laterally in the aquifer ->
    modflow_contaminant_plume; surface-water inundation -> sfincs_flood.

    Builds a MODFLOW 6 dual-model GWF+GWT UZF (unsaturated-flow) + UZT (unsaturated
    transport) 1D column at the spill site, runs it, and produces:

      * A Vega-Lite BREAKTHROUGH CHART (base-of-column tracer concentration vs.
        time, + a half-source threshold rule) -- the primary physical deliverable.
      * A VECTOR MAP layer: a FlatGeobuf spill-site POINT carrying the arrival time
        + peak concentration as attributes (geolocates the 1D column).
      * HEADLINE SCALAR: ``breakthrough_time_days`` -- the first time the base-of-
        column concentration crosses half the infiltration concentration (the
        tracer reaching the water table). Narrate this as the key physical result.

    Use this when:
        - The user asks how long a surface spill / leak takes to reach groundwater
          or the water table, unsaturated-zone travel time, or a vadose-zone
          breakthrough curve.
        - The user asks when infiltrating nitrate / a tracer will contaminate the
          aquifer below a field.

    Do NOT use this for:
        - A saturated contaminant plume spreading laterally (use
          ``modflow_contaminant_plume``).
        - A pumping-well drawdown (use ``modflow_sustainable_yield``).
        - Saltwater intrusion (use ``modflow_saltwater_intrusion``).

    PRECISION CAVEAT: this is a 1D purely-advective UZF+UZT column with DEMO soil
    hydraulics (Brooks-Corey water content, infiltration flux, unsaturated K) and a
    demo depth-to-water. Narrate the arrival time as a qualitative screening
    estimate, NOT a calibrated forecast (MF6 has no unsaturated dispersion).

    Params:
        location: place name (geocoded to the spill site). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` spill point.
        vadose_thickness_m: depth to the water table, m -- the ARRIVAL TIME scales
            with it. None -> ~4 m demo default (narrated).
        vadose_infiltration_rate_m_day: surface infiltration flux, m/day (faster ->
            earlier arrival). None -> 0.01 m/day demo default.
        vadose_infiltration_conc: tracer concentration in the infiltration. None ->
            1.0 (a unit tracer; the curve is then a fraction of the source).
        vadose_thtr / vadose_thts / vadose_eps: Brooks-Corey demo soil hydraulics.
        vadose_vks_m_day: unsaturated saturated-vertical-K demo default.
        contaminant: solute label for narration (transport is a conservative tracer).
        compute_class: FR-CE-3 compute class. This archetype runs LOCAL-ONLY.

    Returns:
        On success: a ``VadoseTransportResult`` JSON dict with the
        ``breakthrough_layer`` (a ``VadoseBreakthroughLayerURI`` carrying
        ``breakthrough_time_days`` + ``peak_concentration`` + ``vadose_thickness_m``
        + the ``concentration_series`` / ``time_series_days``), the
        ``derived_params``, and the ``summary``. On a recoverable failure (incl. a
        missing spill site) the tool returns a typed error the agent narrates
        honestly -- it never fabricates a spill location.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await model_vadose_transport_scenario(
            location=location,
            aoi_latlon=aoi,
            vadose_thickness_m=(
                float(vadose_thickness_m) if vadose_thickness_m is not None else None
            ),
            vadose_thtr=(float(vadose_thtr) if vadose_thtr is not None else None),
            vadose_thts=(float(vadose_thts) if vadose_thts is not None else None),
            vadose_eps=(float(vadose_eps) if vadose_eps is not None else None),
            vadose_infiltration_conc=(
                float(vadose_infiltration_conc)
                if vadose_infiltration_conc is not None
                else None
            ),
            vadose_infiltration_rate_m_day=(
                float(vadose_infiltration_rate_m_day)
                if vadose_infiltration_rate_m_day is not None
                else None
            ),
            vadose_vks_m_day=(
                float(vadose_vks_m_day) if vadose_vks_m_day is not None else None
            ),
            contaminant=contaminant,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except VadoseTransportInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except VadoseTransportScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "VADOSE_TRANSPORT_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
