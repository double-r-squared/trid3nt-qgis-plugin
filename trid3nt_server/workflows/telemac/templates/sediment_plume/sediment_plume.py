"""Engine template ``telemac_sediment_plume`` - a settling plume in a body of water.

TELEMAC-2D coupled with GAIA over the domain this run solves on - a river reach
walked from the release point, a harbour or lake the user draws, a polygon they
own - with ONE settling class over a bed with NO stock at all, so nothing erodes
and only what was injected can deposit."""

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
from trid3nt_server.workflows.telemac.templates.sediment_plume.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
    SEDIMENT_CONCENTRATION_MGL, SOURCE_Q_M3S,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_sediment_plume"]

#: WHERE the substance enters the water: the point the user clicked, else that
#: fraction along the domain's own centerline. The workflow settles it onto a
#: node of the accepted mesh before the sheet reads it back.
_RELEASE = Placed("source", point=PARAMS.release, fraction=PARAMS.spill_fraction,
                  label="Release point")

#: The names the run directory holds this run's files under, stated once on the
#: deck: GAIA reads the hydrodynamic geometry and boundary file by name, and the
#: workflow reads the same three statements off the body rather than a restatement.
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


class DATA:
    """The slots this run stands on - the domain and the bed composed over it -
    and the one reading its inflow run opens on."""

    # A release named up front also names which stretch to model: the reach is
    # walked downstream from it. A domain the user supplies supersedes this.
    domain = Data.need("hydrography", at=Ref("release"),
                       span_km=_REACH_LENGTH_KM)
    # THE BED, as the CLASS it is rather than the source it comes from: the
    # measurement where something measured it, the terrain under the rest. Which
    # survey or which DEM reaches this domain is the match's to answer off their
    # coverage rows, and the merge between the two classes is the runtime's one
    # rule. A domain with no federal navigation project has no published
    # survey, and the sheet says so rather than refusing.
    bed = Data.need("bathymetry")
    # The carrier flow the inflow run prescribes, as a CLASS: which record
    # reports a discharge over this domain is the match's, and the window it
    # reports is opened at the moment the run opens at. A number stated on this
    # row stands over any record, so the flow is the slot's and no param twins
    # it.
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this domain "
        "at that cycle")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on - a gauge publishes its height above its own zero, which is a
    # different surface, so no source is named here and the number is stated.
    level = Data.need("water level series").optional()


