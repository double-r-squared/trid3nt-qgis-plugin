"""Engine template ``telemac3d_stratified_flow`` - TELEMAC-3D vertical structure.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/lib/workflow.py``); the 3D mechanism is the TELEMAC facade's
open-water front (``steps/open_water.py`` + ``steps/stratified.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: what a depth-averaged model cannot see. TELEMAC-3D solves the
three-dimensional (hydrostatic or non-hydrostatic) equations with active-tracer
baroclinic density coupling over sigma layers, so the answer is the VERTICAL
structure itself:

  * ``stratification``    - a warm surface layer over a cold bottom either keeps
                            its thermocline (calm) or is mixed away (wind). The
                            metric is the top-to-bottom difference that SURVIVES.
                            The deck has NO surface heat exchange, so heat is
                            CONSERVED: a falling surface temperature is the warm
                            layer MIXING DOWNWARD, never the lake cooling.
  * ``wind_circulation``  - a steady wind drives surface water downwind and a
                            return flow at depth; the depth average is ~0, which
                            is exactly why a 2D model reports nothing.
  * ``salt_wedge``        - a dense saline column drives a bottom gravity current
                            at the Benjamin front speed. Analytic V&V, idealized.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    Forcing,
    FormGate,
    MeshPolicy,
    P,
    Physics,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.stratified_flow.declarations import (
    DOC,
    PARAMS,
)
from trid3nt_server.workflows.telemac.stratified_flow.flow_mode import flow_mode
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_profile_chart", "plan",
           "telemac3d_stratified_flow"]

#: A lake basin runs wider than it is tall in degrees, so a geocoded place is
#: squared off asymmetrically (~0.35 deg of longitude, ~0.25 of latitude).
_BASIN_HALF_DEG = (0.35, 0.25)


#: NO declared Data. The lake bed is sampled INSIDE the solver container from the
#: NOAA lake-datum grids; that is the in-worker-fetch migration's business, not a
#: gap in this declaration.
DATA = ()


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / D.<data> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("stratified_3d",
                  flow_mode=P.flow_mode,
                  wind_speed_mps=P.wind_speed_mps,
                  wind_direction_deg=P.wind_direction_deg,
                  warm_temp_c=P.warm_temp_c, cold_temp_c=P.cold_temp_c,
                  thermocline_depth_m=P.thermocline_depth_m,
                  non_hydrostatic=P.non_hydrostatic, nplan=P.nplan,
                  sim_duration_hours=P.sim_duration_hours,
                  bathy_source=P.bathy_source)

MESH = MeshPolicy(resolution=None, target_edge_m=P.target_resolution_m)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The 3D-structure recipe. Pure and STATIC: it reads no value, it names them.

    The form gate comes FIRST because the whole answer is prescribed: the warm and
    cold temperatures ARE the initial condition, and the wind decides whether the
    difference between them survives. Reviewing those after the solve would be
    reviewing the answer.
    """
    return [
        FormGate(title="Review the prescribed column and the wind"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, shape="open_water",
                            aoi_half_deg=_BASIN_HALF_DEG, aoi_name="aoi",
                            code_prefix="TELEMAC3D"),
        ops.author(mesh=ops.build_mesh(Ref("aoi"), MESH), physics=PHYSICS,
                   forcing=Forcing()),
        ops.solver_spec(compute_class=P.compute_class, physics=PHYSICS),
        ops.read_results(Ref("solve"), physics=PHYSICS, forcing=Forcing())
           .chart("vertical_profile", builder=build_profile_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("stratification_metric", "stratification_dt", "flow_mode",
          "variable_label", "variable_units", "nplan", "wind_speed_mps",
          "mesh_size_m", "profile_sigma", "profile_values",
          "profile_values_initial")


def build_profile_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The VERTICAL profile chart SPEC: what the column started as, and what survived.

    Two lines against sigma (0 = bed, 1 = surface) - the initial condition and the
    final state - because the 3D answer IS the difference between them, and a map
    of the surface alone carries no depth at all. ``None`` when the run measured no
    profile, which is the honest "there is nothing to plot".

    The stratification deck exchanges NO heat with the atmosphere, so the two lines
    enclose the same heat: the caption must read the change as REDISTRIBUTION
    (mixing), never as the lake losing heat.
    """
    sigma = getattr(result, "profile_sigma", None)
    final = getattr(result, "profile_values", None)
    if not sigma or not final or len(sigma) != len(final):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    units = getattr(result, "variable_units", None) or ""
    label = getattr(result, "variable_label", None) or "Field"
    initial = getattr(result, "profile_values_initial", None)
    values = [{"sigma": float(sigma[i]), "v": float(final[i]), "state": "final"}
              for i in range(len(sigma))]
    if initial and len(initial) == len(sigma):
        values += [{"sigma": float(sigma[i]), "v": float(initial[i]),
                    "state": "initial"} for i in range(len(sigma))]

    metric = getattr(result, "stratification_metric", None)
    where = params.get("location")
    title = (f"Vertical profile - {where}" if where
             else (getattr(result, "name", None) or "Vertical profile"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": values},
            "encoding": {
                "x": {"field": "v", "type": "quantitative",
                      "title": f"{label} ({units})" if units else label},
                "y": {"field": "sigma", "type": "quantitative",
                      "title": "Sigma (0 = bed, 1 = surface)"},
                "color": {"field": "state", "type": "nominal", "title": None},
            },
        },
        title=title,
        caption=(
            (f"Top-to-bottom difference surviving the run: {float(metric):.4g} "
             f"{units}. " if metric is not None else "")
            + "The two lines are the PRESCRIBED initial column and the solved final "
              "one; their separation is what a depth-averaged model cannot show. "
            + ("There is no heat exchange in this deck - heat is CONSERVED, so the "
               "surface falling and the depths rising is the warm layer MIXING "
               "DOWNWARD, not the lake cooling. "
               if getattr(result, "flow_mode", None) == "stratification" else "")
            + "3D screening, not a calibrated study."
        ),
    )


_TELEMAC3D_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=50.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / idealized basin",
    constraint_source="solver",
    rationale=(
        "target HORIZONTAL grid node spacing; the vertical resolution is nplan. A "
        "large lake is coarsened under the node budget (self-labeled), and a 3D "
        "screening field gains nothing finer than the bathymetry"
    ),
)

_TELEMAC3D_METADATA = AtomicToolMetadata(
    name="telemac3d_stratified_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC3D_RES_SPEC,),
)


telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("wind_speed_mps", "wind_note"),
                ("bathy_source", "bathy_note"),
                ("target_resolution_m", "target_resolution_note")),
    # The surface-to-bottom temperature difference is read ACROSS the thermocline,
    # the steepest gradient in the domain: measured -25% on the coarse mesh.
    sensitivity=(("stratification_dt", "gradient"),),
    coerce=(
        location_or_bbox("telemac3d_stratified_flow", code_prefix="TELEMAC3D",
                         hint="For a natural prompt like 'does <lake> stratify', "
                              "pass location='<lake>'."),
        flow_mode(),
        compute_class(),
    ),
    doc=DOC,
)
