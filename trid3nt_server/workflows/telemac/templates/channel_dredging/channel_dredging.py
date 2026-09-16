"""Engine template ``telemac_channel_dredging`` - a maintenance dredge of a channel.

TELEMAC-2D coupled with GAIA, the dredger driven by NESTOR on the sediment deck:
how much material comes out of the fairway to hold it at grade, where the spoil
goes, and what the bed does around both."""

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
    mesh,
)
from trid3nt_server.workflows.telemac.modules.gaia import Dig, Dredging, RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, TimeOrigin
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.channel_dredging.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "telemac_channel_dredging"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The names the run directory holds this run's files under - the deck's own
#: GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, which the workflow reads
#: back off the deck rather than being told them twice.
_GEOMETRY = "channel.slf"
_BOUNDARY = "channel.cli"
_RESULT = "r2d_channel.slf"

#: How much channel the reach producer walks from the seed when this question has
#: to find its own domain. A port that keeps another stretch at grade supplies the
#: fairway polygon, which supersedes the producer.
_REACH_LENGTH_KM = 2.0

#: The roughness this deck is solved at, and the law it is read under. ONE number,
#: stated once, because the outflow stage is derived as a normal depth AT this
#: roughness and a stage derived at one number under a deck written at another is
#: a level the run never sits at - and it is the level every dredged depth is
#: measured down from. A user who knows the channel sets the keyword by name.
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

#: How far back a published channel survey still describes the fairway the dredger
#: works in. The newest survey is not necessarily the one over this stretch, so the
#: window is read and what comes back is unioned.
_SURVEY_SINCE = "2015-01-01"

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and a
#: pinned source surfaces its own typed error instead of switching dataset.
_TERRAIN_RESOLUTION_M = 10

#: Which reference surface every action reads its levels from: the profile file
#: this run authors, which carries the water surface the channel opens at. The
#: alternatives the engine offers - a ZRL variable on the geometry, a level a
#: Save_water_level action captured - are not what this question measures a
#: design depth against.
_REFERENCE_LEVEL = "SECTIONS"


