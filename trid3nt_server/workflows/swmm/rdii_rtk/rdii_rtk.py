"""Engine template ``swmm_rdii_rtk_unit_hydrograph`` - RTK unit-hydrograph RDII.

How much RAINFALL-DERIVED INFLOW AND INFILTRATION (RDII) enters a sewer node vs
DIRECT RUNOFF, via the RTK triangular-unit-hydrograph method (Vallabhaneni et
al. / the EPA SWMM RDII model). Each of up to three unit hydrographs (short /
medium / long response) is a triangle defined by:

  * R = fraction of the rainfall VOLUME over the sewershed that becomes RDII,
  * T = time to the UH peak (hours),
  * K = ratio of the recession limb to the rising limb (so the base = T*(1+K)).

The UH peak is set so the triangle's area equals ``R * rainfall_depth * area``
(the RTK volume identity). Convolving the rainfall hyetograph with the summed
three UHs gives the RDII inflow hydrograph at the node.

TWO acceptance checks, both computed here and both DECLARED plan steps:
  1. the RTK VOLUME IDENTITY - the closed-form RDII volume equals
     ``(R1+R2+R3) * rainfall_depth * sewershed_area`` to machine precision;
  2. a NATIVE-SWMM cross-check - the same R/T/K + rain are authored into a real
     SWMM 5 deck ([HYDROGRAPHS] RTK + [RDII]) and solved through the swmm5
     engine; the closed-form peak RDII inflow reproduces SWMM's node inflow.

Citation (EPA RTK method; NATE to confirm the exact Table 7-1 numbers):
  Vallabhaneni, S., Chan, C.C., Burgess, E.H. 2007. "Computer Tools for Sanitary
  Sewer System Capacity Analysis and Planning." EPA/600/R-07/111 (the RTK
  unit-hydrograph RDII method SWMM 5 implements). The RTK method + its triangular
  unit-hydrograph equations are reproduced here; the closed form is validated
  AGAINST the SWMM 5 engine (the authoritative implementation the published
  worked example tabulates). The literal Table 7-1 row-by-row intermediate flows
  are flagged for NATE to supply/verify (the source is not machine-accessible
  here); the METHOD and the SWMM cross-check are exact. The EPA Table 7-1 setup
  itself is a saved invocation - ``scripts/demo_swmm_rdii_epa_table_7_1.py`` -
  not a constant in this module.

Closed-form validation class: the deliverable is a CHART (RDII hydrograph vs
direct runoff at the node) + typed scalars, no georeferenced raster.

Declared as PARAMS + ``plan(p, d)``; see ``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.lib import (
    DeclarativeError,
    FormGate,
    Param,
    Ref,
    Plan,
    doors,
    interpret,
    render_docstring,
    resolve_params,
)
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.rdii_rtk.steps import (
    build_rdii_chart,
    NODE,
    ClosedForm,
    Deck,
    Metrics,
)
from trid3nt_server.workflows.swmm.steps import Solve, SwmmStepError

logger = logging.getLogger("trid3nt_server.workflows.swmm.rdii_rtk.rdii_rtk")

__all__ = ["DATA", "PARAMS", "plan", "swmm_rdii_rtk_unit_hydrograph"]


TEMPLATE_CARD = TemplateCard(
    question=(
        "how much RAINFALL-DERIVED INFLOW AND INFILTRATION (RDII) enters a sewer "
        "node vs DIRECT RUNOFF, via the RTK triangular unit-hydrograph method "
        "(R/T/K), validated against the native SWMM 5 RDII engine"
    ),
    required_inputs=[],
    knobs=(
        "R1/T1/K1, R2/T2/K2, R3/T3/K3 (three UHs), sewershed_area_ac, "
        "rainfall_depth_in, storm_duration_hr, rainfall_series_in_per_hr, "
        "direct_runoff_coeff, dt_min"
    ),
)


#: R, T and K are CALIBRATION parameters of the RTK method - fitted to flow
#: monitoring at a real sewershed, not fetched. No fetcher serves them, so they
#: are labeled scenario defaults with declared bounds (the do_sag k1/k2 reading of
#: law 9), never claimed as site measurements.
PARAMS: tuple[Param, ...] = (
    # -- the three unit hydrographs ------------------------------------------ #
    Param("R1", door=doors.SCENARIO, default=0.10, bounds=(0.0, 1.0),
          user_lever=True, consequence="scenario",
          desc="SHORT-response unit hydrograph: fraction of the rainfall volume "
               "over the sewershed that becomes RDII through it; 0 drops this UH"),
    Param("T1", door=doors.SCENARIO, default=2.0, bounds=(0.0, 240.0), units="h",
          consequence="scenario",
          desc="Time to the SHORT unit hydrograph's peak"),
    Param("K1", door=doors.SCENARIO, default=2.0, bounds=(0.0, 20.0),
          consequence="scenario",
          desc="SHORT unit hydrograph recession/rise ratio; its base is T*(1+K)"),
    Param("R2", door=doors.SCENARIO, default=0.06, bounds=(0.0, 1.0),
          user_lever=True, consequence="scenario",
          desc="MEDIUM-response unit hydrograph RDII volume fraction; 0 drops it"),
    Param("T2", door=doors.SCENARIO, default=6.0, bounds=(0.0, 240.0), units="h",
          consequence="scenario",
          desc="Time to the MEDIUM unit hydrograph's peak"),
    Param("K2", door=doors.SCENARIO, default=3.0, bounds=(0.0, 20.0),
          consequence="scenario",
          desc="MEDIUM unit hydrograph recession/rise ratio"),
    Param("R3", door=doors.SCENARIO, default=0.03, bounds=(0.0, 1.0),
          user_lever=True, consequence="scenario",
          desc="LONG-response unit hydrograph RDII volume fraction - the slow "
               "infiltration tail; 0 drops it"),
    Param("T3", door=doors.SCENARIO, default=12.0, bounds=(0.0, 240.0), units="h",
          consequence="scenario",
          desc="Time to the LONG unit hydrograph's peak"),
    Param("K3", door=doors.SCENARIO, default=4.0, bounds=(0.0, 20.0),
          consequence="scenario",
          desc="LONG unit hydrograph recession/rise ratio; the tail the sewer "
               "keeps carrying after the storm"),

    # -- the sewershed -------------------------------------------------------- #
    Param("sewershed_area_ac", door=doors.SCENARIO, default=100.0,
          bounds=(0.01, 1.0e6), units="acre", consequence="scenario",
          desc="RDII drainage (sewershed) area tributary to the node"),
    Param("direct_runoff_coeff", door=doors.SCENARIO, default=0.30,
          bounds=(0.0, 1.0), consequence="scenario",
          desc="Rational-method runoff coefficient for the DIRECT-runoff "
               "reference the RDII is compared against (peak = C*i*A)"),

    # -- the storm ------------------------------------------------------------ #
    Param("rainfall_series_in_per_hr", door=doors.USER, optional=True,
          consequence="scenario",
          derived_when_absent=(
              "a uniform design storm of rainfall_depth_in over storm_duration_hr "
              "is used, then dry"),
          desc="An explicit HOURLY hyetograph as bare depths [in, in, ...], "
               "superseding the declared uniform design storm"),
    Param("rainfall_depth_in", door=doors.SCENARIO, default=1.0,
          bounds=(0.0, 100.0), units="in", consequence="scenario",
          desc="Total depth of the uniform design storm"),
    Param("storm_duration_hr", door=doors.SCENARIO, default=1.0,
          bounds=(0.0, 240.0), units="h", consequence="scenario",
          desc="Length of the uniform design storm; it is raised to one timestep "
               "if declared shorter than one"),

    # -- how the answer is computed ------------------------------------------- #
    Param("dt_min", door=doors.CONSTANT, default=15, bounds=(1.0, 60.0),
          units="min", consequence="numerical",
          desc="Timestep the unit hydrographs are sampled on and the deck is "
               "written with; it sets the resolution of the convolution"),
)

#: The RTK method is a lumped sewershed response, and the cross-check deck is a
#: two-junction schematic - nothing this plan consumes is a spatial artifact.
DATA: tuple = ()


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The RTK-RDII recipe. Pure: constructs the plan value, executes nothing.

    The native-SWMM cross-check is DECLARED, not optional. It is one of the two
    acceptance checks the template exists to make, and a run that reported the
    closed form without the engine it is validated against would be reporting an
    unvalidated number as a validated one.
    """
    uhs = dict(R1=p.R1, T1=p.T1, K1=p.K1, R2=p.R2, T2=p.T2, K2=p.K2,
               R3=p.R3, T3=p.T3, K3=p.K3)
    return Plan("swmm_rdii_rtk_unit_hydrograph", "swmm5", (
        FormGate(title="Review the RTK unit-hydrograph RDII scenario"),
        ClosedForm.rtk(
            **uhs, sewershed_area_ac=p.sewershed_area_ac,
            rainfall_depth_in=p.rainfall_depth_in,
            storm_duration_hr=p.storm_duration_hr, dt_min=p.dt_min,
            rainfall_series_in_per_hr=p.rainfall_series_in_per_hr,
        ).named("closed_form"),
        Deck.rtk_rdii(
            uhs=Ref("closed_form.uhs"),
            rain_intensity_in_hr=Ref("closed_form.rain_intensity_in_hr"),
            dt_min=p.dt_min, sewershed_area_ac=p.sewershed_area_ac,
            sim_hours=Ref("closed_form.sim_hours"),
        ).named("deck"),
        Solve.pyswmm(inp_text=Ref("deck.inp_text"), nodes=(NODE,),
                     label="rtk-rdii").named("solve"),
        Metrics.rdii(
            closed_form=Ref("closed_form"), solved=Ref("solve"), node=NODE,
            sewershed_area_ac=p.sewershed_area_ac,
            direct_runoff_coeff=p.direct_runoff_coeff,
        ).named("rdii")
         .chart("rdii_vs_runoff", builder=build_rdii_chart),
    ))


