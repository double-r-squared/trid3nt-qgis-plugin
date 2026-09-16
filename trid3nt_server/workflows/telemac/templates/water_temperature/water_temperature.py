"""Engine template ``telemac_water_temperature`` - how warm a body of water gets.

TELEMAC-2D coupled with WAQTEL THERMIC over the domain the run is given: the full
surface heat budget - shortwave in, longwave out, evaporation and sensible heat -
driven by the hourly weather record over the days the ask names. The water opens
at a MEASURED temperature and carries that value in at every face that feeds it."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    Step,
    register_workflow,
    tool,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, field, mesh, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Atmosphere, Boundaries
from trid3nt_server.workflows.telemac.templates.water_temperature.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_water_temperature"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

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

#: How far back a channel survey still describes the bed the water is moving
#: over. Older soundings are still the measurement a terrain surface is not.
_SURVEY_SINCE = "2015-01-01"

#: How far down the domain the temperature series is read when the ask places no
#: point. The water furthest from where it entered has been under the weather
#: longest, which is the water the question is about; the fraction holds the node
#: off the outflow face, where the boundary condition is what it carries.
_STATION_FRAC = 0.98


class DATA:
    """The slots this run stands on - the water, the bed under it and what it
    opens at - beside the flow that fills its inflow and the week of weather the
    heat budget is driven by."""

    # THE DOMAIN. A polygon the caller supplies or draws supersedes this; unfilled,
    # the reach the seed stands on is cut from the mapped water and arrives with
    # its two end transects, which is where the inflow and the outflow are
    # prescribed. A closed body states no run and its whole edge is wall.
    domain = Data.domain(tool("fetch_river_reach",
                              seed_point=[Ref("seed.lon"), Ref("seed.lat")],
                              distance_km=_REACH_LENGTH_KM))

    # THE MEASUREMENT. Water with no federal navigation project has no published
    # sounding, and the sheet says so rather than refusing.
    survey = Data(tool("fetch_ehydro_surveys", bbox=Ref("domain.bbox"),
                       since=_SURVEY_SINCE)
                  ).context("no published channel survey over this domain; "
                            "the terrain surface stands")
    # A survey is SOUNDINGS; the surface between them is a derive, and it is
    # context for the same reason its input is.
    surveyed_bed = Data(tool("derive_survey_surface", points=survey,
                             resolution_m=ParamRef("mesh_resolution_m"))
                        ).context("the survey held no soundings to grid; "
                                  "the terrain surface stands")
    # GLO-30 is asked for on its OWN 1-arcsecond lattice, so the raster the nodes
    # are sampled from carries the source pixels rather than a resample of them.
    # A surface DEM measures the water top, so where no survey reaches it the
    # modelled water is shallower than the real water - and a surface heat budget
    # is divided by the local depth, so the shallow half warms faster.
    terrain = Data(tool("fetch_copernicus_dem", bbox=Ref("domain.bbox"),
                        px_per_deg=3600.0, purpose="bed elevation"))
    # ONE bed: the survey where it measured, the terrain everywhere else. With
    # the survey absent the terrain passes through unchanged and the merge says
    # which side was missing.
    bed = Data.bed(tool("derive_merge_rasters", primary=surveyed_bed,
                        fallback=terrain))

    # The carrier flow the inflow run prescribes, where the user stated none.
    # ONE reading, not the grid the model published: which reach segment reports
    # it is ranked against the domain's own interior point, and the step that
    # opens the channel refuses a record nobody chose from.
    carrier = Data.observation(
        tool("fetch_noaa_nwm_streamflow", bbox=Ref("domain.bbox"),
             valid_time=ParamRef("event_time")),
        near=Ref("domain.centroid"), value_field="streamflow_cms",
        measures="a streamflow", opens="the carrier flow opens at"
    ).context("the National Water Model published no streamflow over this "
              "domain at that cycle")

    # THE WEATHER the budget reads. The RAWS network is the one hourly record the
    # registry reaches without an account that carries SOLAR RADIATION beside the
    # air temperature, the humidity and the wind, and the shortwave term is what
    # drives a diurnal water temperature. The network sits on ridges and in
    # clearings tens of kilometres from the water, and how far out a station may
    # stand is the fetcher's own declared radius; which one is taken, the unit
    # carriage and the run's own clock are the Atmosphere slot's ingestion.
    weather = Data(tool("fetch_raws_weather", bbox=Ref("domain.bbox"),
                        start_time=P.weather_start, end_time=P.weather_end))
    # WHAT THE WATER OPENS AT, measured. ONE reading off the sample site nearest
    # the point the series is read at, in the unit the keyword carries, with that
    # site, its distance and the sample date on the run journal. The run spends
    # days on water that entered at a face carrying this value, so it is
    # load-bearing: nothing sampled near this domain REFUSES rather than opening
    # at a guessed temperature, and a number supplied here stands over the record.
    water_temperature = Data.observation(
        tool("fetch_usgs_water_quality", bbox=Ref("domain.bbox"),
             characteristic="temperature"),
        near=[Ref("station.lon"), Ref("station.lat")], units="degC",
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

    # The run OPENS at the derived normal depth, laid bed-parallel. Not a
    # constant elevation at the outflow stage: the stage is derived only where
    # the water FALLS, so a horizontal surface at the outlet's level leaves every
    # node upstream of it dry - the flowrate face among them - and the engine
    # refuses a discharge it has no water to impose.
    INITIAL_CONDITIONS = "CONSTANT DEPTH"
    INITIAL_DEPTH = Ref("channel.depth_m")

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

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    #: The carrier declares the temperature tracer ITSELF, so the water opens at
    #: a measured temperature and carries it in at every face that feeds it. The
    #: thermic process matches that name and attaches its budget to this tracer
    #: rather than appending a second one, and the unit written here is the unit
    #: the result carries.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEG"]
    INITIAL_VALUES_OF_TRACERS = [Ref("water_temperature.value")]

    #: The water arriving at a feeding face is the same water the sample site
    #: measured: the domain warms because of what happens OVER it, so the inflow
    #: carries the opening temperature rather than a second number.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("channel.inflow_q_m3s"),
                  "outflow_stage_m": Ref("channel.outflow_stage_m")},
        tracers=[Ref("water_temperature.value")])

    #: The weather over the whole domain, as the one table the engine
    #: interpolates every column of between the same two rows. The nearest RAWS
    #: station whose record can drive the run end to end is the one taken. Cloud
    #: cover and atmospheric pressure are not in what that network reports, so
    #: neither column is written and the engine reads its own CLOUD COVER and
    #: VALUE OF ATMOSPHERIC PRESSURE, both of which the card shows.
    atmosphere = Atmosphere(observed=DATA.weather, at=Ref("station"),
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

#: The run's ANSWER, as the numbers a reader has to be able to check: how warm
#: the water at the point got and when, where it stood when the window closed,
#: how far it swings between a day and its night, how far apart the warmest and
#: the coolest water in the domain ended up - which is what the picture shows -
#: and the speed the water carried that heat at.
ANSWER = {
    "peak_temperature_c": series("T1", at=Ref("station")).measure("max"),
    "peak_temperature_time_s": series("T1", at=Ref("station")).measure("t_max"),
    "final_temperature_c": series("T1", at=Ref("station")).measure("last"),
    "diurnal_range_c": series("T1", at=Ref("station")).measure("range"),
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


telemac_water_temperature = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        produce=(
            # The open-channel hydraulics this question needs on top of the
            # domain: the carrier flow the inflow prescribes, the level the
            # outflow holds, and the depth the run opens at, all measured over
            # the accepted mesh at the roughness the deck is written at.
            Step(runner=f"{_AUTHORING}.assembler.settle_open_channel",
                 stage="author",
                 kwargs={"mesh": Ref("mesh"), "carrier": Ref("carrier"),
                         "discharge_m3s": P.discharge_m3s,
                         "friction_law": _FRICTION_LAW,
                         "friction_coefficient": _FRICTION_COEFFICIENT}
                 ).named("channel"),
            # WHERE the series is read, settled against the accepted mesh before
            # the outputs are anchored: the chart is a node the run solved on.
            # The domain rides along because an unplaced station sits its
            # fraction along that domain's centerline companion, and a supplied
            # point is held inside the water the same way.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.station, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": _STATION_FRAC,
                         "label": "Temperature station"}).named("station")),
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the water, the week of weather, and what it opens at"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
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
    doc=DOC,
)
