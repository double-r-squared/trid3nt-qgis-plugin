"""Engine template ``telemac_do_sag`` - the dissolved-oxygen sag below a discharge.

TELEMAC-2D coupled with WAQTEL O2 over the channel this run solves on - a reach
walked downstream from the outfall, or a polygon the user supplies: where DO
bottoms out below a continuous discharge, against the standard the water is held
to and the closed form the process reduces to."""

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
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, mesh
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Sources
from trid3nt_server.workflows.telemac.templates.do_sag import streeter_phelps
from trid3nt_server.workflows.telemac.templates.do_sag.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Placed, TelemacWorkflow,
)

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_do_sag"]

#: The file the run directory holds this run's picture under. The deck's own
#: RESULTS statement names it, so nothing here restates it.
_RESULT = "r2d_domain.slf"

#: How far the reach producer walks downstream from the outfall when this
#: question has to find its own domain. A sag critical point is often several km
#: below the discharge, so the stretch has to outrun it; a user who wants another
#: one supplies the domain polygon, which supersedes the producer.
_REACH_LENGTH_KM = 12.0

#: How far down the domain's own centerline the outfall sits when no point was
#: placed. It is what holds the source node off the inflow face rather than on
#: it, where it would compete with the boundary condition for the same node.
_OUTFALL_FRAC = 0.02

#: WHERE the outfall enters the water: the point the user clicked, else that
#: fraction along the domain's own centerline. The workflow settles it onto a
#: node of the accepted mesh before the sheet reads it back.
_OUTFALL = Placed("outfall", point=PARAMS.outfall_coords, fraction=_OUTFALL_FRAC,
                  label="Outfall")

#: The roughness this deck is solved at, and the law it is read under: Strickler,
#: the coefficient an unsurveyed channel is screened at. ONE number, stated once,
#: because the outflow stage is derived as a normal depth AT this roughness and a
#: stage derived at one number under a deck written at another is a level the run
#: never sits at. A user who knows the channel sets the keyword by name.
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

