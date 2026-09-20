"""Engine template ``telemac_dye_release`` - a conservative plume through a body of water.

TELEMAC-2D shallow water over the domain this run solves on - a river reach
walked from the release point, a lake the user draws, a polygon they own: how far
a dye, tracer or contaminant spill travels and what its peak concentration is."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    register_workflow,
    tool,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import (
    T2D,
    WAQTEL,
    field,
    max_over_time,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Rain,
    Sources,
    TracerNames,
    Wind,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.dye_release.declarations import (
    ACCEPTS, DECAY_PRESETS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_dye_release"]

#: WHERE the substance enters the water: the point the user clicked, else that
#: fraction along the domain's own centerline. The workflow settles it onto a
#: node of the accepted mesh before the sheet reads it back.
_RELEASE = Placed("source", point=PARAMS.release, fraction=PARAMS.spill_fraction,
                  label="Release point", continues=True)

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


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    # A release named up front also names which stretch to model: the reach is
    # walked downstream from it. A domain the user supplies supersedes this.
    domain = Data.domain(
        tool("fetch_river_reach", distance_km=_REACH_LENGTH_KM,
             seed_point=[Ref("release.lon"), Ref("release.lat")]))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    # THE BED, as the CLASS it is rather than the source it comes from: the
    # measurement where something measured it, the terrain under the rest. Which
    # survey or which DEM reaches this domain is the match's to answer off their
    # coverage rows, and the merge between the two classes is the runtime's one
    # rule.
    bed = Data.bed(need="bathymetry")
    # THE FLOW the inflow run prescribes, as a CLASS: which record reports a
    # discharge over this domain is the match's, and the window it reports is
    # opened at the moment the run opens at. A number stated on this row stands
    # over any record. The deck writes m3/s, so a record measured in another
    # unit is converted or refused - never read as though it were this one.
    carrier = Data.discharge(
        near=Ref("domain.centroid"), measures="a streamflow",
        opens="the carrier flow opens at", need="discharge series").optional()
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on: a gauge reports a height above its OWN zero, so the record
    # carries that zero's elevation and the offset row puts it on the run's
    # frame.
    stage = Data.level(near=Ref("domain.centroid"),
                       opens="the outflow holds at",
                       need="water level series").optional()


class STEERING(T2D):
    """The deck: a body of water, and ONE conservative tracer released into it."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
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

    #: The engine's own last instant, in the double precision a continuation
    #: reads. An uncoupled run is the only one that can be continued, so it is
    #: the only one asked to write this.
    RESTART_FILE = _RESTART

    # HOW LONG the question is asked over, in SECONDS. One hour is the window a
    # finite pulse needs to advect clear of the source and dilute into a plume
    # whose reach can be measured; shorter and the slug is still at the outfall.
    # A user who wants a longer horizon sets the keyword by its own name.
    DURATION = 3600.0

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own default
    # is every step, so an unwritten period is a frame per step: at the 14 m
    # default edge the CFL step is 0.7 s, and the 3600 s DURATION above is about
    # 5,140 of them - one frame every 100 steps is 51 frames of plume. A user
    # who wants another cadence sets the keyword by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    #: The tracer is called what the user called the release point, when a
    #: picked or named point came; the template's own name stands otherwise.
    tracer_names = TracerNames(names=["DYE             MG/L"],
                               named_by=Ref("source.name"))
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: The carrier's own boundary values, in the order the engine numbers its
    #: liquid boundaries: the walk the mesh measured, the flow the inflow run
    #: carries and the level the outflow run holds. ONE tracer, so every liquid
    #: boundary carries one clean value - the arity of that list is what moves
    #: when the question does.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0])

    #: WHERE the slug enters the water, in the mesh's own metres: the settled
    #: release point, read by position because a point is one value with an
    #: order. One element per source, in the engine's own positional order.
    ABSCISSAE_OF_SOURCES = [Ref("source.at.0")]
    ORDINATES_OF_SOURCES = [Ref("source.at.1")]
    #: HOW MUCH enters, and at what concentration: a point discharge small
    #: against the carrier flow, carrying a marker the dilution downstream is
    #: read as a fraction of. A dye question asks WHERE the slug goes and for
    #: how long it is released, never for either of these numbers.
    WATER_DISCHARGE_OF_SOURCES = [8.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    #: A FINITE pulse, so the slug advects away and dilutes instead of
    #: saturating the water.
    sources = Sources(at=_RELEASE, window_s=P.spill_duration_s,
                      until_s=Ref("settled.until_s"))

    #: CALM AND DRY: this question asks what the CURRENT does with the slug, so
    #: this deck states no surface stress and no distributed rain and the answer
    #: is the flow's alone - a zero speed and an absent rate each write nothing
    #: at all. A run that continues nothing states its own initial conditions.
    wind = Wind(speed_mps=0.0, from_deg=0.0)
    rain = Rain(mm_per_day=None, tracers=1)
    #: First-order degradation on the same tracer - no new tracer - when a
    #: decaying substance was named; nothing otherwise. This deck states no
    #: die-off of its own: the substance word picks its narrated preset.
    coupling = [WAQTEL.degradation(substance=P.decaying_substance,
                                   presets=DECAY_PRESETS)]


#: What this question PLACES: the tracer's domain-wide history as the chart.
OUTPUTS = [
    series("T1").chart(),
]
CAPTIONS = {"T1": "dye concentration"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above.
ANSWER = {
    "dye_cmax_mgl": max_over_time("T1").measure("max"),
    "dye_peak_time_s": max_over_time("T1").measure("t_max"),
    "plume_reach_m": field("T1", t="every").measure("travel_m"),
    "active_frames": field("T1", t="every").measure("active_frames"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening plume gains nothing.
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

#: The mesh gate this template stops at is the STANDARD one - the mesh step opens
#: a session, presents the built domain as an editable layer with its probes, and
#: takes every edit action the ``om2d`` mesher registers - so this template
#: declares no solver gate of its own.
_METADATA = AtomicToolMetadata(
    name="telemac_dye_release",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The engine files this run has to write for it to have solved
#: anything; unstated, the deck's own RESULTS FILE is the one.
RESULTS = (_RESULT, _RESTART)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the tracer-release scenario"


telemac_dye_release = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The dye maximum is the canonical peak class: measured 6x LOW on the coarse
    # mesh, because a concentration peak lives inside one element. How far the
    # plume REACHED is a front location and moves with it.
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location")),
    coerce=(
        point_arg("release", tool="telemac_dye_release",
                  prompt="Click on the water where the substance enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
)
