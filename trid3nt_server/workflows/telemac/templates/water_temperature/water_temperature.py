"""Engine template ``telemac_water_temperature`` - how warm a body of water gets.

TELEMAC-2D coupled with WAQTEL THERMIC over the domain the run is given: the full
surface heat budget - shortwave in, longwave out, evaporation and sensible heat -
driven by the hourly weather record over the days the ask names. The water opens
at a MEASURED temperature and carries that value in at every face that feeds it."""

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
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, field, mesh, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Atmosphere, Boundaries
from trid3nt_server.workflows.telemac.templates.water_temperature.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Placed, TelemacWorkflow,
)

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_water_temperature"]


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

#: How far down the domain the temperature series is read when the ask places no
#: point. The water furthest from where it entered has been under the weather
#: longest, which is the water the question is about; the fraction holds the node
#: off the outflow face, where the boundary condition is what it carries.
_STATION_FRAC = 0.98

#: WHERE the series is read: the point the user clicked, else that fraction
#: along the domain's own centerline. The workflow settles it onto a node of the
#: accepted mesh, so the chart is a node the run solved on.
_STATION = Placed("station", point=PARAMS.station, fraction=_STATION_FRAC,
                  label="Temperature station")

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10


class DATA:
    """The slots this run stands on - the water, the bed under it and what it
    opens at - beside the flow that fills its inflow and the week of weather the
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
    # the modelled water is shallower than the real water.
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

    # THE WEATHER the budget reads. The RAWS network is the one hourly record the
    # registry reaches without an account that carries SOLAR RADIATION beside the
    # air temperature, the humidity and the wind, and the shortwave term is what
    # drives a diurnal water temperature. The network sits on ridges and in
    # clearings tens of kilometres from the water, and how far out a station may
    # stand is the fetcher's own declared radius; which one is taken, the unit
    # carriage and the run's own clock are the Atmosphere slot's ingestion.
    # A record a user already holds STANDS OVER the network: the mark puts this
    # row on the wire, and what is handed in is read the same way the fetched
    # record is. A table of weather has no extent, so it is not checked against
    # the domain.
    weather = Data(tool("fetch_raws_weather", bbox=Ref("domain.bbox"),
                        start_time=P.weather_start,
                        end_time=P.weather_end).supplied(validate=None))
    # WHAT THE WATER OPENS AT, measured. ONE reading off the sample site nearest
    # the point the series is read at, in the unit the keyword carries, with that
    # site, its distance and the sample date on the run journal. The run spends
    # days on water that entered at a face carrying this value, so it is
    # load-bearing: nothing sampled near this domain REFUSES rather than opening
    # at a guessed temperature, and a number supplied here stands over the record.
    # The window CLOSES at the run's own moment, the way the carrier's does: a
    # run dated last winter opens at what the water carried then.
    water_temperature = Data.observation(
        tool("fetch_usgs_water_quality", bbox=Ref("domain.bbox"),
             characteristic="temperature",
             valid_time=ParamRef("event_time")),
        near=[Ref("station.lon"), Ref("station.lat")],
        measures="a water temperature", opens="the water opens at")


class STEERING(T2D):
    """The deck: a body of water under a week of weather, and the heat budget over it."""

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

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 20 m default edge the CFL step is 1 s, and the week stated below
    # is about 604,800 of them - one frame every 12,000 steps is 50 frames
    # of the week. A user who wants another cadence sets the keyword by its
    # own name.
    GRAPHIC_PRINTOUT_PERIOD = 12000

    # SEVEN DAYS. A DIURNAL RANGE is a difference between a day and its own
    # night, so the window has to hold whole days and enough of them that the
    # warmest is not the first; the RAWS network keeps a fortnight, which is
    # the ceiling the weather window can drive.
    DURATION = 604800.0

    #: The carrier declares the temperature tracer ITSELF, so the water opens at
    #: a measured temperature and carries it in at every face that feeds it. The
    #: thermic process matches that name and attaches its budget to this tracer
    #: rather than appending a second one, and the unit written here is the unit
    #: the result carries - and the unit a row that OBSERVES this variable reads
    #: its record in, so it names the scale rather than the dimension.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]
    INITIAL_VALUES_OF_TRACERS = [Ref("water_temperature.value")]

    #: The water arriving at a feeding face is the same water the sample site
    #: measured: the domain warms because of what happens OVER it, so the inflow
    #: carries the opening temperature rather than a second number.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[Ref("water_temperature.value")])

    #: The weather over the whole domain, as the one table the engine
    #: interpolates every column of between the same two rows. The nearest RAWS
    #: station whose record can drive the run end to end is the one taken. Cloud
    #: cover and atmospheric pressure are not in what that network reports, so
    #: neither column is written and the engine reads its own CLOUD COVER and
    #: VALUE OF ATMOSPHERIC PRESSURE, both of which the card shows. The engine
    #: stops at an instant outside the table, so the file is written for the
    #: DURATION this deck states rather than for a second number beside it.
    atmosphere = Atmosphere(observed=DATA.weather, at=_STATION,
                            duration_s=Ref("sheet.DURATION"))

    #: The heat budget, on the engine's own calibration constants: this question
    #: asks what the published exchange gives under real weather, so the run
    #: states no keyword of its own and the review can see every one standing at
    #: its default.
    coupling = [WAQTEL.thermal()]


#: What this question PLACES: the temperature over time at the point the ask
#: gave, as the chart, and on the map as the station that carries it.
OUTPUTS = [
    series("T1", at=_STATION).chart(),
    series("T1", at=_STATION).station(),
]
CAPTIONS = {"T1": "water temperature"}

#: The run's ANSWER, as the numbers a reader has to be able to check: how warm
#: the water at the point got and when, where it stood when the window closed,
#: how far it swings between a day and its night, how far apart the warmest and
#: the coolest water in the domain ended up - which is what the picture shows -
#: and the speed the water carried that heat at.
ANSWER = {
    "peak_temperature_c": series("T1", at=_STATION).measure("max"),
    "peak_temperature_time_s": series("T1", at=_STATION).measure("t_max"),
    "final_temperature_c": series("T1", at=_STATION).measure("last"),
    "diurnal_range_c": series("T1", at=_STATION).measure("range"),
    "temperature_spread_c": field("T1").measure("spread"),
    "mean_velocity_mps": field("M").measure("mean"),
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
    native_hint="USACE eHydro channel soundings over Copernicus GLO-30 terrain; "
                "edge sized from the domain's width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long domain is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling. A surface heat budget is "
        "divided by the local depth, so the DEPTH the mesh resolves is what "
        "moves the answer, not the planform"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the water, the week of weather, and what it opens at"


telemac_water_temperature = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The peak is a saturated maximum over a domain-scale field, so it is not a
    # resolution class. The SPREAD's warm end sits in the thinnest water there
    # is, and a coarse element averages that extreme away.
    sensitivity=(("temperature_spread_c", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_water_temperature",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_water_temperature",
                  prompt="Click on the water where the temperature should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
)
