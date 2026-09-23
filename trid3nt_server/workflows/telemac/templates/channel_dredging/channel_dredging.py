"""Engine template ``telemac_channel_dredging`` - a maintenance dredge of a channel.

TELEMAC-2D coupled with GAIA, the dredger driven by NESTOR on the sediment deck:
how much material comes out of the fairway to hold it at grade, where the spoil
goes, and what the bed does around both."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import (
    GAIA,
    T2D,
    )
from trid3nt_server.workflows.telemac.modules.gaia import Dig, Dredging, RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, TimeOrigin
from trid3nt_server.workflows.telemac.templates.channel_dredging.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Measured, TelemacWorkflow,
)

__all__ = ["CAPTIONS", "DATA", "PARAMS", "STEERING",
           "telemac_channel_dredging"]


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

#: The erodible sediment stock under the fairway, in metres. ONE number, stated
#: once: the deck lays it into the bed as the layer the dredger cuts from, and
#: the authoring step measures the cut the grade asks for against the same stock
#: and refuses by name before the run dispatches.
_BED_STOCK_M = 5.0

#: The calendar instant this run's clock starts at. NESTOR dates every action
#: absolutely and differences it against this origin, so the deck states it and
#: the dredge is written against the same six numbers.
_TIME_ORIGIN = [2000, 1, 1, 0, 0, 0]

#: The terrain cell the banks are painted from. 3DEP is PINNED, not preferred: a
#: DSM (Copernicus GLO-30 carries canopy) puts the bank on the tree tops, and its
#: EGM2008 zero is not the NAVD88 the surveys and the gauges are measured on.
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

    #: THE CHANNEL. A seed on the water names a stretch of river and the match
    #: ranks the reach producer and the waterbody producer over it in turn; a
    #: fairway the port supplies supersedes it.
    domain = Data.need("hydrography", at=Ref("seed_point"),
                       span_km=_REACH_LENGTH_KM)
    #: THE LINE the dredge's reference profiles are stationed along: the
    #: centerline the reach producer measured, or the one the port draws over a
    #: fairway nobody mapped a channel through.
    line = Data.supplied(geometry="polyline")
    #: THE MEASUREMENT, and the whole reason a dredged volume is worth reading:
    #: a surface DEM measures the water top, so a fairway painted from one is
    #: centimetres deep and the cut to grade is summed over nothing. The
    #: STRICTER class - the maintained prism, not the water around it - is what
    #: this question asks; a channel with no federal navigation project has no
    #: published survey, and the terrain stands in for it.
    bed = Data.need("channel survey")
    #: The flow that shoals the fairway and carries what the dredger disturbs:
    #: ONE reading off the nearest reporting site, or the number stated on this
    #: row, which stands over any record. What the open channel opened on is
    #: said once, on its own journal note.
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this domain "
        "at that cycle")
    #: WHERE the dredger works, and where the spoil goes. Two SLOTS: the channel
    #: a port keeps at grade and the disposal ground it is licensed to use are
    #: both administrative areas, and no dataset knows either - they are drawn,
    #: or handed over as the port's own layers.
    dredge_area = Data.supplied(geometry="polygon")
    dump_area = Data.supplied(geometry="polygon")
    # THE LEVEL the water stands at, which the run opens flat at and the outflow
    # holds. A reach whose measured ends do not FALL has no uniform-flow depth to
    # derive, and a closed body never had one. An ELEVATION on the datum the bed
    # is painted on - a gauge publishes its height above its own zero, which is a
    # different surface, so no source is named here and the number is stated.
    level = Data.need("water level series").optional()


#: The two areas and the reference surface, measured against the SETTLED run:
#: the surface every design depth is read from is the water surface the run
#: opens at, laid out as cross-sections along the line the domain producer
#: measured, stationed downstream from the end its inflow names. The cut the
#: grade asks for is measured against the stock before the run dispatches: the
#: engine only refuses a dredger with nothing left to cut part-way through its
#: first pass.
_DREDGE = Measured(
    "dredge", kind="dredge",
    asked={"areas": {"dredge_area": DATA.dredge_area,
                     "dump_area": DATA.dump_area},
           "dug_area": "dredge_area",
           "grade_depth_m": ParamRef("design_depth_m"),
           "stock_m": _BED_STOCK_M})


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

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at the
    # 14 m default edge the CFL step is 0.7 s, and the window stated below is
    # about 5,140 of them - one frame every 100 steps is 51 frames of the
    # campaign. A user who wants another cadence sets the keyword by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 100

    # THE HYDRAULIC WINDOW, in seconds. The campaign is read on the BED's own
    # clock - this duration times the morphological factor below - so an hour of
    # hydraulics is ten hours of bed, which is a readable maintenance interval.
    DURATION = 3600.0

    #: The clock every dredging action is dated against. NESTOR reads absolute
    #: dates and differences them against THIS origin, so the deck states it
    #: rather than inheriting the dictionary's own.
    time_origin = TimeOrigin(at=_TIME_ORIGIN)

    #: No tracer: a dredge is a question about the bed, so every liquid boundary
    #: carries the measured flowrate and stage and nothing else. The walk is the
    #: mesh's own; the two values are the open channel's.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[])

    #: The bed the dredger cuts into, and the dredger itself. One class, bedload
    #: on, a real stock: the material the criterion dig takes out of the fairway
    #: and the rate dump lays into the spoil ground both go through GAIA's own
    #: per-class mass evolution, which is what its bed evolution and its sediment
    #: balance are computed from.
    coupling = [GAIA.bed(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        # GAIA's own sediment closure, beside the water volume the carrier
        # accounts for: what the dredger moves is printed in it.
        MASS_BALANCE=True,
        # ONE class, 200 um medium sand in the keyword's own metres: the size a
        # maintained fairway shoals with, and the size the bedload formula below
        # is calibrated over.
        CLASSES_SEDIMENT_DIAMETERS=[2.0e-4],
        # The erodible stock, deeper than any cut this question makes, so the
        # dredged volume is never limited by the material under the fairway.
        LAYERS_INITIAL_THICKNESS=[_BED_STOCK_M],
        # What makes a short hydraulic window produce a readable bed change.
        MORPHOLOGICAL_FACTOR=10.0,
        dredging=Dredging(
            measured=_DREDGE,
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
            origin=_TIME_ORIGIN))]


#: The two measured rows' nouns, read on the run journal.
CAPTIONS = {"discharge": "a streamflow", "level": "a water level"}


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
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The two areas and the reference surface, measured against the SETTLED
#: run: the surface every design depth is read from is the water surface
#: the run opens at, laid out as cross-sections along the line the domain
#: producer measured, stationed downstream from the end its inflow names.
RESULTS = (_RESULT, RESULT_FILENAME)

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the dredge, the bed and the mesh"


telemac_channel_dredging = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
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
    ),
)