class STEERING(T2D):
    """The deck: a body of water, and ONE settling class released into it."""

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

    # HOW LONG the question is asked over, in SECONDS. One hour is what a finite
    # pulse of a settling class needs to advect clear of the source, spread and
    # drop the coarse end of itself onto the bed; shorter and nothing has landed
    # yet. A user who wants a longer horizon sets the keyword by its own name.
    DURATION = 3600.0

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own default
    # is every step, so an unwritten period is a frame per step: at the 14 m
    # default edge the CFL step is 0.7 s, and the 3600 s DURATION above is about
    # 5,140 of them - one frame every 100 steps is 51 frames of the plume. A user
    # who wants another cadence sets the keyword by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    #: The marker is called what the user called the release point, when a
    #: picked or named point came; the template's own name stands otherwise.
    tracer_names = TracerNames(names=["MARKER          MG/L"],
                               named_by=Ref("source.name"))
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: TWO values on every liquid boundary - the carrier's own tracer and the
    #: suspended class behind it - or the solver refuses for want of values.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0, 0.0])

    #: WHERE the marker enters the water, in the mesh's own metres: the settled
    #: release point, read by position because a point is one value with an
    #: order. One element per source, in the engine's own positional order.
    ABSCISSAE_OF_SOURCES = [Ref("source.at.0")]
    ORDINATES_OF_SOURCES = [Ref("source.at.1")]
    #: HOW MUCH enters, and at what concentration: the deck's own fixed source
    #: strength, small against the carrier flow so the pulse is a marker and
    #: not a flood - the question's own input is the spill WINDOW below.
    WATER_DISCHARGE_OF_SOURCES = [SOURCE_Q_M3S]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [SEDIMENT_CONCENTRATION_MGL]
    #: A FINITE pulse, so the slug advects away and dilutes instead of
    #: saturating the water.
    sources = Sources(at=_RELEASE, window_s=P.spill_duration_s,
                      until_s=Ref("settled.until_s"))

    #: The settling class itself, over a bed the composite lays at zero initial
    #: thickness: nothing erodes and only the injected pulse can deposit. The
    #: source concentration is the same fixed number VALUES OF THE TRACERS AT
    #: THE SOURCES states above, restated here because GAIA reads its own
    #: keyword for it.
    coupling = [GAIA.suspended(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        concentration_mgl=SEDIMENT_CONCENTRATION_MGL,
        # ONE class, 30 um fine silt in the keyword's own metres - the class
        # this question is asked of, a fraction that travels as a plume at the
        # currents a release reach carries rather than settling where it
        # enters. GAIA is a COUPLED body and the keywords floor reaches only
        # the carrier's own dictionary, so nothing on the call can state a
        # class the composite does not.
        CLASSES_SEDIMENT_DIAMETERS=[3.0e-5],
        # Zyserman-Fredsoe, of the suspension formulae GAIA offers. It is a
        # reference concentration the deck reads for its settling class, which
        # is non-cohesive at this diameter, so the deck states no CLASSES TYPE
        # OF SEDIMENT to say otherwise.
        SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=3,
        # The character-of-the-flow scheme a PULSE advected over a bed with no
        # stock needs; the dictionary's own upwind pair is for a resident
        # concentration field.
        SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=[1],
        # GAIA's own sediment closure, beside the water volume the carrier
        # accounts for: the net bed mass this answer reads is a line of it.
        MASS_BALANCE=True)]

    #: CALM AND DRY: this question asks what the CURRENT does with the injected
    #: class, so this deck states no surface stress and no distributed rain and
    #: the settling is the flow's alone - a zero speed and an absent rate each
    #: write nothing at all.
    wind = Wind(speed_mps=0.0, from_deg=0.0)
    rain = Rain(mm_per_day=None, tracers=2)


#: What this question PLACES: the suspended class's domain-wide history as the
#: chart.
OUTPUTS = [
    series("T2").chart(),
]
CAPTIONS = {"T2": "suspended sediment concentration", "discharge": "a streamflow"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above; the deposited fraction is the listing's
#: deposited mass over the mass the sheet says the pulse put in.
ANSWER = {
    "suspended_cmax": max_over_time("T2").measure("max"),
    "suspended_peak_time_s": max_over_time("T2").measure("t_max"),
    "plume_reach_m": field("T2", t="every").measure("travel_m"),
    "active_frames": field("T2", t="every").measure("active_frames"),
    "bed_evolution_max_m": field("E", t=-1, module="gaia").measure("max"),
    "net_bed_mass_kg": mass_balance(module="gaia").measure("sediment_net_bed_mass_kg"),
    "deposit_fraction": mass_balance(module="gaia").measure(
        "sediment_deposited_mass_kg").over(P.injected_mass_kg),
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
    name="telemac_sediment_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The engine files this run has to write for it to have solved
#: anything; unstated, the deck's own RESULTS FILE is the one.
RESULTS = (_RESULT, RESULT_FILENAME)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the sediment-plume scenario"


telemac_sediment_plume = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The suspended maximum is the canonical peak class: a concentration peak
    # lives inside one element. How far the plume REACHED is a front location and
    # moves with it, and the bed evolution is a peak over the same elements.
    sensitivity=(("suspended_cmax", "peak"),
                 ("plume_reach_m", "location"),
                 ("bed_evolution_max_m", "peak")),
    coerce=(
        point_arg("release", tool="telemac_sediment_plume",
                  prompt="Click on the water where the sediment enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
