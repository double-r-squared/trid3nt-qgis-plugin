"""Engine template ``telemac_do_sag`` - the dissolved-oxygen sag below an outfall.

TELEMAC-2D coupled with WAQTEL O2 over a real NHDPlus reach: where DO bottoms
out downstream of a continuous discharge, against the standard the reach is held
to and the closed form the process reduces to."""

from __future__ import annotations

from trid3nt_contracts.telemac_contracts import TELEMAC_DO_STYLE
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
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, field, mesh
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Release
from trid3nt_server.workflows.telemac.templates.do_sag import streeter_phelps
from trid3nt_server.workflows.telemac.templates.do_sag.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_do_sag"]

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

#: How far down its own reach the outfall sits. The reach was navigated
#: downstream FROM the outfall, so the discharge belongs at the top; the fraction
#: is what holds the source node off the inflow face rather than on it, where it
#: would compete with the boundary condition for the same node.
_OUTFALL_FRAC = 0.02

#: How far past the centerline the mapped banks are ASKED for. The water that
#: belongs to this reach reaches past the line - a far channel behind a mid-river
#: island is three km off it and is still the same river - and the pad widens the
#: QUESTION, never the meshed domain: the section cut below keeps only the
#: stretch between the reach's two ends.
_BANK_QUERY_PAD_M = 3000.0


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
    """The deck: a river, an OUTFALL, and the four tracers the O2 process runs."""

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
    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1,T2,T3,T4"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    # The carrier declares ONE tracer; WAQTEL's O2 process appends DISSOLVED O2,
    # ORGANIC LOAD and NH4 LOAD behind it, which is why every array sized to the
    # tracer count below carries four values.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0, P.upstream_do_mgl, 0.0, 0.0]

    #: CLEAN RIVER at every liquid boundary: no organic load, its own oxygen. The
    #: load enters at the source, so which boundary the engine numbers first
    #: cannot decide the answer.
    boundaries = Boundaries(measured=Ref("settled"),
                            tracers=[0.0, P.upstream_do_mgl, 0.0, 0.0])

    #: The OUTFALL: a permitted discharge does not pulse, so the flow and its
    #: concentrations hold flat across the whole run and the reach reaches the
    #: steady-state sag the question is asked about.
    releases = [Release(at=Ref("outfall.at"), q=P.effluent_q_m3s,
                        tracers=[0.0, P.effluent_do_mgl, P.effluent_bod_mgl, 0.0],
                        window_s=None, until_s=Ref("settled.until_s"))]

    #: Deoxygenation balanced by surface reaeration, and nothing else: the
    #: modelled curve is the closed form the question is asked against, so this
    #: run states FRESH water, a CONSTANT reaeration rate (formula 0, the one the
    #: closed form holds under) and zeroes the three sources the closed form has
    #: no term for - nitrification, benthic demand, and photosynthesis less
    #: respiration.
    coupling = [WAQTEL.o2(water_temp_c=P.water_temp_c, salinity_ppt=0.0,
                          k1_per_day=P.k1_per_day, k4_per_day=0.0,
                          k2_per_day=P.k2_per_day, k2_formula=0,
                          saturation_mgl=P.do_saturation_mgl,
                          benthic_demand=0.0, photosynthesis_p=0.0,
                          respiration_r=0.0)]


#: What the solved run is read for: the oxygen at the last instant as the map,
#: the oxygen over time as the animation, and the oxygen down the reach as the
#: chart, with the organic load, the closed form and the standard drawn beside it.
OUTPUTS = [
    field("T2", t=-1).layer(style=TELEMAC_DO_STYLE),
    field("T2", t="every").animate(),
    profile("T2", along=DATA.centerline).chart(reference=streeter_phelps.overlay),
]
CAPTIONS = {"T2": "dissolved oxygen", "T3": "organic load"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above: how low the oxygen bottoms out and where,
#: whether that is below the standard the reach is held to, the mixed load that
#: drove it, and the speed it travelled at.
ANSWER = {
    "do_min_mgl": profile("T2", along=DATA.centerline).measure("min"),
    "do_below_standard": profile("T2", along=DATA.centerline).measure("min")
                         .below(P.do_standard_mgl),
    "do_min_distance_m": profile("T2", along=DATA.centerline).measure("x_min_m"),
    "bod_mixed_mgl": profile("T3", along=DATA.centerline).measure("max"),
    "mean_velocity_mps": profile("T2", along=DATA.centerline).measure("velocity_mps"),
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
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_do_sag = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        # The outfall pins the reach: the one centerline is navigated from it.
        domain=(reach.Geocode(location=P.location, bbox=P.bbox),
                reach.ReachSeed(reach=Ref("reach"), rivers=DATA.rivers,
                                supplied=P.outfall_coords),
                reach.CarrierDischarge(seed=Ref("seed"), explicit=P.discharge_m3s,
                                       event_time=P.event_time)),
        mesh=MESH, mesh_on="reach",
        produce=(reach.MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),
                 # The outfall, settled against the accepted mesh before the
                 # sheet reads it.
                 Step(runner=f"{_AUTHORING}.assembler.settle_release",
                      stage="author",
                      kwargs={"point": P.outfall_coords, "mesh": Ref("mesh"),
                              "centerline": DATA.centerline, "seed": Ref("seed"),
                              "reach": Ref("reach"), "fraction": _OUTFALL_FRAC,
                              "label": "Outfall"}).named("outfall")),
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
        review_title="Review the outfall and the reach it discharges to"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # WHERE the sag sits is a local-feature LOCATION and moves with the element
    # that resolves it. The DO minimum itself is a saturated maximum - a
    # converged class - so it carries no label.
    sensitivity=(("do_min_distance_m", "location"),),
    coerce=(
        location_or_bbox("telemac_do_sag", code_prefix="TELEMAC"),
        point_arg("outfall_coords", tool="telemac_do_sag",
                  prompt="Click on the river where the outfall discharges",
                  code="TELEMAC_PARAMS_INVALID"),
        reach.event_time(),
    ),
    doc=DOC,
)
