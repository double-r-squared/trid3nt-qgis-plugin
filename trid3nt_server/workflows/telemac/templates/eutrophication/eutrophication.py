"""Engine template ``telemac_eutrophication`` - what one pass through
enriched water does to it.

TELEMAC-2D coupled with WAQTEL's eutrophication process over the domain the run
is given: phytoplankton growing on stated nitrate and phosphate under stated
light and temperature, and the oxygen budget that growth, its decay and the
nitrification of its ammonium drive. Moving water flushes in hours, so the
answer is the LONGITUDINAL change between the water that enters and the water
that leaves - a seasonal bloom in standing water is a different question."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data, Ref, register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.outputs import (
    profile, reference_line,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.templates.eutrophication.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_eutrophication"]


#: The roughness this deck is solved at, and the law it is read under: Strickler,
#: the coefficient an unsurveyed channel is screened at. ONE number, stated once,
#: because the outflow stage is derived as a normal depth AT this roughness and a
#: stage derived at one number under a deck written at another is a level the run
#: never sits at. A user who knows the channel sets the keyword by name.
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


#: How far downstream of the seed the producer walks when this question has to
#: find its own water. It is also the LENGTH OF ONE PASS - the water grows algae
#: for as long as it takes to travel this far - so it is stated long enough for
#: the drawdown to show. A user who wants another stretch supplies the domain
#: polygon, which supersedes the producer.
_REACH_LENGTH_KM = 12.0

#: The line every longitudinal read is taken along: the LINE SLOT, which the
#: domain's own producer fills with the centerline it measured beside the
#: polygon and which a user draws on a body whose producer measured none.
_CENTERLINE = Ref("line")

#: FORMULA FOR COMPUTING CS: the oxygen saturation follows the STATED water
#: temperature rather than the engine's constant ceiling, so the water is judged
#: against the ceiling it actually has.
_SATURATION_FROM_TEMPERATURE = 1

#: WHAT THE WATER ARRIVES CARRYING, one value per tracer in the order the
#: eutrophication source term appends them: PHY (ug/L), then PO4, POR, NO3, NOR,
#: NH4, organic load and O2 (mg/L). Enrichment the observed record is read
#: AGAINST rather than filled from - `fetch_usgs_water_quality` returns sample
#: SITES, and turning scattered sites into a domain-wide field is not a step this
#: question takes on its own - so a user who knows the water sets INITIAL VALUES
#: OF TRACERS by name. The oxygen is the water at saturation: 8.667 mg/L, the
#: Elmore-Hayes freshwater relation at the stated 22 C (14.652 - 0.41022 T +
#: 0.0079910 T^2 - 0.000077774 T^3, 1 atm). The CEILING the run measures against
#: is WAQTEL's own, computed from the same temperature.
_ENTERING = [2.0, 0.05, 0.02, 1.0, 0.5, 0.05, 2.0, 8.667]
#: The three the longitudinal charts are drawn against: the water entered
#: carrying these, and the far end of the profile is what one pass made of them.
_PHYTO_IN, _PO4_IN, _NO3_IN = _ENTERING[0], _ENTERING[1], _ENTERING[3]


class DATA:
    """The domain and the bed this run stands on, and the two readings it opens
    on: the flow the inflow carries, and the temperature the stated one is read
    against."""

    # THE DOMAIN, as the CLASS it is: a polygon the caller supplies or draws
    # supersedes this; unfilled, the stretch the seed stands on is matched from
    # the mapped water and arrives with its two end transects - which is where
    # the inflow and the outflow are prescribed - and with its centerline, which
    # is the line every longitudinal read below is taken along.
    domain = Data.need("hydrography", at=Ref("seed"),
                       span_km=_REACH_LENGTH_KM)
    # THE LINE every longitudinal read is taken along.
    line = Data.supplied(geometry="polyline")

    # THE BED, as the CLASS it is rather than the source it comes from: the
    # measurement where something measured it, the terrain under the rest. Which
    # survey or which DEM reaches this domain is the match's to answer off their
    # coverage rows, and the merge between the two classes is the runtime's one
    # rule.
    bed = Data.need("bathymetry")

    # THE FLOW the inflow run prescribes, as a CLASS: which record reports a
    # discharge over this domain is the match's, and the window it reports is
    # opened at the moment the run opens at. A number stated on this row stands
    # over any record.
    discharge = Data.need("discharge series").optional()

    # WHAT THE WATER IS ACTUALLY THIS WARM AT, as the record rather than as the
    # deck's number. ONE reading off the nearest sample site, so the run journal
    # says which site took it and when the sample was a moment: the stated
    # temperature is read against this and the keyword is never filled from it.
    # This run publishes no temperature VARIABLE of its own - the water
    # temperature is a stated WAQTEL keyword, not a tracer - so this row states
    # no ``of=`` and its record is read in the unit it was measured in.
    observe = Data.need("water quality sample").context(
        "no water-quality site near this domain reports a water "
        "temperature; the stated value stands")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on: a gauge reports a height above its OWN zero, so the record
    # carries that zero's elevation and the offset row puts it on the run's
    # frame.
    level = Data.need("water level series").optional()


class STEERING(T2D):
    """The deck: water carrying nutrients, and the eight tracers the
    eutrophication process runs on them."""

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
    # under. The tracers advect under the engine's own scheme and diffusivity.
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
    # the 25 m default edge the CFL step is 1 s, and this question's
    # 172800 s window is about 172,800 of them - one frame every
    # 3,600 steps is 48 frames of the bloom. A user who wants another
    # cadence sets the keyword by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 3600
    # THE WINDOW one pass is measured over. The longitudinal answer has to have
    # settled before it is read, so the window covers several travel times: two
    # days against a pass measured in hours. A river flushes far too fast to hold
    # a seasonal bloom, which is a different question and a different window.
    DURATION = 172800.0

    #: The carrier declares NO tracer of its own - nothing is released into this
    #: water - so all eight belong to the coupled process and arrive in the order
    #: the engine appends them.
    INITIAL_VALUES_OF_TRACERS = _ENTERING

    #: The SAME water at every liquid boundary as the domain opens holding: the
    #: question is what one pass does to water of this composition, so water that
    #: arrives different from the water already in it would answer a step change.
    boundaries = Boundaries(measured=Ref("settled"), tracers=_ENTERING)

    #: Growth on the stated nutrients under the stated light, and the oxygen the
    #: growth and its decay drive. SECCHI DEPTH is unwritten, so the engine's own
    #: clarity stands, and every rate and half-saturation constant is the
    #: engine's too.
    coupling = [WAQTEL.eutrophication(
        oxygen=True,
        # Summer water, and the strongest lever on this deck: it sets the growth,
        # the mortality and the nitrification rates AND the oxygen ceiling below.
        # The engine's own is a cold-water number. The observation row says what
        # the water is actually this warm at, and where nothing was sampled near
        # the domain the sheet says so and this stands.
        WATER_TEMPERATURE=22.0,
        # W/m2 at the water surface, averaged over the window: light is what
        # drives the growth and water in the dark cannot bloom. The engine reads
        # it from this keyword and nowhere else - an atmospheric file's radiation
        # columns feed the thermal budget and never this term.
        SUNSHINE_FLUX_DENSITY_ON_WATER_SURFACE=100.0,
        FORMULA_FOR_COMPUTING_CS=_SATURATION_FROM_TEMPERATURE)]


#: What this question PLACES: the bloom and the oxygen ALONG THE WATER'S PATH,
#: which is the longitudinal change the question asks about, and the two of them
#: over time where the user is watching.
OUTPUTS = [
    profile("T1", along=_CENTERLINE).chart(
        reference=reference_line(_PHYTO_IN, label="entering")),
    profile("T2", along=_CENTERLINE).chart(
        reference=reference_line(_PO4_IN, label="entering")),
    profile("T4", along=_CENTERLINE).chart(
        reference=reference_line(_NO3_IN, label="entering")),
    profile("T8", along=_CENTERLINE).chart(
        reference=reference_line(P.do_standard_mgl, label="standard")),
    series("T1", at=P.station).chart(),
    series("T8", at=P.station).chart(),
]
CAPTIONS = {"T1": "phyto biomass", "T2": "phosphate", "T4": "nitrate",
            "T8": "dissolved o2",
            "discharge": "a streamflow", "level": "a water-surface elevation",
            "observe": "a water temperature"}


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
        "(self-labeled); no fixed coarse ceiling. The edge also sets the CFL "
        "step, and this question is watched over days rather than hours"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_eutrophication",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the water this run is carrying"


telemac_eutrophication = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    # WHERE the oxygen bottoms out and where the biomass stands highest are
    # local-feature LOCATIONS and move with the element that resolves them.
    sensitivity=(("do_min_distance_m", "location"),
                 ("phyto_max_distance_m", "location")),
    coerce=(
        point_arg("seed", tool="telemac_eutrophication",
                  prompt="Click on the channel where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_eutrophication",
                  prompt="Click where you want the water watched",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
