"""Engine template ``telemac_river_temperature`` - how warm a reach gets.

TELEMAC-2D coupled with WAQTEL THERMIC over a real NHDPlus reach: the full
surface heat budget - shortwave in, longwave out, evaporation and sensible heat -
driven by the hourly weather record over the days the ask names."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.aoi import location_or_bbox
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.runtime import (
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, mesh, series
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import Atmosphere, Boundaries
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.templates.river_temperature.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_river_temperature"]

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

#: How far down its own reach the temperature series is read when the ask places
#: no point. The water at the bottom of the modelled stretch has been under the
#: weather longest, which is the water the question is about; the fraction holds
#: the node off the outflow face, where the boundary condition is what it carries.
_STATION_FRAC = 0.98

#: How far past the centerline the mapped banks are ASKED for. The water that
#: belongs to this reach reaches past the line - a far channel behind a mid-river
#: island is three km off it and is still the same river - and the pad widens the
#: QUESTION, never the meshed domain: the section cut below keeps only the
#: stretch between the reach's two ends.
_BANK_QUERY_PAD_M = 3000.0


class DATA:
    """The reach chain, one row per artifact, in the order it is read, plus the
    two observed records this question adds and the box each is asked for in:
    the weather over the reach, and the water temperature it opens at. The
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
    # water top, so the modelled channel is shallower than the real one, and a
    # shallower channel warms FASTER under the same budget - the heat is divided
    # by the depth at every node. GLO-30 is asked for on its OWN 1-arcsecond
    # lattice, so the raster the nodes are sampled from carries the source pixels.
    dem = tool("fetch_copernicus_dem", bbox=Ref("window.bbox"), px_per_deg=3600.0,
               purpose="river bed elevation")
    # WHERE THE OBSERVATIONS ARE ASKED FOR. Both records are point measurements
    # off the water, so each box is as wide as its own network is sparse: RAWS
    # stations sit on ridges and in clearings tens of kilometres from a river,
    # and a water-quality site upstream or downstream of the modelled stretch is
    # still measuring this river.
    weather_box = tool("compute_layer_bounds", layer_uri=centerline,
                       pad_m=60000.0, fit_map=False)
    sample_box = tool("compute_layer_bounds", layer_uri=centerline,
                      pad_m=25000.0, fit_map=False)
    # THE WEATHER the budget reads. The RAWS network is the one hourly record the
    # registry reaches without an account that carries SOLAR RADIATION beside the
    # air temperature, the humidity and the wind, and the shortwave term is what
    # drives a diurnal water temperature. Which station, the unit carriage and
    # the run's own clock are the Atmosphere slot's ingestion.
    weather = tool("fetch_raws_weather", bbox=Ref("weather_box.bbox"),
                   start_time=P.weather_start, end_time=P.weather_end)
    # WHAT THE REACH OPENS AT, measured. The run spends days on water that
    # entered at the upstream face, so this value is carried all week.
    water_sample = tool("fetch_usgs_water_quality", bbox=Ref("sample_box.bbox"),
                        characteristic="temperature")


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
    """The deck: a river under a week of weather, and the heat budget over it."""

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
    # under. The temperature advects under the engine's own scheme and
    # diffusivity.
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

    #: The carrier declares the temperature tracer ITSELF, so the reach opens at
    #: a measured water temperature and carries it in at the upstream face. The
    #: thermic process matches that name and attaches its budget to this tracer
    #: rather than appending a second one, and the unit written here is the unit
    #: the result carries.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEG"]
    INITIAL_VALUES_OF_TRACERS = [Ref("opening.water_temp_c")]

    #: The river arriving at the top of the reach is the same water the sample
    #: site measured: the reach warms because of what happens ALONG it, so the
    #: inflow carries the opening temperature rather than a second number.
    boundaries = Boundaries(measured=Ref("settled"),
                            tracers=[Ref("opening.water_temp_c")])

    #: The weather over the whole domain, as the one table the engine
    #: interpolates every column of between the same two rows. The nearest RAWS
    #: station whose record can drive the run end to end is the one taken. Cloud
    #: cover and atmospheric pressure are not in what that network reports, so
    #: neither column is written and the engine reads its own CLOUD COVER and
    #: VALUE OF ATMOSPHERIC PRESSURE, both of which the card shows.
    atmosphere = Atmosphere(observed=DATA.weather,
                            at=[Ref("settled.seed_lon"), Ref("settled.seed_lat")],
                            duration_s=P.sim_duration_s)

    #: The heat budget, on the engine's own calibration constants: this question
    #: asks what the published exchange gives under real weather, so the run
    #: states no keyword of its own and the review can see every one standing at
    #: its default.
    coupling = [WAQTEL.thermal()]