class DATA:
    """The slots this run stands on - the water, its bed, the flow through it -
    and the two areas the dredge works on, which this template names no source for."""

    #: THE CHANNEL. A seed on the water names a stretch of river and the fetcher
    #: returns the polygon with the two end transects it was cut between and the
    #: centerline beside it; a fairway the port supplies supersedes it.
    domain = Data.domain(tool("fetch_river_reach",
                              seed_point=[Ref("seed_point.lon"),
                                          Ref("seed_point.lat")],
                              distance_km=_REACH_LENGTH_KM))
    # THE STRETCHES OF ITS EDGE the water crosses. The reach producer measured
    # them where it cut the section, and they ride on the domain it returned; a
    # domain that arrives with none - a drawn outline, a lake - is asked for
    # them on the canvas, and an edge that names none is a closed body's.
    runs = Data.runs()
    #: THE MEASUREMENT, and the whole reason a dredged volume is worth reading: a
    #: surface DEM measures the water top, so a fairway painted from one is
    #: centimetres deep and the cut to grade is summed over nothing. A channel with
    #: no federal navigation project has no published survey, and the sheet says so
    #: rather than refusing.
    survey = Data(tool("fetch_ehydro_surveys", bbox=Ref("domain.bbox"),
                       since=_SURVEY_SINCE,
                       purpose="channel survey soundings")
                  ).context("no published channel survey over this domain; "
                            "the terrain surface stands")
    #: The soundings are points; the surface between them is a derive, and it is
    #: context for the same reason its input is.
    surveyed_bed = Data(tool("derive_survey_surface", points=survey,
                             value_field="depth_below_datum_m",
                             resolution_m=ParamRef("mesh_resolution_m"))
                        ).context("the survey held no soundings to grid; "
                                  "the terrain surface stands")
    terrain = Data(tool("fetch_dem", bbox=Ref("domain.bbox"), source="3dep",
                        resolution_m=_TERRAIN_RESOLUTION_M,
                        purpose="channel bed elevation"))
    #: ONE bed: the survey where it measured, the terrain everywhere else. With
    #: the survey absent the terrain passes through unchanged, and the merge
    #: refuses two vertical datums by name rather than writing a step into the
    #: bed a dredged volume is then summed over.
    bed = Data.bed(tool("derive_merge_rasters", primary=surveyed_bed,
                        fallback=terrain))
    #: The flow that shoals the fairway and carries what the dredger disturbs:
    #: ONE reading off the nearest reporting site, or the number stated on this
    #: row, which stands over any record. What the open channel opened on is
    #: said once, on its own journal note.
    carrier = Data.observation(
        tool("fetch_noaa_nwm_streamflow", bbox=Ref("domain.bbox"),
             valid_time=ParamRef("event_time")),
        near=Ref("domain.centroid"), value_field="streamflow_cms",
        measures="a streamflow", opens="the carrier flow opens at"
    ).context("the National Water Model published no streamflow over this "
              "domain at that cycle")
    #: WHERE the dredger works, and where the spoil goes. Two SLOTS: the channel
    #: a port keeps at grade and the disposal ground it is licensed to use are
    #: both administrative areas, and no dataset knows either - they are drawn,
    #: or handed over as the port's own layers.
    dredge_area = Data.supplied(geometry="polygon")
    dump_area = Data.supplied(geometry="polygon")
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
    """The deck: a channel over a mobile bed, with a dredger working in it."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    # The step the channel is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for.
    TIME_STEP = Ref("settled.time_step_s")
    LISTING_PRINTOUT_PERIOD = 500

    # The run OPENS at the derived normal depth, laid bed-parallel. Not a
    # constant elevation at the outflow stage: the stage is derived only where
    # the channel FALLS, so a horizontal surface at the outlet's level leaves
    # every node upstream of it dry - the flowrate face among them - and the
    # engine refuses a discharge it has no water to impose. It is also the
    # surface the dredge's own reference profiles are laid at.
    INITIAL_CONDITIONS = "CONSTANT DEPTH"
    INITIAL_DEPTH = Ref("channel.depth_m")

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    # The advection of momentum and depth, and the SUPG the channel is stable
    # under.
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
    # boundary; the sediment side of the same closure is GAIA's, and the dredge's
    # own volumes are printed into the same listing.
    MASS_BALANCE = True

    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    #: The clock every dredging action is dated against. NESTOR reads absolute
    #: dates and differences them against THIS origin, so the deck states it
    #: rather than inheriting the dictionary's own.
    time_origin = TimeOrigin(at=P.time_origin)

    #: No tracer: a dredge is a question about the bed, so every liquid boundary
    #: carries the measured flowrate and stage and nothing else. The walk is the
    #: mesh's own; the two values are the open channel's.
    boundaries = Boundaries(
        measured={"liquid_boundary_order": Ref("settled.liquid_boundary_order"),
                  "liquid_boundary_prescribes":
                      Ref("settled.liquid_boundary_prescribes"),
                  "inflow_q_m3s": Ref("channel.inflow_q_m3s"),
                  "outflow_stage_m": Ref("channel.outflow_stage_m")},
        tracers=[])

    #: The bed the dredger cuts into, and the dredger itself. One class, bedload
    #: on, a real stock: the material the criterion dig takes out of the fairway
    #: and the rate dump lays into the spoil ground both go through GAIA's own
    #: per-class mass evolution, which is what its bed evolution and its sediment
    #: balance are computed from.
    coupling = [GAIA.bed(
        geometry=_GEOMETRY, boundary=_BOUNDARY, mass_balance=True,
        gradation=None, presets={}, d50_um=P.grain_size_um,
        thickness_m=P.bed_thickness_m, formula=P.bedload_formula,
        hiding_factor_formula=1, morphological_factor=P.morphological_factor,
        dredging=Dredging(
            actions=[Dig(field=Ref("dredge.dredge_area"),
                         level=_REFERENCE_LEVEL,
                         start=P.dredge_start_s, end=P.dredge_end_s,
                         repeat=P.dredge_repeat_s,
                         rate=P.dig_rate_m_per_s,
                         depth=P.design_depth_m,
                         crit_depth=P.trigger_depth_m,
                         min_volume=P.min_volume_m3,
                         # The radius a minimum volume is gathered over is the
                         # mesh's own measured edge: it is the length below which
                         # this run resolves nothing anyway.
                         min_volume_radius=Ref("settled.mesh_size_m"),
                         dump=Ref("dredge.dump_area"),
                         dump_rate=P.dump_rate_m_per_s)],
            reference=Ref("dredge.profiles"),
            origin=P.time_origin))]


#: The run's ANSWER. The two volumes are the engine's OWN report lines, summed
#: over the maintenance passes it printed; the two bed changes are the evolution
#: field read inside each area, so the cut is the dredged area's minimum and the
#: heap is the spoil ground's maximum. A pass that does not finish inside the
#: run's clock prints no volume at all, so the report states why the two volumes
#: read as they do rather than leaving a reader with an unexplained blank.
ANSWER = {
    "dug_volume_m3": mass_balance(module="gaia").measure("dug_volume_m3"),
    "dumped_volume_m3": mass_balance(module="gaia").measure("dumped_volume_m3"),
    "dredge_report": mass_balance(module="gaia").measure("dredge_report"),
    "dredged_bed_change_m": field("E", t=-1, module="gaia",
                                  over=DATA.dredge_area).measure("min"),
    "dumped_bed_change_m": field("E", t=-1, module="gaia",
                                 over=DATA.dump_area).measure("max"),
    "net_bed_mass_kg": mass_balance(module="gaia").measure("sediment_net_bed_mass_kg"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="USACE eHydro channel soundings over 3DEP terrain; edge sized "
                "from the domain's own geometry",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long channel is further coarsened under the mesh node budget "
        "(self-labeled); the dredged volume is summed over the nodes inside the "
        "field, so a fairway a few cells wide reads it coarsely"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_channel_dredging",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_channel_dredging = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        produce=(
            # The open-channel hydraulics this question needs on top of the
            # domain: the flow the inflow prescribes, the level the outflow
            # holds, and the depth the run opens at, all measured over the
            # accepted mesh at the roughness the deck is written at.
            Step(runner=f"{_AUTHORING}.assembler.settle_open_channel",
                 stage="author",
                 kwargs={"mesh": Ref("mesh"), "carrier": Ref("carrier"),
                         "stage": Ref("stage"),
                         "friction_law": _FRICTION_LAW,
                         "friction_coefficient": _FRICTION_COEFFICIENT}
                 ).named("channel"),
            # The two areas and the reference surface, measured against the
            # accepted mesh: the surface every design depth is read from is the
            # bed the mesh carries plus the depth the channel opens at, laid out
            # as cross-sections along the domain's own centerline, stationed
            # downstream from the end its inflow run names.
            Step(runner=f"{_AUTHORING}.assembler.settle_dredge",
                 stage="author",
                 kwargs={"mesh": Ref("mesh"),
                         "domain": Ref("domain"),
                         "settled": Ref("channel"),
                         "areas": {"dredge_area": DATA.dredge_area,
                                   "dump_area": DATA.dump_area}}
                 ).named("dredge")),
        results=(_RESULT, RESULT_FILENAME),
        compute_class=ParamRef("compute_class"),
        answer=ANSWER,
        review_title="Review the dredge, the bed and the mesh"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The dredged volume is a sum over the nodes inside the field, so a coarse
    # mesh resolves a narrow fairway - and the volume it holds - badly.
    sensitivity=(("dug_volume_m3", "peak"),
                 ("dredged_bed_change_m", "peak")),
    coerce=(
        point_arg("seed_point", tool="telemac_channel_dredging",
                  prompt="Click on the channel this dredge works in",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
        compute_class(),
    ),
    doc=DOC,
)
