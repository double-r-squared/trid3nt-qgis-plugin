"""Engine template ``telemac_oil_spill`` - an oil slick on the water it is spilled onto.

TELEMAC-2D shallow water over the domain this run solves on - a river reach
walked from the release point, a harbour the user draws, a polygon they own -
with the engine's own oil-spill module riding on the solve: the module tracks
floating particles and the tracer carries what dissolved."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    Ref,
    register_workflow,
)
from trid3nt_server.inputs import point_arg
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
    Sources,
    TracerNames,
    Wind,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.oil_spill.declarations import (
    ACCEPTS, DOC, OIL_PRESETS, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_oil_spill"]

#: WHERE the substance enters the water: the point the user clicked, else that
#: fraction along the domain's own centerline. The workflow settles it onto a
#: node of the accepted mesh before the sheet reads it back.
_RELEASE = Placed("source", point=PARAMS.release, fraction=PARAMS.spill_fraction,
                  label="Release point")

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
    domain = Data.need("hydrography", at=Ref("release"), span_km=_REACH_LENGTH_KM)
    # THE BED, as the CLASS it is rather than the source it comes from: the
    # measurement where something measured it, the terrain under the rest. Which
    # survey or which DEM reaches this domain is the match's to answer off their
    # coverage rows, and the merge between the two classes is the runtime's one
    # rule.
    bed = Data.need("bathymetry")
    # THE FLOW the inflow run prescribes, as a CLASS: which record reports a
    # discharge over this domain is the match's, and the window it reports is
    # opened at the moment the run opens at. A number stated on this row stands
    # over any record, and the unit it is read in is the unit of the keyword
    # the slot fills.
    discharge = Data.need("discharge series")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on: a gauge reports a height above its OWN zero, so the record
    # carries that zero's elevation and the offset row puts it on the run's
    # frame.
    level = Data.need("water level series").optional()


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
    #: reads. This run couples nothing, so it can write it.
    RESTART_FILE = _RESTART
    PREVIOUS_COMPUTATION_FILE_FORMAT = "SERAFIND"

    # HOW LONG the question is asked over, in SECONDS. One hour is the window a
    # slick needs to leave the release point and spread far enough for its drift
    # to be measured against the flow; shorter and the floats are still bunched
    # at the source. A user who wants a longer horizon sets the keyword by its
    # own name.
    DURATION = 3600.0

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 14 m default edge the CFL step is 0.7 s, and the 3600 s DURATION
    # above is about 5,140 of them - one frame every 100 steps is 51 frames
    # of the slick. A user who wants another cadence sets the keyword by its
    # own name.
    GRAPHIC_PRINTOUT_PERIOD = 100

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
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0])

    #: WHERE the slick enters the water, in the mesh's own metres: the settled
    #: release point, read by position because a point is one value with an
    #: order. One element per source, in the engine's own positional order.
    ABSCISSAE_OF_SOURCES = [Ref("source.at.0")]
    ORDINATES_OF_SOURCES = [Ref("source.at.1")]
    #: HOW MUCH enters, and at what concentration: a point discharge small
    #: against the carrier flow, at the concentration the dissolved-oil answer
    #: is measured against. What an oil question states is where the slick goes
    #: and which oil it is, so neither number is asked for.
    WATER_DISCHARGE_OF_SOURCES = [8.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    #: The dissolved fraction, released as a FINITE pulse at the same point the
    #: floats are compiled to enter at.
    sources = Sources(at=_RELEASE, window_s=P.spill_duration_s,
                      until_s=Ref("settled.until_s"))

    #: The module itself: the preset the deck carries under the name the ask
    #: chose, and the per-run source the settled point is compiled into.
    oil = Oil(presets=OIL_PRESETS, named=P.oil_type, at=Ref("source.at"),
              release_step=P.oil_release_step)

    # HOW MANY floats the module tracks. The slick is drawn from their
    # positions alone, so the count is the resolution of the picture: a hundred
    # over a domain this question walks is a readable drift cloud, and a coarser
    # count draws a coarser slick.
    MAXIMUM_NUMBER_OF_DROGUES = 100

    # HOW OFTEN their positions are written, in SOLVER STEPS like the graphic
    # period above and sized the same way: 5,140 steps of the stated DURATION at
    # one write every 60 is about 86 positions along each track, which draws the
    # drift without writing a file the reader cannot scrub.
    PRINTOUT_PERIOD_FOR_DROGUES = 60

    #: CALM AND DRY: this question asks what the CURRENT does with the slick, so
    #: this deck states no surface stress and no distributed rain and the drift
    #: is the flow's alone - a zero speed and an absent rate each write nothing
    #: at all. A user who wants a wind states SPEED AND DIRECTION OF WIND by
    #: name; the term it needs is armed by the ingestion.
    wind = Wind(speed_mps=0.0, from_deg=0.0)
    rain = Rain(mm_per_day=None, tracers=1)


#: What this question PLACES: the dissolved fraction's domain-wide history as
#: the chart, and the floats' track, which is no field on the mesh at all.
OUTPUTS = [
    series("T1").chart(),
    drogues().layer(),
]
CAPTIONS = {"T1": "dissolved oil concentration", "drogues": "oil slick track",
            "discharge": "a streamflow", "level": "a water-surface elevation"}

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
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The engine files this run has to write for it to have solved
#: anything; unstated, the deck's own RESULTS FILE is the one.
RESULTS = (_RESULT, _RESTART, DROGUES_FILENAME)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the oil spill scenario"


telemac_oil_spill = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
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
    ),
)
