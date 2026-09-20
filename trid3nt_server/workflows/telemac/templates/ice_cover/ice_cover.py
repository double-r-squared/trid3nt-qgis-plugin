"""Engine template ``telemac_ice_cover`` - when a body of water freezes over.

TELEMAC-2D coupled with KHIONE over the domain the run is given: the surface
heat budget under the hourly weather record, the frazil the cooling water makes,
the border ice that grows in from the banks and the cover that thickens over it.
The water opens at a MEASURED temperature and the air, the dew point, the cloud
and the wind are the record's."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    register_workflow,
    tool,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.modules import KHIONE, T2D, mesh, series
from trid3nt_server.workflows.telemac.modules.khione import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import Atmosphere, Boundaries
from trid3nt_server.workflows.telemac.templates.ice_cover.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Placed, TelemacWorkflow,
)

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_ice_cover"]


#: The roughness this deck is solved at, and the law it is read under: Strickler,
#: the coefficient an unsurveyed channel is screened at. ONE number, stated once,
#: because the outflow stage is derived as a normal depth AT this roughness and a
#: stage derived at one number under a deck written at another is a level the run
#: never sits at. A user who knows the channel sets the keyword by name.
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

#: How far the reach producer walks downstream from the seed when this question
#: has to find its own domain. A user who wants another stretch - or another body
#: of water entirely - supplies the domain polygon, which supersedes the producer.
_REACH_LENGTH_KM = 12.0

#: How far down the domain the cover series is read when the ask places no
#: point. The water furthest from where it entered has been losing heat to the
#: air longest, which is the water that freezes first; the fraction holds the
#: node off the outflow face, where the boundary condition is what it carries.
_STATION_FRAC = 0.98

#: WHERE the series is read: the point the user clicked, else that fraction
#: along the domain's own centerline. The workflow settles it onto a node of the
#: accepted mesh, so the chart is a node the run solved on.
_STATION = Placed("station", point=PARAMS.station, fraction=_STATION_FRAC,
                  label="Ice station")

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10

#: The tracers KHIONE appends to this host under the switches the ice deck below
#: states, in the order it appends them: the water temperature, the frazil it
#: suspends, and the two the dynamic cover carries. The boundary list is written
#: one value per tracer per liquid boundary, so this deck can only state the
#: inflow water if it states all four.
_INFLOW_ICE = [0.0, 0.0, 0.0]


class DATA:
    """The slots this run stands on - the water, the bed under it and what it
    opens at - beside the flow that fills its inflow and the days of weather the
    heat budget is driven by."""

    # THE DOMAIN. A polygon the caller supplies or draws supersedes this; unfilled,
    # the reach the seed stands on is cut from the mapped water and arrives with
    # its two end transects, which is where the inflow and the outflow are
    # prescribed. A closed body states no run and its whole edge is wall.
    domain = Data.domain(
        tool("fetch_river_reach", distance_km=_REACH_LENGTH_KM,
             seed_point=[Ref("seed.lon"), Ref("seed.lat")]))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()

    # THE MEASUREMENT. Water with no federal navigation project has no published
    # sounding, and the sheet says so rather than refusing.
    survey = Data(tool("fetch_ehydro_surveys", bbox=Ref("domain.bbox"))
                  ).context("no published channel survey over this domain; "
                            "the terrain surface stands")
    # THE SOUNDINGS AS A SURFACE, at the scale the mesh resolves. Its values are
    # DEPTHS below the survey's own project datum, and the merge below reads
    # them as elevations on the terrain's zero through the shift the survey
    # publishes about itself.
    surveyed_bed = Data(tool("derive_survey_surface", points=survey,
                             value_field="depth_below_datum_m",
                             resolution_m=ParamRef("mesh_resolution_m"))
                        ).context("no soundings to grid into a surveyed bed "
                                  "over this domain")
    # A terrain surface measures the water TOP, so where no survey reaches it
    # the modelled water is shallower than the real water - and a heat budget
    # divided by too small a depth cools that water too fast.
    terrain = Data(tool("fetch_dem", bbox=Ref("domain.bbox"), source="3dep",
                        resolution_m=_TERRAIN_RESOLUTION_M,
                        purpose="bed elevation"))
    # ONE bed: the survey where it measured, the terrain everywhere else, as a
    # derive over the two rows. With the survey absent the terrain passes
    # through the merge unchanged and is the whole bed.
    bed = Data.bed(tool("derive_merge_rasters", primary=surveyed_bed,
                        fallback=terrain))

    # The carrier flow the inflow run prescribes. A number stated on this row
    # stands over any record, so the flow is the slot's and no param twins it.
    # ONE reading, not the grid the model published: which reach segment reports
    # it is ranked against the domain's own interior point, and the step that
    # opens the channel refuses a record nobody chose from.
    carrier = Data.discharge(
        tool("fetch_noaa_nwm_streamflow", bbox=Ref("domain.bbox"),
             valid_time=ParamRef("event_time")),
        near=Ref("domain.centroid"), value_field="streamflow_cms",
        measures="a streamflow", opens="the carrier flow opens at"
    ).context("the National Water Model published no streamflow over this "
              "domain at that cycle")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on - a gauge publishes its height above its own zero, which is a
    # different surface, so no source is named here and the number is stated.
    stage = Data.level(near=Ref("domain.centroid"),
                       opens="the outflow holds at").optional()

    # THE WEATHER the budget reads. The ice module takes the air temperature, the
    # DEW POINT, the cloud and the wind out of this one file and computes its own
    # shortwave from the cloud and the local longitude, so the airport network -
    # which measures a dew point and codes a sky cover, and which sits where
    # people and water are rather than on a ridge - is the record that fits it.
    # Which station is taken, the unit carriage and the run's own clock are the
    # Atmosphere slot's ingestion.
    # A record a user already holds STANDS OVER the network: the mark puts this
    # row on the wire, and what is handed in is read the same way the fetched
    # record is. A table of weather has no extent, so it is not checked against
    # the domain.
    weather = Data(tool("fetch_asos_metar", bbox=Ref("domain.bbox"),
                        start_time=P.weather_start,
                        end_time=P.weather_end).supplied(validate=None))
    # WHAT THE WATER OPENS AT, measured. ONE reading off the sample site nearest
    # the point the series is read at, in the unit the keyword carries, with that
    # site, its distance and the sample date on the run journal. How much heat
    # the water has to lose before it makes any ice at all is this number, so it
    # is load-bearing, and it is the water THIS run is about: the row states the
    # run's own moment, and a sample from outside the window that closes there
    # is another river's reading rather than this one's. A number supplied here
    # stands over the record, and where the portal sampled nothing in the window
    # the sheet says so and the value is the caller's.
    water_temperature = Data.observation(
        tool("fetch_usgs_water_quality", bbox=Ref("domain.bbox"),
             characteristic="temperature",
             valid_time=ParamRef("event_time")),
        near=[Ref("station.lon"), Ref("station.lat")], units="degC",
        at=ParamRef("event_time"),
        measures="a water temperature", opens="the water opens at"
    ).context("no sample near this domain in this window; the stated value "
              "stands")


class STEERING(T2D):
    """The deck: a body of water under a cold snap, and the ice module over it."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = "r2d_domain.slf"
    TITLE = Ref("settled.title")

    # The step the domain is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for.
    TIME_STEP = Ref("settled.time_step_s")
    LISTING_PRINTOUT_PERIOD = 500

    # HOW THE WATER OPENS is the settle's, not this deck's: flat at the level
    # somebody measured, or a sheet of one depth on the bed where a uniform-flow
    # depth was derived instead. A horizontal surface at a level the reach does
    # not reach leaves every node upstream of it dry - the flowrate face among
    # them - which is why the two are not the same statement.
    INITIAL_CONDITIONS = Ref("settled.opening")
    INITIAL_DEPTH = Ref("settled.depth_m")
    INITIAL_ELEVATION = Ref("settled.level_m")

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    # The advection of momentum and depth, and the SUPG the domain is stable
    # under. Every tracer on this run is the ice module's, and it advects them
    # under its own scheme and diffusivity.
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

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 20 m default edge the CFL step is 1 s, and the week stated below
    # is about 604,800 of them - one frame every 12,000 steps is 50 frames
    # of the week. A user who wants another cadence sets the keyword by its
    # own name, on this deck and on the ice deck's own period below.
    GRAPHIC_PRINTOUT_PERIOD = 12000

    # SEVEN DAYS. A cover forms over nights of sustained heat loss and thickens
    # over the days between them, so the window has to hold whole days and
    # enough of them that the first freezing night is not the last instant.
    DURATION = 604800.0

    #: WHAT THE WATER OPENS AT. KHIONE declares its own tracers behind this deck,
    #: and the temperature is the first of them, so the one value written here is
    #: the measured temperature the whole domain starts at; the frazil and the
    #: cover behind it open at the engine's own zero, which is water with no ice
    #: in it yet.
    INITIAL_VALUES_OF_TRACERS = [Ref("water_temperature.value")]

    #: The water arriving at a feeding face is the OPEN water above the reach:
    #: it carries the temperature the sample site measured and no ice at all -
    #: no frazil in suspension, no cover on it. One value per appended tracer,
    #: in the order the ice deck below appends them.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[Ref("water_temperature.value"), *_INFLOW_ICE])

    #: The weather over the whole domain, as the one table the engine
    #: interpolates every column of between the same two rows. The nearest
    #: station whose record can drive the run end to end is the one taken, and
    #: what is written into it is what the readers of this run read. The engine
    #: stops at an instant outside the table, so the file is written for the
    #: DURATION this deck states rather than for a second number beside it.
    atmosphere = Atmosphere(observed=DATA.weather, at=_STATION,
                            duration_s=Ref("sheet.DURATION"))

    #: The ice. Five statements, and every other constant the module carries -
    #: the heat budget itself, the frazil class count and its seeding, the
    #: critical velocity and temperature border ice forms under, the cover's own
    #: friction - stands at the engine's published default where the review can
    #: see it.
    coupling = [KHIONE.ice(
        geometry=GEOMETRY_FILE, boundary=BOUNDARY_CONDITIONS_FILE,
        # The budget reads the atmospheric file rather than a constant sky: at
        # the engine's own 0 nothing in the weather reaches the water and no
        # run driven by a record could ever freeze.
        ATMOSPHERE_WATER_EXCHANGE_MODEL=1,
        # The cover this question is about: the keyword that allocates the cover
        # fraction and its thickness and lets them grow, drift and thicken.
        DYNAMIC_ICE_COVER=True,
        # What turns the frazil the budget makes into that cover. At the
        # engine's own 0 the suspended ice never builds a surface and the cover
        # stays where it started.
        MODEL_FOR_MASS_EXCHANGE_BETWEEN_FRAZIL_AND_ICE_COVER=1,
        # A cover on a river starts at the BANKS, where the water is slow and
        # shallow, and grows inward; without this the run can only make ice
        # where the frazil deposits.
        BORDER_ICE_COVER=True,
        # The ice result is written on the host's own cadence, so the two files
        # carry the same instants and a series read off either is the same clock.
        GRAPHIC_PRINTOUT_PERIOD=GRAPHIC_PRINTOUT_PERIOD,
        LISTING_PRINTOUT_PERIOD=LISTING_PRINTOUT_PERIOD)]


