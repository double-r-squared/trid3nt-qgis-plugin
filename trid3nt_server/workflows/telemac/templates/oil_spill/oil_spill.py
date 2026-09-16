"""Engine template ``telemac_oil_spill`` - an oil slick on the water it is spilled onto.

TELEMAC-2D shallow water over the domain this run solves on - a river reach
walked from the release point, a harbour the user draws, a polygon they own -
with the engine's own oil-spill module riding on the solve: the module tracks
floating particles and the tracer carries what dissolved."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    Step,
    register_workflow,
    tool,
)
from trid3nt_server.inputs import point_arg, user_input
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import (
    T2D,
    field,
    max_over_time,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.outputs import drogues
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    DROGUES_FILENAME,
    Boundaries,
    Oil,
    Rain,
    Release,
    TracerNames,
    Wind,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.oil_spill.declarations import (
    ACCEPTS, DOC, OIL_PRESETS, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_oil_spill"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The files the run directory holds beside the deck's own statements. The
#: restart is the engine's full state at its last instant in double precision,
#: which is what a continuation reads; the results file is a picture of the run.
_RESULT = "r2d_domain.slf"
_RESTART = "restart_domain.slf"

#: How far the reach producer walks downstream from the release point when this
#: question has to find its own domain. A user who wants another stretch supplies
#: the domain polygon, which supersedes the producer.
_REACH_LENGTH_KM = 6.0

#: The roughness this deck is solved at, and the law it is read under: Strickler,
#: the coefficient an unsurveyed channel is screened at. ONE number, stated once,
#: because the outflow stage is derived as a normal depth AT this roughness and a
#: stage derived at one number under a deck written at another is a level the run
#: never sits at. A user who knows the channel sets the keyword by name.
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

#: How far back a channel survey still describes the bed the water is moving
#: over. Older soundings are still the measurement a terrain surface is not.
_SURVEY_SINCE = "2015-01-01"


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    # A release named up front also names which stretch to model: the reach is
    # walked downstream from it. A domain the user supplies supersedes this.
    domain = Data.domain(tool("fetch_river_reach",
                              seed_point=[Ref("release.lon"), Ref("release.lat")],
                              distance_km=_REACH_LENGTH_KM))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    # THE MEASUREMENT. A domain with no federal navigation project has no
    # published survey, and the sheet says so rather than refusing.
    survey = Data(tool("fetch_ehydro_surveys", bbox=Ref("domain.bbox"),
                       since=_SURVEY_SINCE)
                  ).context("no published channel survey over this domain; "
                            "the terrain surface stands")
    # The survey is SOUNDINGS; the surface between them is a derive, and it is
    # context for the same reason its input is.
    surveyed_bed = Data(tool("derive_survey_surface", points=survey,
                             resolution_m=ParamRef("mesh_resolution_m"))
                        ).context("the survey held no soundings to grid; "
                                  "the terrain surface stands")
    # GLO-30 is asked for on its OWN 1-arcsecond lattice, so the raster the nodes
    # are sampled from carries the source pixels rather than a resample of them.
    # A surface DEM measures the water top, so where no survey reaches it the
    # modelled channel is shallower than the real one.
    terrain = Data(tool("fetch_copernicus_dem", bbox=Ref("domain.bbox"),
                        px_per_deg=3600.0, purpose="bed elevation"))
    # ONE bed: the survey where it measured, the terrain everywhere else. With
    # the survey absent the terrain passes through unchanged and the result says
    # which side was missing.
    bed = Data.bed(tool("derive_merge_rasters", primary=surveyed_bed,
                        fallback=terrain))
    # The carrier flow the inflow run prescribes. A number stated on this row
    # stands over any record, so the flow is the slot's and no param twins it.
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
    # THE LEVEL THE OUTFLOW HOLDS where the reach does not FALL. A reach whose
    # bed is a surface DEM has the water top for a floor and no fall between its
    # ends, so there is no uniform-flow depth to derive and the outflow holds at
    # a level somebody measured instead. An ELEVATION on the datum the bed is
    # painted on - a gauge publishes its height above its own zero, which is a
    # different surface, so no source is named here and the number is stated.
    stage = Data.observation(near=Ref("domain.centroid"), units="m",
                             measures="a water-surface elevation",
                             opens="the outflow holds at").optional()


class STEERING(T2D):
    """The deck: a body of water, a slick on it, and the fraction that dissolved."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    # The step the domain is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for.
    TIME_STEP = Ref("settled.time_step_s")
    LISTING_PRINTOUT_PERIOD = 500

    # The run OPENS at the derived normal depth, laid bed-parallel. Not a
    # constant elevation at the outflow stage: the stage is derived only where
    # the domain FALLS, so a horizontal surface at the outlet's level leaves every
    # node upstream of it dry - the flowrate face among them - and the engine
    # refuses a discharge it has no water to impose.
    INITIAL_CONDITIONS = "CONSTANT DEPTH"
    INITIAL_DEPTH = Ref("channel.depth_m")

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    # The advection of momentum and depth, and the SUPG the domain is stable
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

    #: The engine's own last instant, in the double precision a continuation
    #: reads. This run couples nothing, so it can write it.
    RESTART_FILE = _RESTART
    PREVIOUS_COMPUTATION_FILE_FORMAT = "SERAFIND"

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    #: The tracer is called what the user called the release point, when a
    #: picked or named point came; the template's own name stands otherwise.
    tracer_names = TracerNames(names=["OIL             MG/L"],
                               named_by=Ref("source.name"))
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: The carrier's own boundary values, in the order the engine numbers its
    #: liquid boundaries: the walk the mesh measured, the flow the inflow run
    #: carries and the level the outflow run holds. ONE tracer, so every liquid
    #: boundary carries one clean value.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("channel.inflow_q_m3s"),
                  "outflow_stage_m": Ref("channel.outflow_stage_m")},
        tracers=[0.0])

    #: The dissolved fraction, released as a FINITE pulse at the same point the
    #: floats are compiled to enter at.
    releases = [Release(at=Ref("source.at"), q=P.source_q_m3s,
                        tracers=[P.oil_concentration_mgl],
                        window_s=P.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The module itself: the preset the deck carries under the name the ask
    #: chose, the per-run source the settled point is compiled into, and the
    #: floats the slick is drawn from.
    oil = Oil(presets=OIL_PRESETS, named=P.oil_type, at=Ref("source.at"),
              release_step=P.oil_release_step, drogues=P.n_drogues,
              drogues_period_s=P.drogues_period_s,
              time_step_s=Ref("settled.time_step_s"))

    wind = Wind(speed_mps=P.wind_speed_mps, from_deg=P.wind_direction_deg)
    rain = Rain(mm_per_day=P.rainfall_mm_per_day, tracers=1)


#: What this question PLACES: the dissolved fraction's domain-wide history as
#: the chart, and the floats' track, which is no field on the mesh at all.
OUTPUTS = [
    series("T1").chart(),
    drogues().layer(),
]
CAPTIONS = {"T1": "dissolved oil concentration", "drogues": "oil slick track"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above.
ANSWER = {
    "oil_cmax_mgl": max_over_time("T1").measure("max"),
    "oil_peak_time_s": max_over_time("T1").measure("t_max"),
    "plume_reach_m": field("T1", t="every").measure("travel_m"),
    "active_frames": field("T1", t="every").measure("active_frames"),
    "slick_drift_m": drogues().measure("drift_m"),
    "floats_released": drogues().measure("released"),
    "floats_remaining": drogues().measure("remaining"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a large domain
#: WITHIN this declaration, and the effective edge stays >= 2 cells across it.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="the domain's own geometry + the bed it is painted from",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a large domain is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_oil_spill",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_oil_spill = register_workflow(
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
                         "stage": Ref("stage"),
                         "friction_law": _FRICTION_LAW,
                         "friction_coefficient": _FRICTION_COEFFICIENT}
                 ).named("channel"),
            # WHERE the source enters the water, settled against the accepted
            # mesh before the sheet reads it. The domain rides along because an
            # unplaced release sits its fraction along that domain's centerline
            # companion, and a supplied point is snapped onto the same line.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.release, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": P.spill_fraction,
                         "label": "Release point"}).named("source")),
        results=(_RESULT, _RESTART, DROGUES_FILENAME),
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the oil spill scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The dissolved maximum is the canonical peak class: a concentration peak
    # lives inside one element. How far the slick REACHED is a front location and
    # moves with it.
    sensitivity=(("oil_cmax_mgl", "peak"),
                 ("plume_reach_m", "location")),
    coerce=(
        point_arg("release", tool="telemac_oil_spill",
                  prompt="Click on the water where the oil enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
)
