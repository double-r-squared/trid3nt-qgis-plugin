"""Engine template ``telemac_river_dye`` - TELEMAC-2D river surface-tracer engine.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/runtime/workflow.py``); the reach mechanism is the TELEMAC facade
(``workflows/telemac/workflow.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how far a DYE / TRACER / CONTAMINANT / oil / sewage / sediment
spill travels DOWNSTREAM in a river reach, and what its peak concentration is;
OR where the bed SCOURS and re-deposits under a flood (GAIA erodible-bed
morphodynamics). TELEMAC-2D shallow water over a real reach, with the plume or
the bed evolution animated from the native time-stepped mesh.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    DrawGate,
    Forcing,
    FormGate,
    Physics,
    Ref,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.river_dye.coercions import release_points
from trid3nt_server.workflows.telemac.river_dye.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.helpers.forcing import event_time
from trid3nt_server.workflows.telemac.helpers.reach import MeshCoverage
from trid3nt_server.workflows.telemac.helpers.substance import substance_class
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_dye_chart", "plan", "telemac_river_dye"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


#: The reach pipeline's data, one row per artifact, in the order the chain reads
#: them. The carrier discharge is a STEP rather than a row: it reads the resolved
#: mid-reach seed, which is a step result and not something a producer declaration
#: can name.
class DATA:
    rivers = tool(f"{_HELPERS}.reach.fetch_reach_flowline",
                  prefetched=P.river_geometry_uri)
    # THE REACH, narrowed by CHAINING tools rather than by a mesher that grew a
    # corridor of its own. The navigated mainstem names the stretch, its two ends
    # name where the stretch stops, and the cut through the MAPPED banks is the
    # domain - so the two end faces are the transects the inflow and the outflow
    # are prescribed on, measured off real geometry rather than a ribbon.
    centerline = tool("fetch_nhdplus_nldi_navigate",
                      seed_point=[Ref("seed.lon"), Ref("seed.lat")],
                      direction="DM",
                      distance_km=P.reach_length_km)
    ends = tool("endpoints", line=centerline)
    # The QUERY WINDOW the banks are asked for: the centerline's own extent grown
    # by a stated distance, because the water that belongs to this reach reaches
    # past the line - a far channel behind a mid-river island is three km off it
    # and is still the same river. The pad widens the QUESTION, never the meshed
    # domain: what returns is real mapped water, and the section cut below keeps
    # only the stretch between the reach's two ends.
    window = tool("compute_layer_bounds", layer_uri=centerline, pad_m=3000.0,
                  fit_map=False)
    water = tool("fetch_nhd_area_water", bbox=Ref("window.bbox"))
    # HOW MUCH of the reach the returned polygons actually map, measured before
    # the cut so an unmapped reach refuses on its own cause instead of arriving
    # at the section as an empty geometry.
    mapped_water = tool(f"{_HELPERS}.reach.measure_water_coverage",
                        water=water, centerline=centerline)
    reach_polygon = tool("section", polygon=mapped_water,
                         between=Ref("ends.between"))
    # THE SUBSTITUTION, declared where a reader can see it. A bed is TOPOBATHY -
    # the channel bottom - and no topobathy survey covers an inland reach, so
    # this row is a surface DEM and the recipe below says so by painting the bed
    # from it BY NAME. The consequence travels with it: a surface measures the
    # water top, so the modelled channel is shallower than the real one and the
    # journal names this row as what the bed came from. GLO-30 is asked for on
    # its OWN 1-arcsecond lattice, so the raster the nodes are sampled from
    # carries the source pixels rather than a resample of them.
    dem = tool("fetch_copernicus_dem", bbox=Ref("window.bbox"), px_per_deg=3600.0,
               purpose="river bed elevation")
    # The cadence and units the sheet receives, stated rather than assumed: the
    # producer answers in daily rates, so this asks for no interpolation - and a
    # sub-daily target would refuse here instead of manufacturing a storm shape
    # gridMET never reported.
    rain = tool(f"{_HELPERS}.forcing.resolve_rain_forcing",
                rainfall_mm_per_day=P.rainfall_mm_per_day,
                evaporation_mm_per_day=P.evaporation_mm_per_day,
                gridmet_window=P.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / DATA.<row> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

#: The reach's own extent rides HERE rather than on the mesh ask: the mesher is
#: handed a measured polygon, while the sheet still states the stretch it asked for.
PHYSICS = Physics(
    "tracer",
    reach_length_km=P.reach_length_km,
    substance=P.substance, release_coords=P.release_coords,
    sim_duration_s=P.sim_duration_s, continue_from=P.continue_from,
    spill_fraction=P.spill_fraction, spill_duration_s=P.spill_duration_s,
    dye_concentration_mgl=P.dye_concentration_mgl, source_q_m3s=P.source_q_m3s,
    output_interval_min=P.output_interval_min,
    friction_coefficient=P.friction_coefficient, friction_law=P.friction_law,
    velocity_diffusivity=P.velocity_diffusivity,
    tracer_diffusivity=P.tracer_diffusivity, erodible_bed=P.erodible_bed,
    sediment_gradation=P.sediment_gradation, dredging=P.dredging,
    decay_half_life_hours=P.decay_half_life_hours,
    decay_rate_per_day=P.decay_rate_per_day, sediment_type=P.sediment_type,
    grain_size_um=P.grain_size_um, bed_thickness_m=P.bed_thickness_m,
    bedload_formula=P.bedload_formula,
    morphological_factor=P.morphological_factor, dredge_mode=P.dredge_mode,
    dredge_volume_m3=P.dredge_volume_m3, dredge_disposal=P.dredge_disposal,
    dredge_crit_depth_m=P.dredge_crit_depth_m,
    dredge_dig_depth_m=P.dredge_dig_depth_m,
    dredge_bank_offset_m=P.dredge_bank_offset_m,
)

FORCING = Forcing(carrier=Ref("carrier_discharge"), rain=DATA.rain,
                  wind_speed_mps=P.wind_speed_mps,
                  wind_direction_deg=P.wind_direction_deg)

#: The MESH RECIPE, frozen at declaration and building nothing at import. Three
#: agnostic params and the ordered program that produces the mesh. The extent is
#: the CHAIN's product - the stretch of mapped water the section cut between the
#: centerline's two ends - so the mesher triangulates a domain other tools
#: measured rather than growing a corridor of its own. The ops are oceanmesh's
#: own clean passes under its own names, then the two things we impose: the bed,
#: painted from the substitution this template declared above and named in the
#: journal, and the roles, prescribed across the two end transects the section
#: cut.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("reach_polygon"),
    resolution_m=P.mesh_resolution_m,
    ops=[
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("laplacian2"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source=DATA.dem, interp="nearest"),
        mesh_op("set_boundary_roles",
                inflow=Ref("reach_polygon.face_start"),
                outflow=Ref("reach_polygon.face_end")),
    ],
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The river-tracer recipe. Pure and STATIC: it reads no value, it names them.

    The gates come FIRST so every step and every producer downstream of them runs
    on the approved sheet - a step that had already consumed a value the form can
    revise would be exactly the contradiction the review exists to prevent.
    """
    return [
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=DATA.rivers,
                            discharge=P.discharge_m3s, event_time=P.event_time,
                            seed_coords=P.reach_seed_coords),
        MeshStep.build(mesh=MESH, name=Ref("reach")).named("mesh"),
        MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("dye_concentration", builder=build_dye_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "max_deposition_mm", "max_scour_mm", "deposited_mass_kg",
          "deposit_fraction", "sediment_surface_d50_range_um", "mesh_size_m")