#: What this question PLACES: the cover and how thick it got over time at the
#: point the ask gave, off the ice module's own result. Everything else the two
#: modules wrote - the heat fluxes, the frazil, the ice type - is published
#: because their tables row it, not because this template asked.
OUTPUTS = [
    series("DYNCOVC", at=_STATION, module="khione").chart(),
    series("DYNCOVT", at=_STATION, module="khione").chart(),
]
CAPTIONS = {"DYNCOVC": "ice cover fraction", "DYNCOVT": "ice cover thickness"}

#: The run's ANSWER, as the numbers a reader has to be able to check: when the
#: water at the point first stood under more ice than the ask calls frozen, when
#: anywhere in the domain first did, the thickest ice the run made anywhere, and
#: how much of the surface the point was under when the window closed.
ANSWER = {
    "freeze_time_s": series("DYNCOVC", at=_STATION, module="khione",
                            above=ParamRef("cover_threshold")).measure("t_above")
    .otherwise("the cover at the point did not freeze within the window"),
    "domain_freeze_time_s": series("DYNCOVC", module="khione",
                                   above=ParamRef("cover_threshold")
                                   ).measure("t_above")
    .otherwise("no node in the domain froze within the window"),
    # The thickest ice anywhere is the engine's TOTAL: the solid border ice
    # and the dynamic cover together, not the cover alone.
    "peak_ice_thickness_m": series("COV_THT", module="khione").measure("max"),
    "final_cover_fraction": series("DYNCOVC", at=_STATION, module="khione"
                                   ).measure("last"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long domain
#: WITHIN this declaration, and the effective edge stays >= 2 cells across the
#: channel.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="USACE eHydro channel soundings over 3DEP terrain; edge sized "
                "from the domain's width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long domain is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling. The heat lost through the "
        "surface is divided by the local depth and border ice grows from the "
        "bank, so the DEPTH and the EDGE the mesh resolves are what move the "
        "answer"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_ice_cover",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: WHAT THE RUN HAS TO WRITE: the host's file and the ice module's own
#: beside it. Every measure this question answers is read off the second
#: one, so a run that published only the host's would come back with the
#: ice it made left in the box.
RESULTS = (STEERING.RESULTS_FILE, RESULT_FILENAME)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the water, the cold snap, and what it opens at"


telemac_ice_cover = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The thickest ice sits where the water is thinnest and slowest - against
    # the bank, in the shallows - and a coarse element averages that water in
    # with the channel it is beside.
    sensitivity=(("peak_ice_thickness_m", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_ice_cover",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_ice_cover",
                  prompt="Click on the water where the ice should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
)