#: THE KINETICS the sag develops under, and the saturation the deficit is
#: measured against. ONE set of numbers, because the closed form drawn beside
#: the solved profile grades nothing unless it is computed at the rates the run
#: was solved at. The deoxygenation rate a documented shallow stream carries, a
#: reaeration rate three times it, and the freshwater saturation the
#: Elmore-Hayes relation gives at 20 C - the standard Streeter-Phelps condition
#: this question is asked under. WAQTEL is a COUPLED body and the keywords floor
#: reaches only the carrier's own dictionary, so these are the deck's fixed
#: opinion until a coupled module's keywords have a surface of their own.
_WATER_TEMPERATURE_C = 20.0
_K1_PER_DAY = 0.3
_K2_PER_DAY = 0.9
_SATURATION_MGL = 9.022

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    # The outfall names which stretch to model: the reach is walked DOWNSTREAM
    # from it, so the sag develops inside the domain rather than past its end. A
    # domain the user supplies supersedes this.
    # The seed is on a lake rather than in a channel where no reach cuts,
    # so the producer is a LADDER: the reach, else the waterbody the seed
    # stands in, which is a closed body and names no runs.
    domain = Data.domain(
        tool("fetch_river_reach", distance_km=_REACH_LENGTH_KM,
             seed_point=[Ref("outfall_coords.lon"), Ref("outfall_coords.lat")])
        .ladder(tool("fetch_nhd_waterbody_at_point",
                     seed_point=[Ref("outfall_coords.lon"), Ref("outfall_coords.lat")])))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    # THE LINE the oxygen is read down. The reach producer measured a centerline
    # and it fills this; a lake is asked for the line the question is about.
    line = Data.line()
    # THE MEASUREMENT. A domain with no federal navigation project has no
    # published survey, and the sheet says so rather than refusing.
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
    # ONE reading off whatever reports nearest the water, because the dilution
    # the whole sag rests on is a number and not a layer.
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
    """The deck: a channel, an OUTFALL, and the four tracers the O2 process runs."""

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

    # The advection of momentum and depth, and the SUPG the channel is stable
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

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 14 m default edge the CFL step is 0.7 s, and the window stated
    # below is about 246,900 of them - one frame every 5,000 steps is 49
    # frames of the sag. A user who wants another cadence sets the keyword
    # by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 5000

    # TWO DAYS. A sag is a STEADY-STATE answer, so the window has to cover
    # several travel times through the reach and stand against 1/k1; a
    # shorter one reports a sag that has not developed yet.
    DURATION = 172800.0

    # The carrier declares ONE tracer; WAQTEL's O2 process appends DISSOLVED O2,
    # ORGANIC LOAD and NH4 LOAD behind it, which is why every array sized to the
    # tracer count below carries four values. The reach OPENS clean: no organic
    # load, and oxygen at saturation, so the deficit the answer reads is the
    # outfall's own and not a state the run was started in.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0, _SATURATION_MGL, 0.0, 0.0]

    #: CLEAN WATER at every liquid boundary: no organic load, its own oxygen. The
    #: load enters at the source, so which boundary the engine numbers first
    #: cannot decide the answer. The walk is the mesh's own; the flow the inflow
    #: carries and the level the outflow holds are the open channel's.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0, _SATURATION_MGL, 0.0, 0.0])

    #: WHERE the outfall enters the water, in the mesh's own metres: the settled
    #: discharge point, read by position because a point is one value with an
    #: order. One element per source, in the engine's own positional order.
    ABSCISSAE_OF_SOURCES = [Ref("outfall.at.0")]
    ORDINATES_OF_SOURCES = [Ref("outfall.at.1")]
    #: HOW MUCH the outfall discharges, and at what concentration: the
    #: discharge, then every tracer of the one source in the order NAMES OF
    #: TRACERS declares them - DYE, then the three WAQTEL O2 tracers behind it.
    #: A secondary-treated municipal effluent arrives oxygen-poor and heavily
    #: loaded, which is the initial deficit the sag starts from; it is what the
    #: deck is read against, not a number this question asks for.
    WATER_DISCHARGE_OF_SOURCES = [1.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [0.0, 2.0, 250.0, 0.0]
    #: A PERMITTED discharge does not pulse: held flat across the whole run so
    #: the water reaches the steady-state sag the question is asked about.
    sources = Sources(at=_OUTFALL, window_s=None, until_s=Ref("settled.until_s"))

    #: Deoxygenation balanced by surface reaeration, and nothing else: the
    #: modelled curve is the closed form the question is asked against, so this
    #: run states FRESH water, a CONSTANT reaeration rate (formula 0, the one the
    #: closed form holds under) and zeroes the three sources the closed form has
    #: no term for - nitrification, benthic demand, and photosynthesis less
    #: respiration.
    coupling = [WAQTEL.o2(
        WATER_TEMPERATURE=_WATER_TEMPERATURE_C, WATER_SALINITY=0.0,
        CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1=_K1_PER_DAY,
        CONSTANT_OF_NITRIFICATION_KINETIC_K4=0.0,
        FORMULA_FOR_COMPUTING_K2=0, K2_REAERATION_COEFFICIENT=_K2_PER_DAY,
        O2_SATURATION_DENSITY_OF_WATER__CS_=_SATURATION_MGL,
        BENTHIC_DEMAND=0.0, PHOTOSYNTHESIS_P=0.0, VEGETAL_RESPIRATION_R=0.0)]


#: What this question PLACES: the oxygen down the channel as the chart, with the
#: closed form and the standard drawn beside it. The line is the LINE SLOT's:
#: the centerline the domain's producer measured, or the one a user draws when
#: the body it is asked of has none.
OUTPUTS = [profile("T2", along=Ref("line")).chart(
    reference=streeter_phelps.overlay(saturation_mgl=_SATURATION_MGL,
                                      k1_per_day=_K1_PER_DAY,
                                      k2_per_day=_K2_PER_DAY))]
CAPTIONS = {"T2": "dissolved oxygen"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above: how low the oxygen bottoms out and where,
#: whether that is below the standard the water is held to, the mixed load that
#: drove it, and the speed it travelled at.
ANSWER = {
    "do_min_mgl": profile("T2", along=Ref("line")).measure("min"),
    "do_below_standard": profile("T2", along=Ref("line"))
                         .measure("min").below(P.do_standard_mgl),
    "do_min_distance_m": profile("T2", along=Ref("line"))
                         .measure("x_min_m"),
    "bod_mixed_mgl": profile("T3", along=Ref("line")).measure("max"),
    "mean_velocity_mps": profile("T2", along=Ref("line"))
                         .measure("velocity_mps"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long domain
#: WITHIN this declaration, and the effective edge stays >= 2 cells across it.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="the domain's own geometry + the bed it is painted from",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long domain is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the outfall and the water it discharges to"


telemac_do_sag = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # WHERE the sag sits is a local-feature LOCATION and moves with the element
    # that resolves it. The DO minimum itself is a saturated maximum - a
    # converged class - so it carries no label.
    sensitivity=(("do_min_distance_m", "location"),),
    coerce=(
        point_arg("outfall_coords", tool="telemac_do_sag",
                  prompt="Click on the water where the outfall discharges",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
)
