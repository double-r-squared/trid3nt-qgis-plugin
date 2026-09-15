"""Engine template ``telemac_river_eutrophication`` - what one pass down an
enriched reach does to the water.

TELEMAC-2D coupled with WAQTEL's eutrophication process over a real NHDPlus
reach: phytoplankton growing on stated nitrate and phosphate under stated light
and temperature, and the oxygen budget that growth, its decay and the
nitrification of its ammonium drive. A reach flushes in hours, so the answer is
the LONGITUDINAL change between the water that enters and the water that
leaves - a seasonal bloom is a lake's question, not a river's."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import ParamRef, Ref, Step, register_workflow
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, mesh, series
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.templates.river_eutrophication.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_river_eutrophication"]

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

#: FORMULA FOR COMPUTING CS: the oxygen saturation follows the STATED water
#: temperature rather than the engine's constant ceiling, so the reach is judged
#: against the ceiling its own water actually has.
_SATURATION_FROM_TEMPERATURE = 1


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
    # water top, so the modelled channel is shallower than the real one, the
    # light reaches further down it than it does in the river, and the journal
    # names this row as what the bed came from. GLO-30 is asked for on its OWN
    # 1-arcsecond lattice, so the raster the nodes are sampled from carries the
    # source pixels rather than a resample of them.
    dem = tool("fetch_copernicus_dem", bbox=Ref("window.bbox"), px_per_deg=3600.0,
               purpose="river bed elevation")
    # WHAT THE REACH IS ACTUALLY THIS WARM AT, as the record rather than as the
    # deck's number. The engine reads ONE water temperature and this row returns
    # sample SITES with their latest value, so the row is what the stated
    # temperature is read against and never what fills the keyword: turning
    # scattered sites into a reach-wide number is a step nobody asked for.
    water_temperature = tool("fetch_usgs_water_quality", bbox=Ref("window.bbox"),
                             characteristic="temperature",
                             purpose="observed water temperature")


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
    """The deck: a river carrying nutrients, and the eight tracers the
    eutrophication process runs on them."""

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

    #: The carrier declares NO tracer of its own - nothing is released into this
    #: reach - so all eight belong to the coupled process and arrive in the order
    #: the engine appends them: PHY, PO4, POR, NO3, NOR, NH4, organic load, O2.
    INITIAL_VALUES_OF_TRACERS = [
        P.initial_phyto_ug_l, P.initial_po4_mgl, P.initial_por_mgl,
        P.initial_no3_mgl, P.initial_nor_mgl, P.initial_nh4_mgl,
        P.initial_organic_load_mgl, P.initial_do_mgl]

    #: The SAME water at every liquid boundary as the reach opens holding: the
    #: question is what the reach does to water of this composition, so a river
    #: that arrives different from the water already in it would answer a step
    #: change instead.
    boundaries = Boundaries(
        measured=Ref("settled"),
        tracers=[P.initial_phyto_ug_l, P.initial_po4_mgl, P.initial_por_mgl,
                 P.initial_no3_mgl, P.initial_nor_mgl, P.initial_nh4_mgl,
                 P.initial_organic_load_mgl, P.initial_do_mgl])

    #: Growth on the stated nutrients under the stated light, and the oxygen the
    #: growth and its decay drive. The light is stated because the engine reads
    #: none from a forcing file - the atmospheric file's radiation columns feed
    #: the thermal budget and nothing else - and a reach in the dark cannot
    #: bloom; the temperature because the engine's own is a cold-water number.
    #: Every rate and half-saturation constant is the engine's own.
    coupling = [WAQTEL.eutrophication(
        oxygen=True,
        WATER_TEMPERATURE=P.water_temp_c,
        SUNSHINE_FLUX_DENSITY_ON_WATER_SURFACE=P.sunshine_w_m2,
        SECCHI_DEPTH=P.secchi_depth_m,
        FORMULA_FOR_COMPUTING_CS=_SATURATION_FROM_TEMPERATURE)]


#: What this question PLACES: the bloom and the oxygen DOWN THE REACH, which is
#: the longitudinal change the question asks about, and the two of them over time
#: where the user is watching.
OUTPUTS = [
    profile("T1", along=DATA.centerline).chart(),
    profile("T8", along=DATA.centerline).chart(),
    series("T1", at=P.station).chart(),
    series("T8", at=P.station).chart(),
]
CAPTIONS = {"T1": "phyto biomass", "T8": "dissolved o2"}

#: The run's ANSWER: what one pass down the reach did to the water, as the
#: numbers a reader has to be able to check. Every ratio is held to the stated
#: concentration the water ENTERED at - the boundaries hold that value flat, so
#: it is the inlet, and the profile's other end is what the reach made of it.
ANSWER = {
    "phyto_max_ug_l": profile("T1", along=DATA.centerline).measure("max"),
    "phyto_max_distance_m": profile("T1", along=DATA.centerline).measure("x_max_m"),
    "phyto_growth_ratio": profile("T1", along=DATA.centerline).measure("max")
                          .over(P.initial_phyto_ug_l),
    "no3_remaining_ratio": profile("T4", along=DATA.centerline).measure("min")
                           .over(P.initial_no3_mgl),
    "po4_remaining_ratio": profile("T2", along=DATA.centerline).measure("min")
                           .over(P.initial_po4_mgl),
    "do_min_mgl": profile("T8", along=DATA.centerline).measure("min"),
    "do_min_distance_m": profile("T8", along=DATA.centerline).measure("x_min_m"),
    "do_below_standard": profile("T8", along=DATA.centerline).measure("min")
                         .below(P.do_standard_mgl),
    "pass_velocity_mps": profile("T8", along=DATA.centerline)
                         .measure("velocity_mps"),
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
        "(self-labeled); no fixed coarse ceiling. The edge also sets the CFL "
        "step, and this question is watched over days rather than hours"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_river_eutrophication",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_river_eutrophication = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        # Nothing is released here, so the reach is seeded on its own flowline
        # and the station is read wherever the user is watching.
        domain=(reach.Geocode(location=P.location, bbox=P.bbox),
                reach.ReachSeed(reach=Ref("reach"), rivers=DATA.rivers,
                                supplied=P.station),
                reach.CarrierDischarge(seed=Ref("seed"), explicit=P.discharge_m3s,
                                       event_time=P.event_time)),
        mesh=MESH, mesh_on="reach",
        produce=(reach.MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),),
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
        review_title="Review the reach and the water it is carrying"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # WHERE down the reach the oxygen bottoms out and where the biomass stands
    # highest are local-feature LOCATIONS and move with the element that
    # resolves them.
    sensitivity=(("do_min_distance_m", "location"),
                 ("phyto_max_distance_m", "location")),
    coerce=(
        location_or_bbox("telemac_river_eutrophication", code_prefix="TELEMAC"),
        point_arg("station", tool="telemac_river_eutrophication",
                  prompt="Click on the river where you want the water watched",
                  code="TELEMAC_PARAMS_INVALID"),
        reach.event_time(),
    ),
    doc=DOC,
)
