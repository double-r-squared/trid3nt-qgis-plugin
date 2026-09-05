"""Engine template ``telemac3d_stratified_flow`` - TELEMAC-3D vertical structure.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/runtime/workflow.py``); the 3D mechanism is the TELEMAC facade's
open-water front (``authoring/open_water.py`` + ``authoring/stratified.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: what a depth-averaged model cannot see. TELEMAC-3D solves the
three-dimensional (hydrostatic or non-hydrostatic) equations with active-tracer
baroclinic density coupling over sigma layers, so the answer is the VERTICAL
structure itself:

  * ``stratification``    - a warm surface layer over a cold bottom either keeps
                            its thermocline (calm) or is mixed away (wind). The
                            metric is the top-to-bottom difference that SURVIVES.
                            The run has NO surface heat exchange, so heat is
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

from trid3nt_server.workflows.runtime import (
    Forcing,
    FormGate,
    Physics,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.stratified_flow.declarations import (
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.stratified_flow.flow_mode import flow_mode
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_profile_chart", "plan",
           "telemac3d_stratified_flow"]

#: A lake basin runs wider than it is tall in degrees, so a geocoded place is
#: squared off asymmetrically (~0.35 deg of longitude, ~0.25 of latitude).
_BASIN_HALF_DEG = (0.35, 0.25)

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"


#: The BED, as declared reference data. Sampling it inside the solver
#: container would bypass the emit, cache, provenance and retry the router
#: gives every other fetch. Declaring it here puts the bathymetry on the
#: canvas as a continuous surface and lets the worker run with no network:
#: the producer fetches, the manifest stages the raster into the run
#: directory, and the builder reads a file.
#: ``px_per_deg`` is THIS builder's sample lattice - the grid its nodes are read
#: against - so it travels from the template rather than being a router default.
class DATA:
    bed = tool(f"{_AUTHORING}.open_water.fetch_domain_bed",
               bathy_source=P.bathy_source,
               mode=P.flow_mode,
               real_bed_modes=("stratification", "wind_circulation"),
               px_per_deg=1200.0, max_px_per_side=2000)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / DATA.<row> / Ref) that the
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
                  bathy_source=P.bathy_source,
                  bed=DATA.bed)

#: The MESH RECIPE, frozen at declaration and building nothing at import. An
#: open-water run solves on a uniform lattice over the acquired AOI, so the recipe
#: is the three agnostic params and the mesher's own near-empty default program:
#: a lattice at one size word, with no bed of its own (the solver stages that).
MESH = tool.build_mesh(
    mesher="reg_grid",
    kind="structured_grid",
    extent=Ref("aoi"),
    resolution_m=P.target_resolution_m,
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The 3D-structure recipe. Pure and STATIC: it reads no value, it names them.

    The form gate comes FIRST because the whole answer is prescribed: the warm and
    cold temperatures ARE the initial condition, and the wind decides whether the
    difference between them survives. Reviewing those after the solve would be
    reviewing the answer.
    """
    return [
        FormGate(title="Review the prescribed column and the wind"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox,
                            aoi_half_deg=_BASIN_HALF_DEG, aoi_name="aoi",
                            code_prefix="TELEMAC3D"),
        ops.author(mesh=MESH, physics=PHYSICS,
                   forcing=Forcing()),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=Forcing())
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

    The stratification run exchanges NO heat with the atmosphere, so the two lines
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
            + ("There is no heat exchange in this run - heat is CONSERVED, so the "
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
