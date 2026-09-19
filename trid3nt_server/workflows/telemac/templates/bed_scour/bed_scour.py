"""Engine template ``telemac_bed_scour`` - a mobile bed under moving water.

TELEMAC-2D coupled with GAIA over the domain this run solves on - a river reach
walked from the release point, an estuary the user draws, a polygon they own:
where the bed scours and re-deposits, and whether a graded mixture SORTS as it
goes."""

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
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
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
    Sources,
    TracerNames,
    Wind,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.bed_scour.declarations import (
    ACCEPTS, DOC, GRADATION_PRESETS, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_bed_scour"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The names the run directory holds this run's files under - the deck's own
#: GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, which the workflow reads
#: back off the deck rather than being told them twice. GAIA reads the same
#: geometry and boundary files, so it is handed them by name.
_GEOMETRY = "domain.slf"
_BOUNDARY = "domain.cli"
_RESULT = "r2d_domain.slf"

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

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    # A marker named up front also names which stretch to model: the reach is
    # walked downstream from it. A domain the user supplies supersedes this.
    # The seed is on a lake rather than in a channel where no reach cuts,
    # so the producer is a LADDER: the reach, else the waterbody the seed
    # stands in, which is a closed body and names no runs.
    domain = Data.domain(
        tool("fetch_river_reach", distance_km=_REACH_LENGTH_KM,
             seed_point=[Ref("release.lon"), Ref("release.lat")])
        .ladder(tool("fetch_nhd_waterbody_at_point",
                     seed_point=[Ref("release.lon"), Ref("release.lat")])))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    # THE MEASUREMENT, and the whole reason a scour answer is worth reading: a
    # surface DEM measures the water top, so a bed painted from one is centimetres
    # deep at the banks and the shear that moves it is measured over nothing. A
    # domain with no federal navigation project has no published survey, and the
    # sheet says so rather than refusing.
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
    # ONE reading off whatever reports nearest the water, because the flow the
    # scour is driven by is a number and not a layer.
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


class STEERING(T2D):
    """The deck: moving water, a marker tracer in it, and a BED that moves under it."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
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

    # HOW LONG the question is asked over, in SECONDS. A mobile bed reads
    # through the MORPHOLOGICAL FACTOR stated below, so this hour of hydraulics
    # is ten hours of bed - long enough for a scour hole to cut and its material
    # to re-deposit downstream. A user who wants another window sets the keyword
    # by its own name.
    DURATION = 3600.0

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 14 m default edge the CFL step is 0.7 s, and the 3600 s DURATION
    # above is about 5,140 of them - one frame every 100 steps is 51 frames
    # of bed change. A user who wants another cadence sets the keyword by its
    # own name.
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    #: The marker is called what the user called the release point, when a
    #: picked or named point came; the template's own name stands otherwise.
    tracer_names = TracerNames(names=["MARKER          MG/L"],
                               named_by=Ref("source.name"))
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: The carrier's own boundary values, in the order the engine numbers its
    #: liquid boundaries: the walk the mesh measured, the flow the inflow run
    #: carries and the level the outflow run holds. The bed is GAIA's; the
    #: carrier still runs ONE tracer, so every liquid boundary carries one
    #: clean-water value.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("settled.inflow_q_m3s"),
                  "outflow_stage_m": Ref("settled.outflow_stage_m")},
        tracers=[0.0])

    #: WHERE the marker enters the water, in the mesh's own metres: the settled
    #: release point, read by position because a point is one value with an
    #: order. One element per source, in the engine's own positional order.
    ABSCISSAE_OF_SOURCES = [Ref("source.at.0")]
    ORDINATES_OF_SOURCES = [Ref("source.at.1")]
    #: HOW MUCH enters, small against the carrier flow this deck opens on.
    WATER_DISCHARGE_OF_SOURCES = [8.0]
    #: The concentration the deposited fraction downstream is measured against;
    #: the bed change itself does not scale with this number.
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    #: A FINITE pulse, so the marker advects and passes instead of holding the
    #: whole domain at a steady concentration.
    sources = Sources(window_s=P.spill_duration_s,
                      until_s=Ref("settled.until_s"))

    #: The bed itself: bedload on, one class or a mixture, a real stock to scour
    #: into. What the dictionary has no keyword for is the GRADATION - a named
    #: mixture or the pairs one is written as - and its class table is expanded
    #: LAST, so a mixture wins over the single class stated beside it.
    coupling = [GAIA.bed(geometry=_GEOMETRY, boundary=_BOUNDARY,
                         gradation=P.sediment_gradation, presets=GRADATION_PRESETS,
                         # 100 um very fine sand, in the metres the keyword
                         # reads: the finest class the bed-load formulae treat
                         # as sand (silt below about 63 um is cohesive and they
                         # are not written for it), so a scour answer stands on
                         # a bed that moves; the whole bed where no gradation
                         # is given, and one number across the mesh until a
                         # bed-material field fills the class fractions.
                         CLASSES_SEDIMENT_DIAMETERS=[1.0e-4],
                         # Five metres of erodible stock, deeper than any cut
                         # this question makes, so the answer is never
                         # stock-limited.
                         LAYERS_INITIAL_THICKNESS=[5.0],
                         # Meyer-Peter-Mueller, the bed-load law a sand bed under
                         # a channel flow is screened with; the dictionary
                         # publishes all ten choices.
                         BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=1,
                         # The classes of a MIXTURE shelter each other, and 1 is
                         # the engine's own Egiazaroff hiding factor; a single
                         # class hides behind nothing and never reads it.
                         HIDING_FACTOR_FORMULA=1,
                         # What makes a short window produce a readable bed
                         # change: bed evolution is amplified ten times against
                         # the hydraulic clock.
                         MORPHOLOGICAL_FACTOR=10.0,
                         # The listing's own sediment balance is what the net bed
                         # mass is read off.
                         MASS_BALANCE=True)]

    #: CALM AND DRY: this question asks what the CARRIER FLOW does to the bed, so
    #: this deck states no surface stress and no distributed rain - a zero speed
    #: and an absent rate each write nothing at all. A user who wants either
    #: states SPEED AND DIRECTION OF WIND or RAIN OR EVAPORATION IN MM PER DAY
    #: by name; the term each value needs is armed by the ingestion.
    wind = Wind(speed_mps=0.0, from_deg=0.0)
    rain = Rain(mm_per_day=None, tracers=1)


#: What this question PLACES: the marker's domain-wide history as the chart.
OUTPUTS = [
    series("T1").chart(),
]
CAPTIONS = {"T1": "marker concentration"}

#: The run's ANSWER, as the numbers a reader has to be able to check. The
#: evolution is signed: deposition positive, scour negative. The surface D50
#: spread is the sorting signature, in the metres the module writes: one class
#: cannot sort, so a single-class bed reads zero and a mixture reads its grading.
ANSWER = {
    "bed_evolution_max_m": field("E", t=-1, module="gaia").measure("max"),
    "bed_evolution_min_m": field("E", t=-1, module="gaia").measure("min"),
    "net_bed_mass_kg": mass_balance(module="gaia").measure("sediment_net_bed_mass_kg"),
    "surface_d50_spread_m": field("D50", t=-1, module="gaia").measure("spread"),
    "marker_cmax_mgl": max_over_time("T1").measure("max"),
    "active_frames": series("T1").measure("active_frames"),
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
    native_hint="USACE eHydro channel soundings over Copernicus GLO-30 terrain; "
                "edge sized from the domain's own geometry",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a large domain is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_bed_scour",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_bed_scour = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        produce=(
            # WHERE the marker enters the water, settled against the accepted
            # mesh before the sheet reads it. The domain rides along because an
            # unplaced marker sits along its centerline companion, and a placed
            # one is held on that same line.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.release, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": P.spill_fraction,
                         "label": "Release point"}).named("source"),),
        results=(_RESULT, RESULT_FILENAME),
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the mobile-bed scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # Scour and deposition maxima live inside single elements, so a coarse mesh
    # reads both low.
    sensitivity=(("bed_evolution_max_m", "peak"),
                 ("bed_evolution_min_m", "peak")),
    coerce=(
        point_arg("release", tool="telemac_bed_scour",
                  prompt="Click on the water where the sediment marker is injected",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
    doc=DOC,
)
