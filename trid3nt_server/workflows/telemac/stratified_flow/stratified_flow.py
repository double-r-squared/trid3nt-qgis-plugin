"""Engine template ``telemac3d_stratified_flow`` - TELEMAC-3D vertical structure.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the 3D mechanism is the TELEMAC
facade's open-water front (``steps/open_water.py`` + ``steps/stratified.py``).
See ``docs/design/declarative-workflows.md``.

THE QUESTION: what a depth-averaged model cannot see. TELEMAC-3D solves the
three-dimensional (hydrostatic or non-hydrostatic) equations with active-tracer
baroclinic density coupling over sigma layers, so the answer is the VERTICAL
structure itself:

  * ``stratification``    - a warm surface layer over a cold bottom either keeps
                            its thermocline (calm) or is mixed away (wind). The
                            metric is the top-to-bottom difference that SURVIVES.
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
    Param,
    Physics,
    Ref,
    doors,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.stratified_flow.flow_mode import flow_mode
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_profile_chart", "plan",
           "telemac3d_stratified_flow"]

#: A lake basin runs wider than it is tall in degrees, so a geocoded place is
#: squared off asymmetrically (~0.35 deg of longitude, ~0.25 of latitude).
_BASIN_HALF_DEG = (0.35, 0.25)


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Lake or basin place near the AOI (e.g. 'Lake Superior'), geocoded"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - deep open "
               "water inside a lake for the real-bathymetry path"),
    Param("flow_mode", door=doors.QUESTION, default="stratification",
          consequence="scenario",
          desc="Which 3D question: stratification (does the thermocline survive) | "
               "wind_circulation (surface downwind, return flow at depth) | "
               "salt_wedge (a density-driven bottom gravity current)"),

    # -- the column --------------------------------------------------------- #
    Param("warm_temp_c", door=doors.SCENARIO, default=25.0, bounds=(-2.0, 40.0),
          units="C", consequence="physics",
          desc="Epilimnion (warm surface layer) temperature - a PRESCRIBED demo "
               "column, since no met-forcing fetcher exists yet"),
    Param("cold_temp_c", door=doors.SCENARIO, default=15.0, bounds=(-2.0, 40.0),
          units="C", consequence="physics",
          desc="Hypolimnion (cold bottom layer) temperature; the initial "
               "top-to-bottom difference is what the run either keeps or mixes away"),
    Param("thermocline_depth_m", door=doors.SCENARIO, default=8.0,
          bounds=(0.5, 200.0), units="m", consequence="physics",
          desc="Depth of the thermocline below the surface"),
    Param("wind_speed_mps", door=doors.SCENARIO, default=0.0, bounds=(0.0, 40.0),
          units="m/s", consequence="physics",
          desc="Sustained wind speed; 0 is CALM - the half of the pair in which the "
               "thermocline persists - and a nonzero value mixes the column and "
               "drives the circulation"),
    Param("wind_direction_deg", door=doors.SCENARIO, default=270.0,
          bounds=(0.0, 360.0), units="deg", consequence="scenario",
          desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W)"),

    # -- the numerics (the advanced fold) ----------------------------------- #
    Param("nplan", door=doors.SCENARIO, default=13, bounds=(5.0, 30.0),
          type=int, consequence="numerical",
          desc="Number of vertical sigma levels - the degree of freedom a 2D model "
               "does not have, so it is the resolution lever that matters here"),
    Param("non_hydrostatic", door=doors.USER, optional=True, type=bool,
          consequence="physics",
          derived_when_absent="the hydrostatic solver runs",
          desc="Force the non-hydrostatic solver - the dam-break-3D fidelity rung a "
               "salt wedge's front needs"),
    Param("bathy_source", door=doors.SCENARIO, default="auto",
          consequence="physics",
          desc="Bed source: auto (a Great Lakes AOI samples the real NOAA lake-datum "
               "bathymetry, anywhere else runs the idealized basin) | noaa_greatlakes "
               "| idealized"),
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(50.0, 20000.0), units="m", consequence="numerical",
          derived_when_absent=(
              "the horizontal grid is laid at the labeled default spacing - 2000 m "
              "over a real lake, 250 m in the idealized basin"),
          desc="Explicit HORIZONTAL grid node spacing; the vertical is nplan"),
    Param("sim_duration_hours", door=doors.SCENARIO, default=5.0, bounds=(1.0, 24.0),
          units="h", consequence="numerical",
          desc="Simulated duration - long enough for the column to settle or mix"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: NO declared Data. The lake bed is sampled INSIDE the solver container from the
#: NOAA lake-datum grids; that is the in-worker-fetch migration's business, not a
#: gap in this declaration.
DATA = ()


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The 3D-structure recipe. Pure: constructs the plan value, executes nothing.

    The form gate comes FIRST because the whole answer is prescribed: the warm and
    cold temperatures ARE the initial condition, and the wind decides whether the
    difference between them survives. Reviewing those after the solve would be
    reviewing the answer.
    """
    physics = Physics("stratified_3d",
                      flow_mode=p.flow_mode,
                      wind_speed_mps=p.wind_speed_mps,
                      wind_direction_deg=p.wind_direction_deg,
                      warm_temp_c=p.warm_temp_c, cold_temp_c=p.cold_temp_c,
                      thermocline_depth_m=p.thermocline_depth_m,
                      non_hydrostatic=p.non_hydrostatic, nplan=p.nplan,
                      sim_duration_hours=p.sim_duration_hours,
                      bathy_source=p.bathy_source)
    mesh = ops.build_mesh(Ref("aoi"),
                          MeshPolicy(resolution=None,
                                     target_edge_m=p.target_resolution_m))
    return [
        FormGate(title="Review the prescribed column and the wind"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, shape="open_water",
                            aoi_half_deg=_BASIN_HALF_DEG, aoi_name="aoi",
                            code_prefix="TELEMAC3D"),
        ops.author(mesh=mesh, physics=physics, forcing=Forcing()),
        ops.solver_spec(compute_class=p.compute_class, physics=physics),
        ops.read_results(Ref("solve"), physics=physics, forcing=Forcing())
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
              "3D screening, not a calibrated study."
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


_DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a water body a 2D depth-averaged model cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"epilimnion over hypolimnion\", "
        "\"wind-driven vertical circulation / return flow in a lake\", \"surface-vs-"
        "bottom current structure\", \"salt wedge / salinity intrusion in an estuary\", "
        "\"density-driven bottom gravity current\". TELEMAC-3D with active-tracer "
        "baroclinic coupling over sigma layers. THREE question classes via "
        "`flow_mode`: `stratification` (default), `wind_circulation`, `salt_wedge`. "
        "Returns the SURFACE field map with a BOTTOM companion beside it and the "
        "vertical profile. Supply a lake `location` OR a `bbox`."
    ),
    not_for=(
        "a 2D river dye/contaminant plume (`telemac_river_dye`); inundation DEPTH "
        "(`sfincs_flood` / `geoclaw_inundation`); coastal storm-tide flooding "
        "(`coastal_tidal_surge`); the surface wave field (`tomawac_wave_field` / "
        "`artemis_harbor_agitation`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved column and wind for review/edit before '
         'the solve and WAITS; "auto" (session default) proceeds with every '
         "assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step."),
    ),
    returns=(
        "On success a `Telemac3dLayerURI` (a `LayerURI` subtype) - the SURFACE-layer "
        "field COG, with the BOTTOM companion emitted beside it. It carries "
        "`stratification_metric` / `stratification_dt` / `flow_mode` / `nplan` and "
        "the vertical `profile_*` arrays; narrate those typed numbers. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)


telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("wind_speed_mps", "wind_note"),
                ("bathy_source", "bathy_note")),
    coerce=(
        location_or_bbox("telemac3d_stratified_flow", code_prefix="TELEMAC3D",
                         hint="For a natural prompt like 'does <lake> stratify', "
                              "pass location='<lake>'."),
        flow_mode(),
        compute_class(),
    ),
    doc=_DOC,
)