def _bed_of(result: Any) -> str:
    """The bed the run's mesh was painted from, off the layer's own provenance."""
    for row in getattr(result, "synthetic_inputs", None) or ():
        if getattr(row, "param", None) == "mesh_bed" and row.value:
            return str(row.value)
    return "unrecorded bed"


def build_dye_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The plume's concentration HISTORY: one point per frame the solver wrote.

    Every point is the reach maximum at that output time, measured off the
    postprocessed field, so the curve is the arrival, the peak and the flush-out
    as the run produced them. ``None`` when the run persisted no history, which
    is the honest "there was no curve to draw".
    """
    times = getattr(result, "dye_curve_time_s", None)
    values = getattr(result, "dye_curve_cmax_mgl", None)
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if not times or not values or cmax is None or peak_t is None:
        return None
    from trid3nt_server.emission.styles import preset_units
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    substance = params.get("substance") or "dye"
    # The UNITS come from the style contract, so the chart's axis and the layer's
    # legend cannot disagree about what this field is measured in.
    units = preset_units("continuous_plume_concentration") or "mg/L"
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": float(t), "dye_mgl": float(c)}
                                for t, c in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"{str(substance).capitalize()} concentration ({units})"},
            },
        },
        title=f"Reach maximum {substance} concentration - {where}",
        caption=(f"The highest {substance} concentration anywhere in the reach at "
                 f"each of {len(times)} output times; peaks at {float(cmax):.3g} "
                 f"{units}, {float(peak_t):.0f} s after release. Bed: "
                 f"{_bed_of(result)}."),
    )


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening plume gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long reach WITHIN
#: this declaration, and the effective edge stays >= 2 cells across the channel.
_TELEMAC_RIVER_DYE_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long reach is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

#: The mesh gate this template stops at is the STANDARD one - the mesh step opens
#: a session, presents the built reach as an editable layer with its probes, and
#: takes every edit action the ``om2d`` mesher registers - so this template
#: declares no solver gate of its own.
_TELEMAC_RIVER_DYE_METADATA = AtomicToolMetadata(
    name="telemac_river_dye",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_RIVER_DYE_RES_SPEC,),
)


#: Wire ALIASES the model uses for values PARAMS already declares.
#: ``release_points`` folds them into the declared params before any door.
_EXTRA_ARGS: tuple[tuple[str, Any], ...] = (
    ("contaminant", str | None),
    ("release_lon", float | None),
    ("release_lat", float | None),
    ("spill_location_latlon", str | None),
)


telemac_river_dye = register_workflow(
    TelemacWorkflow, _TELEMAC_RIVER_DYE_METADATA, PARAMS, plan,
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    # The dye maximum is the canonical peak class: measured 6x LOW on the coarse
    # mesh, because a concentration peak lives inside one element. How far the
    # plume REACHED is a front location and moves with it.
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location"),
                 ("max_deposition_mm", "peak"),
                 ("max_scour_mm", "peak")),
    coerce=(
        location_or_bbox("telemac_river_dye", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'dye spill in the river "
                              "near <place>', pass location='<place>'."),
        release_points,
        event_time(),
        substance_class(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
    extra_args=_EXTRA_ARGS,
)
