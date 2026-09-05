"""RIVER - the body, the chain and the mesh recipe every river template lists.

A reach is one question asked five ways: a dye plume, an oil slick, a scouring
bed, a settling plume and an oxygen sag all run the same shallow-water solve over
the same stretch of mapped water, and differ in which slots they fill on top of
it. What that sameness IS lives here: the DATA chain that cuts the reach out of
real geometry, the MESH recipe that triangulates it, the acquisition steps that
establish the modelled world, and the keyword assertions the deck makes about the
water rather than about the substance in it.

Every assertion below was a hardcoded literal in the steering-file writer this
body replaces. What is NOT here is as deliberate: a keyword whose stated value
was the dictionary's own default is unwritten, because the engine already
supplies it and restating it would make the deck claim a choice nobody made.

The three slots the boundary and the initial state are set from are DERIVED
producers rather than numbers: the outflow level is a normal depth over the
section the accepted mesh's outflow face cuts, at the roughness THIS deck writes
and for the discharge it prescribes, and the run opens at that same depth laid
bed-parallel - so the reach starts at the equilibrium its own downstream boundary
holds it to instead of draining a blanket depth into it over the first minutes.
"""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.runtime import (
    Param,
    ParamRef,
    Ref,
    Step,
    data_rows,
    doors,
    param_rows,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.helpers.forcing import CarrierDischarge
from trid3nt_server.workflows.telemac.helpers.reach import Geocode, ReachSeed
from trid3nt_server.workflows.telemac.modules import T2D

__all__ = ["DATA", "DATA_ROWS", "GEOMETRY", "BOUNDARY", "RESULT", "RESTART",
           "MESH", "PARAMS", "PARAM_ROWS", "RELEASE", "RELEASE_ROWS",
           "RIVER", "acquire", "settle"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"
_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The names the run directory holds a reach's files under. They are the deck's
#: own GEOMETRY / BOUNDARY CONDITIONS / RESULTS / RESTART statements, so the
#: steering file reads as the record of the run it is.
GEOMETRY = "river.slf"
BOUNDARY = "river.cli"
RESULT = "r2d_river.slf"
#: The engine's perfect-restart record - the full state at the last time step in
#: double precision, which is what a continuation reads. The graphic results file
#: is a picture of the run; this is the run's last instant.
RESTART = "restart_river.slf"

#: The friction the reach is solved at when the ask states none. It is named on
#: the declaration because the outflow stage is a normal depth AT this roughness:
#: a stage derived at one number under a deck written at another is a level the
#: run never sits at. The assembler reads the same two numbers.
_STRICKLER = 33.0

#: How far past the centerline the mapped banks are ASKED for. The water that
#: belongs to this reach reaches past the line - a far channel behind a mid-river
#: island is three km off it and is still the same river - and the pad widens the
#: QUESTION, never the meshed domain: the section cut below keeps only the
#: stretch between the reach's two ends.
_BANK_QUERY_PAD_M = 3000.0


class PARAMS:
    """The rows the SHARED chain and the shared settle read, on every river run.

    A template declares its own question's rows beside these; what is here is
    what the part itself reads, so a body that lists RIVER cannot be missing a
    value the part goes looking for.
    """

    river_geometry_uri = Param(
        door=doors.USER, optional=True, consequence="aoi",
        derived_when_absent="the reach flowline is fetched fresh for the AOI",
        desc="Reuse an already-fetched river flowline for this reach instead of "
             "re-fetching it")
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            f"the reach is solved at Strickler {_STRICKLER}, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
        derived_when_absent="the coefficient is read as a Strickler one",
        desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, "
             "4=Manning")


    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Place name on the river, geocoded to the reach")

    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place")

    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is resolved from the NOAA National "
            "Water Model at the reach; no NWM coverage refuses typed rather "
            "than falling back to a constant"),
        desc="Steady upstream CARRIER discharge - the river flow that dilutes "
             "and transports the release")

    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The storm/event moment to read the carrier discharge cycle at - "
             "from phrasing like 'during last Tuesday's storm'; an ISO date "
             "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
             "Unset reads the most recent published NWM cycle. The NWM PDS "
             "bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a "
             "different cycle.")

    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        desc="Result-writing cadence; unset keeps the steering file's own period")

    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


