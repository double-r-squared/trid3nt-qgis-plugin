"""Engine template ``telemac_eutrophication`` - what one pass through
enriched water does to it.

TELEMAC-2D coupled with WAQTEL's eutrophication process over the domain the run
is given: phytoplankton growing on stated nitrate and phosphate under stated
light and temperature, and the oxygen budget that growth, its decay and the
nitrification of its ammonium drive. Moving water flushes in hours, so the
answer is the LONGITUDINAL change between the water that enters and the water
that leaves - a seasonal bloom in standing water is a different question."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data, ParamRef, Ref, register_workflow, tool,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, mesh, series
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.templates.eutrophication.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
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

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
_TERRAIN_RESOLUTION_M = 10


class DATA:
    """The domain and the bed this run stands on, and the two readings it opens
    on: the flow the inflow carries, and the temperature the stated one is read
    against."""

    # THE DOMAIN. A polygon the caller supplies or draws supersedes this;
    # unfilled, the stretch the seed stands on is cut from the mapped water and
    # arrives with its two end transects - which is where the inflow and the
    # outflow are prescribed - and with its centerline, which is the line every
    # longitudinal read below is taken along.
    # The seed is on a lake rather than in a channel where no reach cuts,
    # so the producer is a LADDER: the reach, else the waterbody the seed
    # stands in, which is a closed body and names no runs.
    domain = Data.domain(
        tool("fetch_river_reach", distance_km=_REACH_LENGTH_KM,
             seed_point=[Ref("seed.lon"), Ref("seed.lat")])
        .ladder(tool("fetch_nhd_waterbody_at_point",
                     seed_point=[Ref("seed.lon"), Ref("seed.lat")])))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    # THE LINE every longitudinal read is taken along.
    line = Data.line()

    # THE BED, as one source composed from two. ABSENT is legal on the survey:
    # water with no federal navigation project has no published sounding, and
    # the sheet says so.
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

    # The carrier flow the inflow run prescribes. A number stated on this row
    # stands over any record, so the flow is the slot's and no param twins it.
    # ONE reading, not the grid the model published: which reach segment reports
    # it is ranked against the domain's own interior point, and the step that
    # opens the channel refuses a record nobody chose from.
    carrier = Data.discharge(
        tool("fetch_noaa_nwm_streamflow", bbox=Ref("domain.bbox"),
             valid_time=ParamRef("event_time")),
        near=Ref("domain.centroid"), value_field="streamflow_cms",
        measures="a streamflow", opens="the carrier flow opens at"
    ).context("the National Water Model published no streamflow over this "
              "domain at that cycle")

    # WHAT THE WATER IS ACTUALLY THIS WARM AT, as the record rather than as the
    # deck's number. ONE reading off the nearest sample site, so the run journal
    # says which site took it and when the sample was a moment: the stated
    # temperature is read against this and the keyword is never filled from it.
    # Nothing sampled near this domain is an absence the sheet states.
    water_temperature = Data.observation(
        tool("fetch_usgs_water_quality", bbox=Ref("domain.bbox"),
             characteristic="temperature",
             purpose="observed water temperature"),
        near=Ref("domain.centroid"), units="degC",
        measures="a water temperature",
        opens="the nearest sampled water temperature is"
    ).context("no water-quality site near this domain reports a water "
              "temperature; the stated value stands")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on - a gauge publishes its height above its own zero, which is a
    # different surface, so no source is named here and the number is stated.
    stage = Data.level(near=Ref("domain.centroid"),
                       opens="the outflow holds at").optional()


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

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    #: The carrier declares NO tracer of its own - nothing is released into this
    #: water - so all eight belong to the coupled process and arrive in the order
    #: the engine appends them: PHY, PO4, POR, NO3, NOR, NH4, organic load, O2.
    INITIAL_VALUES_OF_TRACERS = [
        P.initial_phyto_ug_l, P.initial_po4_mgl, P.initial_por_mgl,
        P.initial_no3_mgl, P.initial_nor_mgl, P.initial_nh4_mgl,
        P.initial_organic_load_mgl, P.initial_do_mgl]

    #: The SAME water at every liquid boundary as the domain opens holding: the
    #: question is what one pass does to water of this composition, so water that
    #: arrives different from the water already in it would answer a step change.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("settled.inflow_q_m3s"),
                  "outflow_stage_m": Ref("settled.outflow_stage_m")},
        tracers=[P.initial_phyto_ug_l, P.initial_po4_mgl, P.initial_por_mgl,
                 P.initial_no3_mgl, P.initial_nor_mgl, P.initial_nh4_mgl,
                 P.initial_organic_load_mgl, P.initial_do_mgl])

    #: Growth on the stated nutrients under the stated light, and the oxygen the
    #: growth and its decay drive. The light is stated because the engine reads
    #: none from a forcing file - the atmospheric file's radiation columns feed
    #: the thermal budget and nothing else - and water in the dark cannot bloom;
    #: the temperature because the engine's own is a cold-water number.
    #: Every rate and half-saturation constant is the engine's own.
    coupling = [WAQTEL.eutrophication(
        oxygen=True,
        WATER_TEMPERATURE=P.water_temp_c,
        SUNSHINE_FLUX_DENSITY_ON_WATER_SURFACE=P.sunshine_w_m2,
        SECCHI_DEPTH=P.secchi_depth_m,
        FORMULA_FOR_COMPUTING_CS=_SATURATION_FROM_TEMPERATURE)]


#: What this question PLACES: the bloom and the oxygen ALONG THE WATER'S PATH,
#: which is the longitudinal change the question asks about, and the two of them
#: over time where the user is watching.
OUTPUTS = [
    profile("T1", along=_CENTERLINE).chart(),
    profile("T8", along=_CENTERLINE).chart(),
    series("T1", at=P.station).chart(),
    series("T8", at=P.station).chart(),
]
CAPTIONS = {"T1": "phyto biomass", "T8": "dissolved o2"}

#: The run's ANSWER: what one pass did to the water, as the numbers a reader has
#: to be able to check. Every ratio is held to the stated concentration the water
#: ENTERED at - the boundaries hold that value flat, so it is the inlet, and the
#: profile's other end is what the pass made of it.
ANSWER = {
    "phyto_max_ug_l": profile("T1", along=_CENTERLINE).measure("max"),
    "phyto_max_distance_m": profile("T1", along=_CENTERLINE).measure("x_max_m"),
    "phyto_growth_ratio": profile("T1", along=_CENTERLINE).measure("max")
                          .over(P.initial_phyto_ug_l),
    "no3_remaining_ratio": profile("T4", along=_CENTERLINE).measure("min")
                           .over(P.initial_no3_mgl),
    "po4_remaining_ratio": profile("T2", along=_CENTERLINE).measure("min")
                           .over(P.initial_po4_mgl),
    "do_min_mgl": profile("T8", along=_CENTERLINE).measure("min"),
    "do_min_distance_m": profile("T8", along=_CENTERLINE).measure("x_min_m"),
    "do_below_standard": profile("T8", along=_CENTERLINE).measure("min")
                         .below(P.do_standard_mgl),
    "pass_velocity_mps": profile("T8", along=_CENTERLINE)
                         .measure("velocity_mps"),
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
        "(self-labeled); no fixed coarse ceiling. The edge also sets the CFL "
        "step, and this question is watched over days rather than hours"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_eutrophication",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)

telemac_eutrophication = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        compute_class=ParamRef("compute_class"),
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the water this run is carrying"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
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
        compute_class(),
    ),
    doc=DOC,
)
