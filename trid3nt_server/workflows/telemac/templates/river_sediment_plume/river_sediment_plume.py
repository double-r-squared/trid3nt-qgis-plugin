"""Engine template ``telemac_river_sediment_plume`` - a settling plume in a reach.

TELEMAC-2D coupled with GAIA over a real reach: ONE settling class over a bed
with NO stock at all, so nothing erodes and only what was injected can deposit."""

from __future__ import annotations

from trid3nt_contracts.telemac_contracts import (
    TELEMAC_BED_EVOLUTION_STYLE,
    TELEMAC_SEDIMENT_CONCENTRATION_STYLE,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.inputs import point_arg, user_input
from trid3nt_server.inputs.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.modules import (
    GAIA,
    T2D,
    field,
    mass_balance,
    max_over_time,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.gaia import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Rain,
    Release,
    TracerNames,
    Wind,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.river_sediment_plume.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_river_sediment_plume"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"
_REACH = "trid3nt_server.workflows.telemac.templates.reach"
_ENGINE = "trid3nt_server.workflows.telemac.engine"

#: The names the run directory holds this run's files under. They are the deck's
#: own STEERING / GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, so the
#: directory reads as the record of the run it is.
_STEERING_FILE = "t2d_river.cas"
_GEOMETRY = "river.slf"
_BOUNDARY = "river.cli"
_RESULT = "r2d_river.slf"

#: How far past the centerline the mapped banks are ASKED for. The water that
#: belongs to this reach reaches past the line - a far channel behind a mid-river
#: island is three km off it and is still the same river - and the pad widens the
#: QUESTION, never the meshed domain: the section cut below keeps only the
#: stretch between the reach's two ends.
_BANK_QUERY_PAD_M = 3000.0


class DATA:
    """The reach chain, one row per artifact, in the order it is read, and the
    rain this question adds to it. The carrier discharge is a STEP, because it
    reads the resolved seed."""

    rivers = tool(f"{_REACH}.fetch_reach_flowline",
                  prefetched=ParamRef("river_geometry_uri"))
    # THE REACH, narrowed by CHAINING tools rather than by a mesher that grew a
    # corridor of its own. The navigated mainstem names the stretch, its two ends
    # name where the stretch stops, and the cut through the MAPPED banks is the
    # domain - so the two end faces are the transects the inflow and the outflow
    # are prescribed on, measured off real geometry rather than a ribbon.
    centerline = tool("fetch_nhdplus_nldi_navigate",
                      seed_point=[Ref("seed.lon"), Ref("seed.lat")],
                      direction="DM",
                      distance_km=ParamRef("reach_length_km"))
    ends = tool("endpoints", line=centerline)
    window = tool("compute_layer_bounds", layer_uri=centerline,
                  pad_m=_BANK_QUERY_PAD_M, fit_map=False)
    water = tool("fetch_nhd_area_water", bbox=Ref("window.bbox"))
    # HOW MUCH of the reach the returned polygons actually map, measured before
    # the cut so an unmapped reach refuses on its own cause instead of arriving
    # at the section as an empty geometry.
    mapped_water = tool(f"{_REACH}.measure_water_coverage",
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
    # The cadence and units the run receives, stated rather than assumed: the
    # producer answers in daily rates, so this asks for no interpolation - and a
    # sub-daily target would refuse here instead of manufacturing a storm shape
    # gridMET never reported.
    rain = tool(f"{_REACH}.resolve_rain_forcing",
                rainfall_mm_per_day=P.rainfall_mm_per_day,
                evaporation_mm_per_day=P.evaporation_mm_per_day,
                gridmet_window=P.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: extent is the CHAIN's product - the stretch of mapped water the section cut
#: between the centerline's two ends - so the mesher triangulates a domain other
#: tools measured rather than growing a corridor of its own. The ops are
#: oceanmesh's own clean passes under its own names, then the two things we
#: impose: the bed, painted from the substitution declared above and named in the
#: journal, and the roles, prescribed across the two end transects the section cut.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("reach_polygon"),
    resolution_m=ParamRef("mesh_resolution_m"),
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


class STEERING(T2D):
    """The deck: a river, and ONE settling class released into it."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    # The step the reach is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for.
    TIME_STEP = Ref("settled.time_step_s")
    LISTING_PRINTOUT_PERIOD = 500

    # The run OPENS at the derived normal depth, laid bed-parallel. Not a
    # constant elevation at the outflow stage: the stage is derived only where
    # the reach FALLS, so a horizontal surface at the outlet's level leaves every
    # node upstream of it dry - the flowrate face among them - and the engine
    # refuses a discharge it has no water to impose.
    INITIAL_CONDITIONS = "CONSTANT DEPTH"
    INITIAL_DEPTH = Ref("settled.depth_m")

    # The roughness the outflow stage was DERIVED at, written back out as the
    # roughness the run is solved at. One number, stated once: a stage derived at
    # one and written at another is a level the run never sits at.
    LAW_OF_BOTTOM_FRICTION = Ref("settled.friction_law")
    FRICTION_COEFFICIENT = Ref("settled.friction_coefficient")

    # The advection of momentum and depth, and the SUPG the reach is stable
    # under. The tracer advects under the engine's own scheme and diffusivity.
    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 500
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6

    # The engine accounts for its own water volume and prints one flux per liquid
    # boundary. That is the only honest check that the level prescribed at a
    # boundary reached it: a server-side integration of the depth and velocity
    # fields reads near zero at a prescribed-depth face, where the boundary values
    # are clamped after the flux was computed.
    MASS_BALANCE = True

    # A coupled run drives the module's own launcher whole rather than the
    # stepped arm, so it writes no restart record and cannot be continued.
    #: GAIA's suspended class arrives at the carrier as a SECOND tracer, so the
    #: file has to output it and every array sized to the tracer count carries
    #: two values - the visible half of what this fork costs.
    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1,T2"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    #: The marker is called what the user called the release point, when a
    #: picked or named point came; the template's own name stands otherwise.
    tracer_names = TracerNames(names=["MARKER          MG/L"],
                               named_by=Ref("release.name"))
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: TWO values on every liquid boundary - the carrier's own tracer and the
    #: suspended class behind it - or the solver refuses for want of values.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0, 0.0])

    #: The marker the settling is watched against, released as a finite pulse at
    #: the same source the class is injected at.
    releases = [Release(at=Ref("release.at"), q=P.source_q_m3s,
                        tracers=[P.sediment_concentration_mgl],
                        window_s=P.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The settling class itself: zero initial thickness, so nothing erodes and
    #: only the injected pulse deposits. Zyserman-Fredsoe, out of the suspension
    #: formulas GAIA offers, is the one written for the fine non-cohesive class
    #: this plume carries; scheme 1 is the character-of-the-flow scheme a pulse
    #: advected over a bed with no stock needs, where the dictionary's own
    #: upwind pair is for a resident concentration field. The listing's own
    #: sediment balance is what the net bed mass is read off.
    coupling = [GAIA.suspended(geometry=_GEOMETRY, boundary=_BOUNDARY,
                               d50_um=P.grain_size_um,
                               concentration_mgl=P.sediment_concentration_mgl,
                               transport_formula=3, advection_scheme=[1],
                               printouts="B,E", mass_balance=True)]

    wind = Wind(speed_mps=P.wind_speed_mps, from_deg=P.wind_direction_deg)
    rain = Rain(mm_per_day=Ref("rain.mm_per_day"), tracers=2)


#: What the solved run is read for: the suspended class over time as the
#: animation, its envelope as the map, its reach-wide history as the chart, and
#: what settled onto the bed off GAIA's own result.
OUTPUTS = [
    field("T2", t="every").animate(),
    max_over_time("T2").layer(style=TELEMAC_SEDIMENT_CONCENTRATION_STYLE),
    series("T2").chart(),
    field("E", t=-1, module="gaia").layer(style=TELEMAC_BED_EVOLUTION_STYLE),
]
CAPTIONS = {"T2": "suspended sediment concentration", "E": "bed evolution"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above; the deposited fraction is the listing's
#: deposited mass over the mass the sheet says the pulse put in.
ANSWER = {
    "suspended_cmax": max_over_time("T2").measure("max"),
    "suspended_peak_time_s": max_over_time("T2").measure("t_max"),
    "plume_reach_m": field("T2", t="every").measure("travel_m"),
    "active_frames": field("T2", t="every").measure("active_frames"),
    "bed_evolution_max_m": field("E", t=-1, module="gaia").measure("max"),
    "net_bed_mass_kg": mass_balance(module="gaia").measure("sediment_net_bed_mass_kg"),
    "deposit_fraction": mass_balance(module="gaia").measure(
        "sediment_deposited_mass_kg").over(P.injected_mass_kg),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long reach WITHIN
#: this declaration, and the effective edge stays >= 2 cells across the channel.
_RES_SPEC = ResolutionSpec(
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

_METADATA = AtomicToolMetadata(
    name="telemac_river_sediment_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_river_sediment_plume = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        # A release named up front also names which stretch to model: the one
        # centerline is navigated from it.
        domain=(reach.Geocode(location=P.location, bbox=P.bbox),
                reach.ReachSeed(reach=Ref("reach"), rivers=DATA.rivers,
                                supplied=P.release),
                reach.CarrierDischarge(seed=Ref("seed"), explicit=P.discharge_m3s,
                                       event_time=P.event_time)),
        mesh=MESH, mesh_on="reach",
        produce=(reach.MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),
                 # WHERE the source enters the water, settled against the
                 # accepted mesh before the sheet reads it.
                 Step(runner=f"{_AUTHORING}.assembler.settle_release",
                      stage="author",
                      kwargs={"point": P.release, "mesh": Ref("mesh"),
                              "centerline": DATA.centerline, "seed": Ref("seed"),
                              "reach": Ref("reach"), "fraction": P.spill_fraction,
                              "label": "Release point"}
                      ).named("release")),
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_reach", stage="author",
                    kwargs={"reach": Ref("reach"), "seed": Ref("seed"),
                            "mesh": Ref("mesh"), "centerline": DATA.centerline,
                            "carrier_discharge": Ref("carrier_discharge"),
                            "sim_duration_s": P.sim_duration_s,
                            "mesh_resolution_m": P.mesh_resolution_m,
                            "output_interval_min": P.output_interval_min,
                            "friction_law": P.friction_law,
                            "friction_coefficient": P.friction_coefficient}),
        results=(_RESULT, RESULT_FILENAME),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_ENGINE}.solve_case", compute_class=P.compute_class,
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the sediment-plume scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    sensitivity=(("suspended_cmax", "peak"),
                 ("plume_reach_m", "location"),
                 ("bed_evolution_max_m", "peak")),
    coerce=(
        location_or_bbox("telemac_river_sediment_plume", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'sediment spill in the "
                              "river near <place>', pass location='<place>'."),
        point_arg("release", tool="telemac_river_sediment_plume",
                  prompt="Click on the river where the sediment enters the water",
                  code="TELEMAC_PARAMS_INVALID"),
        reach.event_time(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
)