class RELEASE:
    """The rows a run that RELEASES something at a point in the reach declares.

    Where it enters, how much of it, for how long, and the two forcings that act
    on the water it enters - all identical whatever is released, which is why
    they are here and the substance's own rows are in its template.
    """


    release_coords = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=tuple[float, float] | list[float],
        derived_when_absent=(
            "the release sits at spill_fraction along the meshed reach; the "
            "downstream plume distance is measured from there"),
        desc="Where the substance enters the water, (lon, lat) EPSG:4326")

    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-reach release position, 0=upstream..1=downstream; the source "
             "must sit strictly INSIDE the reach, never on a boundary")

    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")

    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "river's carrier flow")

    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")

    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")

    rainfall_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 2000.0),
        units="mm/day", consequence="scenario",
        desc="Distributed ON-MESH rainfall applied at every wet node, independent "
             "of the inflow hydrograph")

    evaporation_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 50.0),
        units="mm/day", consequence="scenario",
        desc="Distributed evaporation, subtracted from the net rain flux")

    rainfall_gridmet_window = Param(
        door=doors.USER, optional=True,
        consequence="scenario",
        desc="Real-storm source: an ISO window 'YYYY-MM-DD:YYYY-MM-DD' whose "
             "gridMET domain-mean daily precipitation supersedes rainfall_mm_per_day")

    velocity_diffusivity = Param(
        door=doors.USER, optional=True, bounds=(1e-3, 10.0),
        units="m^2/s", consequence="numerical",
        desc="Turbulent momentum diffusivity")

    tracer_diffusivity = Param(
        door=doors.USER, optional=True, bounds=(1e-3, 10.0),
        units="m^2/s", consequence="numerical",
        desc="Tracer diffusivity, which sets lateral plume spread")

    reach_seed_coords = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float] | list[float], wire=False,
        derived_when_absent=(
            "the reach centerline is resolved from the mid-reach point on the "
            "largest fetched flowline, else the geocoded centroid"),
        desc="The point the reach centerline is navigated from, (lon, lat); "
             "set when the release must pin which water body is meshed")


#: The shared rows, as a template composes them with its own.
PARAM_ROWS = param_rows(PARAMS)
RELEASE_ROWS = param_rows(RELEASE)


class DATA:
    """The reach chain, one row per artifact, in the order it is read.

    The carrier discharge is a STEP rather than a row: it reads the resolved
    mid-reach seed, which is a step result and not something a producer
    declaration can name.
    """

    rivers = tool(f"{_HELPERS}.reach.fetch_reach_flowline",
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


#: The chain as ROWS, so a template composes it with rows of its own. A DATA body
#: is read by ``vars()``, which sees nothing a subclass inherited.
DATA_ROWS = data_rows(DATA)


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


def acquire(*, seed_coords: Any) -> tuple[Step, ...]:
    """The steps that establish the modelled world and the flow that carries it.

    Three, because the world is not established until the flow through it is: the
    seed is the point the discharge is read at, so it cannot be declared
    independently of the reach. ``seed_coords`` pins that seed - a template whose
    ask names where the substance enters the water is naming which stretch to
    model, and the one centerline is navigated from there.
    """
    return (
        Geocode.reach(ParamRef("location"), ParamRef("bbox")).named("reach"),
        ReachSeed(reach=Ref("reach"), rivers=DATA.rivers,
                  supplied=seed_coords).named("seed"),
        CarrierDischarge(seed=Ref("seed"), explicit=ParamRef("discharge_m3s"),
                         event_time=ParamRef("event_time")
                         ).named("carrier_discharge"),
    )


def settle(*, release_coords: Any, spill_fraction: Any,
           marker_label: str = "Release point", rain: Any = None,
           continue_from: Any = None, oil: Any = None,
           dredge: Any = None) -> Step:
    """Measure the reach the accepted mesh holds -> what the sheet is filled from.

    Everything a keyword cannot be set without: the bed at the mesh's declared
    roles, the section its outflow face cuts, the uniform-flow depth that section
    conveys the prescribed discharge at, and where the release actually lands.
    """
    return Step(runner=f"{_AUTHORING}.assembler.settle_reach", stage="author",
                kwargs={
                    "reach": Ref("reach"), "seed": Ref("seed"),
                    "mesh": Ref("mesh"), "centerline": DATA.centerline,
                    "reach_polygon": DATA.reach_polygon,
                    "carrier_discharge": Ref("carrier_discharge"),
                    "sim_duration_s": ParamRef("sim_duration_s"),
                    "mesh_resolution_m": ParamRef("mesh_resolution_m"),
                    "output_interval_min": ParamRef("output_interval_min"),
                    "friction_law": ParamRef("friction_law"),
                    "friction_coefficient": ParamRef("friction_coefficient"),
                    "release_coords": release_coords,
                    "spill_fraction": spill_fraction,
                    "marker_label": marker_label, "rain": rain,
                    "continue_from": continue_from, "oil": oil, "dredge": dredge})


class RIVER(T2D):
    """What every river deck states about the WATER, whatever is carried in it."""

    GEOMETRY_FILE = GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BOUNDARY
    RESULTS_FILE = RESULT
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
    VELOCITY_DIFFUSIVITY = 0.1

    # The advection of momentum and depth, and the SUPG the reach is stable
    # under. The tracer's own scheme is stated separately below.
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

    # Every river template carries at least one tracer, and all of them advect it
    # the same way: the scheme the reach is stable under, and a diffusivity that
    # sets lateral plume spread.
    SCHEME_FOR_ADVECTION_OF_TRACERS = 1
    COEFFICIENT_FOR_DIFFUSION_OF_TRACERS = 0.1
