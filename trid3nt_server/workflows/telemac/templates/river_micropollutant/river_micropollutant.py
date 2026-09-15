"""Engine template ``telemac_river_micropollutant`` - a sorbing substance in a reach.

TELEMAC-2D coupled with WAQTEL micropol over a real NHDPlus reach: how much of a
released substance travels dissolved, how much rides the river's suspended
sediment, and how much of it is left on the bed."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.modules import (
    T2D,
    WAQTEL,
    field,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Release
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.river_micropollutant.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_river_micropollutant"]

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

#: The substance's own tracer, declared by the carrier so that the engine ADOPTS
#: it for the micropol process rather than adding a sixth: ADDTRACER matches on
#: the first sixteen characters, which are the process's own name for it. The
#: four the process appends behind it are the suspended sediment, the bed
#: sediment, and the substance sorbed onto each.
_DISSOLVED = "MICRO POLLUTANT MG/L"


class DATA:
    """The reach chain, one row per artifact, in the order it is read. The
    carrier discharge is a STEP, because it reads the resolved seed."""

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
    """The deck: a river carrying suspended sediment, and a substance released
    into it that partitions onto that sediment and settles with it."""

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
    # under. The tracers advect under the engine's own scheme and diffusivity.
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

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    # The carrier declares the DISSOLVED substance; the micropol process adopts
    # it and appends the suspended sediment, the bed sediment and the two sorbed
    # phases behind it, which is why every array sized to the tracer count below
    # carries five values in that order.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = [_DISSOLVED]
    # The river arrives carrying its sediment and nothing else: the bed starts
    # clean, and everything on it at the end got there during the run.
    INITIAL_VALUES_OF_TRACERS = [0.0, P.ambient_spm_mgl, 0.0, 0.0, 0.0]

    #: The carrier's own boundary values: the same clean river, carrying the same
    #: suspended sediment, at every liquid boundary.
    boundaries = Boundaries(measured=Ref("settled"),
                            tracers=[0.0, P.ambient_spm_mgl, 0.0, 0.0, 0.0])

    #: A FINITE release at a mid-reach point source, so what happens to the
    #: substance after it is in the water is what the rest of the run shows. It
    #: enters DISSOLVED and carries no sediment of its own.
    releases = [Release(at=Ref("release.at"), q=P.source_q_m3s,
                        tracers=[P.source_concentration_mgl, 0.0, 0.0, 0.0, 0.0],
                        window_s=P.release_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The partition, as WAQTEL's own keywords: how fast the sediment settles,
    #: how hard the substance holds onto it, how fast it comes back off, and how
    #: fast it decays. Each is the engine's own where the ask states none.
    coupling = [WAQTEL.micropollutant(
        SEDIMENT_SETTLING_VELOCITY=P.settling_velocity_mps,
        COEFFICIENT_OF_DISTRIBUTION=P.distribution_coefficient_m3kg,
        CONSTANT_OF_DESORPTION_KINETIC=P.desorption_constant_per_s,
        EXPONENTIAL_DESINTEGRATION_CONSTANT=P.decay_constant_per_s)]


#: What this question PLACES: the dissolved history where the user asks for it.
#: The settled point is read as the WHOLE step, whose lon/lat is where the node
#: is looked up; its ``at`` is the source's own UTM pair, which is what a deck
#: keyword takes and not what a read is anchored by.
OUTPUTS = [
    series("T1", at=Ref("monitoring")).chart(),
]
CAPTIONS = {"T1": "dissolved micropollutant"}

#: The run's ANSWER, as the numbers a reader has to be able to check: how
#: concentrated the dissolved substance got AT THE MONITORING POINT and when,
#: how far the dissolved body of water travelled, and where the substance stands
#: at the last instant - dissolved, on the suspended sediment, and on the bed.
#: The three final means are one partition read three ways, and the last answer
#: is the question's own comparison over two of them: how much of the substance
#: sits on the bed for every unit of it still dissolved.
ANSWER = {
    "dissolved_cmax_mgl": series("T1", at=Ref("monitoring")).measure("max"),
    "dissolved_peak_time_s": series("T1", at=Ref("monitoring")).measure("t_max"),
    "dissolved_travel_m": field("T1", t="every").measure("travel_m"),
    "dissolved_final_mean_mgl": field("T1").measure("mean"),
    "suspended_sorbed_final_mean_mgl": field("T4").measure("mean"),
    "bed_sorbed_final_mean_mgl": field("T5").measure("mean"),
    "bed_over_dissolved": field("T5").measure("mean")
                         .over(field("T1").measure("mean")),
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
    native_hint="NHD channel geometry + Copernicus GLO-30 terrain; edge sized "
                "from reach width",
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
_METADATA = AtomicToolMetadata(
    name="telemac_river_micropollutant",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_river_micropollutant = register_workflow(
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
                 # WHERE the substance enters the water, settled against the
                 # accepted mesh before the sheet reads it.
                 Step(runner=f"{_AUTHORING}.assembler.settle_release",
                      stage="author",
                      kwargs={"point": P.release, "mesh": Ref("mesh"),
                              "centerline": DATA.centerline, "seed": Ref("seed"),
                              "reach": Ref("reach"),
                              "fraction": P.release_fraction,
                              "label": "Release point"}
                      ).named("release"),
                 # WHERE the dissolved history is read, settled the same way, so
                 # the chart is anchored on a node the run actually solved on.
                 Step(runner=f"{_AUTHORING}.assembler.settle_release",
                      stage="author",
                      kwargs={"point": P.monitoring_point, "mesh": Ref("mesh"),
                              "centerline": DATA.centerline, "seed": Ref("seed"),
                              "reach": Ref("reach"),
                              "fraction": P.monitoring_fraction,
                              "label": "Monitoring point"}
                      ).named("monitoring")),
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_reach", stage="author",
                    kwargs={"reach": Ref("reach"), "seed": Ref("seed"),
                            "mesh": Ref("mesh"), "centerline": DATA.centerline,
                            "carrier_discharge": Ref("carrier_discharge"),
                            "sim_duration_s": P.sim_duration_s,
                            "mesh_resolution_m": P.mesh_resolution_m,
                            "output_interval_min": P.output_interval_min,
                            "friction_law": P.friction_law,
                            "friction_coefficient": P.friction_coefficient}),
        results=(_RESULT,),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_ENGINE}.solve_case", compute_class=P.compute_class,
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the release and the sediment it partitions onto"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # A concentration peak lives inside one element and is measured LOW on a
    # coarse mesh. How far the dissolved body of water reached is a front
    # location and moves with it.
    sensitivity=(("dissolved_cmax_mgl", "peak"),
                 ("dissolved_travel_m", "location")),
    coerce=(
        location_or_bbox("telemac_river_micropollutant", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'a metal spill in the "
                              "river near <place>', pass location='<place>'."),
        point_arg("release", tool="telemac_river_micropollutant",
                  prompt="Click on the river where the substance enters the water",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("monitoring_point", tool="telemac_river_micropollutant",
                  prompt="Click where downstream to read the dissolved history",
                  code="TELEMAC_PARAMS_INVALID"),
        reach.event_time(),
        compute_class(),
    ),
    doc=DOC,
)