#: What this question PLACES: the temperature over time at the point the ask
#: gave, as the chart, and on the map as the station that carries it.
OUTPUTS = [
    series("T1", at=Ref("station")).chart(),
    series("T1", at=Ref("station")).station(),
]
CAPTIONS = {"T1": "water temperature"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above: how warm the water at the point got and
#: when, where it stood when the window closed, how far it swings between a day
#: and its night, and the swing between the warmest and the coolest station down
#: the reach, which is what the water picked up travelling it.
ANSWER = {
    "peak_temperature_c": series("T1", at=Ref("station")).measure("max"),
    "peak_temperature_time_s": series("T1", at=Ref("station")).measure("t_max"),
    "final_temperature_c": series("T1", at=Ref("station")).measure("last"),
    "diurnal_range_c": series("T1", at=Ref("station")).measure("range"),
    "warming_along_reach_c": profile("T1", along=DATA.centerline).measure("range"),
    "mean_velocity_mps": profile("T1", along=DATA.centerline).measure("velocity_mps"),
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
    native_hint="NHD channel geometry + Copernicus GLO-30 terrain; edge from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long reach is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling. A surface heat budget is "
        "divided by the local depth, so the DEPTH the mesh resolves is what "
        "moves the answer, not the planform"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_river_temperature",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_river_temperature = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        domain=(reach.Geocode(location=P.location, bbox=P.bbox),
                reach.ReachSeed(reach=Ref("reach"), rivers=DATA.rivers),
                reach.CarrierDischarge(seed=Ref("seed"), explicit=P.discharge_m3s,
                                       event_time=P.event_time)),
        mesh=MESH, mesh_on="reach",
        produce=(reach.MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),
                 reach.WaterTemperature(sample=DATA.water_sample,
                                        supplied=P.initial_water_temp_c,
                                        seed=Ref("seed")),
                 # WHERE the series is read, settled against the accepted mesh
                 # before the outputs are anchored: the same placement a release
                 # gets, because both are one node of a mesh nobody had yet.
                 Step(runner=f"{_AUTHORING}.assembler.settle_release",
                      stage="author",
                      kwargs={"point": P.station, "mesh": Ref("mesh"),
                              "centerline": DATA.centerline, "seed": Ref("seed"),
                              "reach": Ref("reach"), "fraction": _STATION_FRAC,
                              "label": "Temperature station"}).named("station")),
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
        review_title="Review the reach, the week of weather, and what it opens at"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # The peak is a saturated maximum over a reach-scale field, so it is not a
    # resolution class. WHERE the warming is greatest along the line is a local
    # feature and moves with the element that resolves the depth under it.
    sensitivity=(("warming_along_reach_c", "location"),),
    coerce=(
        location_or_bbox("telemac_river_temperature", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'how warm does the river "
                              "near <place> get this week', pass location='<place>'."),
        point_arg("station", tool="telemac_river_temperature",
                  prompt="Click on the river where the temperature should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        reach.event_time(),
        compute_class(),
    ),
    doc=DOC,
)