_METADATA = AtomicToolMetadata(
    name="swmm_rdii_rtk_unit_hydrograph",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def swmm_rdii_rtk_unit_hydrograph(
    R1: float | None = None, T1: float | None = None, K1: float | None = None,
    R2: float | None = None, T2: float | None = None, K2: float | None = None,
    R3: float | None = None, T3: float | None = None, K3: float | None = None,
    sewershed_area_ac: float | None = None,
    rainfall_depth_in: float | None = None,
    storm_duration_hr: float | None = None,
    rainfall_series_in_per_hr: list[float] | None = None,
    direct_runoff_coeff: float | None = None,
    dt_min: int | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    supplied = {k: v for k, v in locals().items()
                if k in {q.name for q in PARAMS} and v is not None}
    try:
        p = await resolve_params(PARAMS, supplied)
        result = await interpret(
            plan(p, None), p, PARAMS, DATA,
            input_mode=input_mode, resume=not restart_clean,
        )
    except asyncio.CancelledError:
        raise
    except DeclarativeError as exc:
        logger.warning("swmm_rdii_rtk_unit_hydrograph %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except SwmmStepError as exc:
        logger.warning("swmm_rdii_rtk_unit_hydrograph %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            raise
        logger.exception("swmm_rdii_rtk_unit_hydrograph unexpected failure")
        return {"status": "error", "error_code": "SWMM_RDII_RTK_INTERNAL_ERROR",
                "error_message": str(exc)}

    return {
        "status": "ok",
        "model": "rtk_unit_hydrograph_rdii",
        "citation": ("EPA RTK RDII method (Vallabhaneni et al. 2007, "
                     "EPA/600/R-07/111); Table 7-1 numbers pending NATE"),
        **dict(result.value),
        # The SPEC is the product and the dock is the renderer, so what this
        # reports is what the run BUILT - never a claim about a card it cannot see.
        "chart_specs": sorted(result.charts),
        "notes": result.notes,
    }


_DOC = dict(
    summary="RDII (rainfall-derived inflow and infiltration) at a SEWER NODE vs DIRECT RUNOFF.",
    routing=(
        "THE tool for \"how much RDII enters this sewer\", \"inflow and "
        "infiltration from a storm\", \"RTK unit hydrograph\", \"wet-weather "
        "sanitary-sewer flow\", \"how much of the peak is infiltration vs runoff\". "
        "Builds up to three RTK triangular unit hydrographs from (R,T,K), convolves "
        "the hyetograph to get the RDII inflow hydrograph, and ALSO solves a real "
        "SWMM 5 [HYDROGRAPHS]/[RDII] deck (pyswmm) so the closed form is validated "
        "against the native engine on the same forcing. SCHEMATIC deck - the "
        "product is the RDII-vs-runoff CHART + typed scalars, never a map."
    ),
    not_for=(
        "street or pipe FLOODING extents (`swmm_urban_flood`); groundwater baseflow "
        "to a node (`swmm_aquifer_baseflow_to_node`); importing a real sewer network "
        "(`swmm_network_import`); regional groundwater (`modflow_*`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved param sheet - the three R/T/K unit '
         "hydrographs, the sewershed and the storm - for review/edit before the "
         'convolution and the solve, and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same "
         "invocation left behind and re-runs every step from the top. Default "
         "False resumes at the failed step."),
    ),
    returns=(
        "On success a dict of scalars: `rdii_peak_cfs`, `rdii_volume_cf`, "
        "`rtk_volume_identity_ratio` (closed form / R*P*A, ~1.0), "
        "`swmm_rdii_peak_cfs` + `swmm_vs_closed_form_peak_ratio` (the native "
        "cross-check), `direct_runoff_peak_cfs`, `rdii_fraction_of_total`, "
        "`sum_R`, `flow_routing_error_pct` and `curves`. Narrate those typed "
        "numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)

swmm_rdii_rtk_unit_hydrograph.__doc__ = render_docstring(**_DOC)
swmm_rdii_rtk_unit_hydrograph.routing_doc = render_docstring(**_DOC, view="routing")
