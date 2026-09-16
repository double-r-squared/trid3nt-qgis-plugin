"""Engine template ``telemac_micropollutant_release`` - a sorbing substance in water.

TELEMAC-2D coupled with WAQTEL micropol over the domain this run solves on - a
river reach walked from the release point, a harbour basin the user draws, a
polygon they own: how much of a released substance travels dissolved, how much
rides the suspended sediment, and how much of it is left on the bed."""

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
    T2D,
    WAQTEL,
    field,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Release
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.micropollutant_release.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_micropollutant_release"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

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

#: The substance's own tracer, declared by the carrier so that the engine ADOPTS
#: it for the micropol process rather than adding a sixth: ADDTRACER matches on
#: the first sixteen characters, which are the process's own name for it. The
#: four the process appends behind it are the suspended sediment, the bed
#: sediment, and the substance sorbed onto each.
_DISSOLVED = "MICRO POLLUTANT MG/L"

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10


class DATA:
    """The three slots this run stands on, and the flow that fills its inflow."""

    #: THE DOMAIN. A release named up front also names which stretch to model:
    #: the reach is walked downstream from it. A polygon the user supplies or
    #: draws supersedes this, and states its own edges or none.
    #: The seed is on a lake rather than in a channel where no reach cuts,
    #: so the producer is a LADDER: the reach, else the waterbody the seed
    #: stands in, which is a closed body and names no runs.
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
    #: THE MEASUREMENT. Water with no federal navigation project has no published
    #: sounding, and the sheet says so rather than refusing.
    survey = Data(tool("fetch_ehydro_surveys", bbox=Ref("domain.bbox"),
                       purpose="channel survey soundings")
                  ).context("no published channel survey over this domain; "
                            "the terrain surface stands")
    # A terrain surface measures the water TOP, so where no survey reaches it
    # the modelled water is shallower than the real water.
    terrain = Data(tool("fetch_dem", bbox=Ref("domain.bbox"), source="3dep",
                        resolution_m=_TERRAIN_RESOLUTION_M,
                        purpose="bed elevation"))
    # ONE bed: the survey where it measured, the terrain everywhere else. The
    # soundings are DEPTHS below the survey's own project datum and the slot
    # reads them as elevations on the frame that survey publishes itself
    # against; with the survey absent the terrain is the whole bed.
    bed = Data.bed(tool("derive_survey_surface", points=survey,
                        value_field="depth_below_datum_m",
                        resolution_m=ParamRef("mesh_resolution_m")),
                   over=terrain)
    #: The carrier flow the inflow run prescribes - the water that dilutes and
    #: transports the release. A number stated on this row stands over any
    #: record, so the flow is the slot's. ONE reading, not the
    #: grid the model published: which reach segment reports it is ranked against
    #: the domain's own interior point, and the step that opens the channel
    #: refuses a record nobody chose from.
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
    """The deck: water carrying suspended sediment, and a substance released into
    it that partitions onto that sediment and settles with it."""

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

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    # The carrier declares the DISSOLVED substance; the micropol process adopts
    # it and appends the suspended sediment, the bed sediment and the two sorbed
    # phases behind it, which is why every array sized to the tracer count below
    # carries five values in that order.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = [_DISSOLVED]
    # The water arrives carrying its sediment and nothing else: the bed starts
    # clean, and everything on it at the end got there during the run.
    INITIAL_VALUES_OF_TRACERS = [0.0, P.ambient_spm_kg_m3, 0.0, 0.0, 0.0]

    #: The carrier's own boundary values, in the order the engine numbers its
    #: liquid boundaries: the walk the mesh measured, the flow the inflow run
    #: carries and the level the outflow run holds. The same clean water, with
    #: the same suspended sediment, at every one of them.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("settled.inflow_q_m3s"),
                  "outflow_stage_m": Ref("settled.outflow_stage_m")},
        tracers=[0.0, P.ambient_spm_kg_m3, 0.0, 0.0, 0.0])

    #: A FINITE release at a point source inside the domain, so what happens to
    #: the substance after it is in the water is what the rest of the run shows.
    #: It enters DISSOLVED and carries no sediment of its own.
    releases = [Release(at=Ref("source.at"), q=P.source_q_m3s,
                        tracers=[P.source_concentration_mgl, 0.0, 0.0, 0.0, 0.0],
                        window_s=P.release_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The partition, as WAQTEL's own keywords: how fast the sediment settles,
    #: how hard the substance holds onto it, how fast it comes back off, and how
    #: fast it decays. Each is the engine's own where the ask states none.
    coupling = [WAQTEL.micropollutant(
        SEDIMENT_SETTLING_VELOCITY=P.settling_velocity_mps,
        COEFFICIENT_OF_DISTRIBUTION=P.distribution_coefficient_m3kg,
        CONSTANT_OF_DESORPTION_KINETIC=P.desorption_constant_per_s,
        EXPONENTIAL_DESINTEGRATION_CONSTANT=P.decay_constant_per_s)]


#: What this question PLACES: the dissolved history where the user asks for it.
#: The settled point is read as the WHOLE step, whose lon/lat is where the node
#: is looked up.
OUTPUTS = [
    series("T1", at=Ref("monitoring")).chart(),
]
CAPTIONS = {"T1": "dissolved micropollutant"}

#: The run's ANSWER, as the numbers a reader has to be able to check: how
#: concentrated the dissolved substance got AT THE MONITORING POINT and when,
#: how far the dissolved body of water travelled, and where the substance stands
#: at the last instant - dissolved, on the suspended sediment, and on the bed.
#: The bed phase is what SETTLED onto a square metre and the other two are
#: concentrations in the water, so the question's own comparison is over the two
#: that share a class: how much rides the sediment for every unit still
#: dissolved, which is the partition this question is about.
ANSWER = {
    "dissolved_cmax_mgl": series("T1", at=Ref("monitoring")).measure("max"),
    "dissolved_peak_time_s": series("T1", at=Ref("monitoring")).measure("t_max"),
    "dissolved_travel_m": field("T1", t="every").measure("travel_m"),
    "dissolved_final_mean_mgl": field("T1").measure("mean"),
    "suspended_sorbed_final_mean_mgl": field("T4").measure("mean"),
    "bed_sorbed_final_mean_g_m2": field("T5").measure("mean"),
    "sorbed_over_dissolved": field("T4").measure("mean")
                             .over(field("T1").measure("mean")),
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
                "edge sized from the domain's width",
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
    name="telemac_micropollutant_release",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_micropollutant_release = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        produce=(
            # WHERE the substance enters the water, settled against the accepted
            # mesh before the sheet reads it. The domain rides along because an
            # unplaced release sits its fraction along that domain's centerline
            # companion, and a supplied point is snapped onto the same line.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.release, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": P.release_fraction,
                         "label": "Release point"}).named("source"),
            # WHERE the dissolved history is read, settled the same way, so the
            # chart is anchored on a node the run actually solved on.
            Step(runner=f"{_AUTHORING}.assembler.settle_release", stage="author",
                 kwargs={"point": P.monitoring_point, "mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "fraction": P.monitoring_fraction,
                         "label": "Monitoring point"}).named("monitoring"),),
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the release and the sediment it partitions onto"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # A concentration peak lives inside one element and is measured LOW on a
    # coarse mesh. How far the dissolved body of water reached is a front
    # location and moves with it.
    sensitivity=(("dissolved_cmax_mgl", "peak"),
                 ("dissolved_travel_m", "location")),
    coerce=(
        point_arg("release", tool="telemac_micropollutant_release",
                  prompt="Click on the water where the substance enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("monitoring_point", tool="telemac_micropollutant_release",
                  prompt="Click where downstream to read the dissolved history",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
    doc=DOC,
)
