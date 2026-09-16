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
from trid3nt_server.inputs import point_arg, user_input
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
    Release,
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

#: How far back a channel survey still describes the bed the water is moving
#: over. Older soundings are still the measurement a terrain surface is not.
_SURVEY_SINCE = "2015-01-01"


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    # A marker named up front also names which stretch to model: the reach is
    # walked downstream from it. A domain the user supplies supersedes this.
    domain = Data.domain(tool("fetch_river_reach",
                              seed_point=[Ref("release.lon"), Ref("release.lat")],
                              distance_km=_REACH_LENGTH_KM))
    # THE MEASUREMENT, and the whole reason a scour answer is worth reading: a
    # surface DEM measures the water top, so a bed painted from one is centimetres
    # deep at the banks and the shear that moves it is measured over nothing. A
    # domain with no federal navigation project has no published survey, and the
    # sheet says so rather than refusing.
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
    terrain = Data(tool("fetch_copernicus_dem", bbox=Ref("domain.bbox"),
                        px_per_deg=3600.0, purpose="bed elevation"))
    # ONE bed: the survey where it measured, the terrain everywhere else. With
    # the survey absent the terrain passes through unchanged, and the merge
    # refuses two vertical datums by name rather than writing a step into the
    # bed an evolution depth is then read off.
    bed = Data.bed(tool("derive_merge_rasters", primary=surveyed_bed,
                        fallback=terrain))
    # The carrier flow the inflow run prescribes, where the user stated none.
    # ONE reading off whatever reports nearest the water, because the flow the
    # scour is driven by is a number and not a layer.
    carrier = Data.observation(
        tool("fetch_noaa_nwm_streamflow", bbox=Ref("domain.bbox"),
             valid_time=ParamRef("event_time")),
        near=Ref("domain.centroid"), value_field="streamflow_cms",
        measures="a streamflow", opens="the carrier flow opens at"
    ).context("the National Water Model published no streamflow over this "
              "domain at that cycle")


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

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

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
                  "inflow_q_m3s": Ref("channel.inflow_q_m3s"),
                  "outflow_stage_m": Ref("channel.outflow_stage_m")},
        tracers=[0.0])

    #: The marker the bed change is watched against, released as a finite pulse
    #: at a point source inside the domain.
    releases = [Release(at=Ref("source.at"), q=P.source_q_m3s,
                        tracers=[P.tracer_concentration_mgl],
                        window_s=P.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The bed itself: one class or a mixture, bedload on, a real stock to scour
    #: into. The classes of a MIXTURE shelter each other, and formula 1 is the engine's own Egiazaroff
    #: hiding factor; a single class hides behind nothing and never reads it.
    #: The listing's own sediment balance is what the net bed mass is read off.
    coupling = [GAIA.bed(geometry=_GEOMETRY, boundary=_BOUNDARY,
                         gradation=P.sediment_gradation, presets=GRADATION_PRESETS,
                         d50_um=P.grain_size_um, thickness_m=P.bed_thickness_m,
                         formula=P.bedload_formula, hiding_factor_formula=1,
                         morphological_factor=P.morphological_factor,
                         mass_balance=True)]

    wind = Wind(speed_mps=P.wind_speed_mps, from_deg=P.wind_direction_deg)
    rain = Rain(mm_per_day=P.rainfall_mm_per_day, tracers=1)


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
            # WHERE the marker enters the water, settled against the accepted
            # mesh before the sheet reads it. The domain rides along because an
            # unplaced marker sits along its centerline companion, and a placed
            # one is held on that same line.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.release, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": P.spill_fraction,
                         "label": "Release point"}).named("source")),
        results=(_RESULT, RESULT_FILENAME),
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the mobile-bed scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
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
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
)
