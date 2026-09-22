"""Engine template ``telemac3d_stratified_flow`` - what a 2D model cannot see.

TELEMAC-3D over sigma layers with active-tracer baroclinic coupling, over the
body of water this run solves on: a lake, a reservoir, a pond, a quarry pit, an
outline drawn on the canvas. The run has NO surface heat exchange, so a falling
surface temperature is the warm layer MIXING DOWNWARD. The domain is CLOSED: it
names no liquid boundary."""

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
from trid3nt_server.workflows.telemac.authoring.assembler import (
    BASIN_BOUNDARY,
    BASIN_GEOMETRY,
)
from trid3nt_server.workflows.telemac.modules import column, mesh
from trid3nt_server.workflows.telemac.modules.telemac3d import (
    T3D,
    Column,
    VerticalGrid,
    Wind,
)
from trid3nt_server.workflows.telemac.templates.stratified_flow.declarations import (
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "OUTPUTS", "PARAMS", "STEERING",
           "telemac3d_stratified_flow"]


#: What the run directory holds the run's files under - the deck's own 3D and 2D
#: RESULT FILE statements. The 3D file is the answer; the 2D file is the depth
#: average the question exists to refuse, written because the engine's own
#: printouts read it.
_RESULT_3D = "res3d_basin.slf"
_RESULT_2D = "res2d_basin.slf"

#: How often the solver prints a listing, in its own steps. The dictionary's own
#: value is 1, which prints every step of a run whose answer is its settled state.
_LISTING_PERIOD = 100

class DATA:
    """The three slots this run stands on: the body of water, its bed, and the
    level its free surface opens at.

    Every row is superseded by what the caller hands in, so the same declaration
    solves a charted Great Lake and a pond nobody has ever mapped."""

    # DRAWN, picked, or the caller's own layer. A body of water is a closed
    # polygon whether a fetcher maps it or nobody ever has, so the match here
    # is a PREFERENCE: where the seed names or points at a mapped body, its
    # outline is the domain, and anything the caller supplies supersedes it.
    # The feature is the WATERBODY by name, so a reach mapped at the same seed
    # never stands in for the body this question stratifies.
    domain = Data.need("hydrography", at=Ref("seed"))
    # THE BED, as the CLASS it is rather than the source it comes from: the
    # measurement where something measured it, the terrain under the rest. A
    # bed is TOPOBATHY and the coastal composites do not reach the Great Lakes
    # at all, so this slot is answered by the survey the lakes are charted on;
    # anywhere else it is a survey raster, a layer of soundings, or the depth
    # in metres the water body holds.
    bed = Data.need("bathymetry")
    # THE LEVEL the free surface opens at, over the day the run is about: ONE
    # measured value off the nearest gauge that watched this water, read on the
    # SAME datum the charted bed above is counted from. ABSENT is legal - a pond
    # has no gauge, and a bed stated as a depth is counted from the free surface
    # itself, so the column opens at the bed's own zero and the sheet says so.
    level = Data.need("water level series").context(
        "no water-level gauge published a reading over this domain that day; "
        "the free surface opens at the zero the bed is counted from")


class STEERING(T3D):
    """The deck: a prescribed column, the planes that hold it, and no wind."""

    TITLE = Ref("settled.title")
    GEOMETRY_FILE = BASIN_GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BASIN_BOUNDARY
    RD_RESULT_FILE = _RESULT_3D
    ED_RESULT_FILE = _RESULT_2D

    # The step the water is solved at follows the edge the accepted mesh was
    # BUILT at, through the CFL producer every domain's step comes from.
    TIME_STEP = Ref("settled.time_step_s")
    # HOW LONG the column is watched, in SECONDS. The answer is the column's
    # SETTLED state rather than an event, so the window is "long enough": five
    # hours is what a basin-scale column takes to either hold its difference or
    # mix it away. A user who wants another horizon sets the keyword by its own
    # name.
    DURATION = 18000.0
    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 120 m default edge the CFL step is 1 s, and the 18000 s DURATION
    # above is about 18,000 of them - one frame every 360 steps is 50 frames
    # of the column. A user who wants another cadence sets the keyword by its
    # own name.
    GRAPHIC_PRINTOUT_PERIOD = 360
    LISTING_PRINTOUT_PERIOD = _LISTING_PERIOD
    MASS_BALANCE = True

    # HOW MANY SIGMA PLANES the column is carried on - the VERTICAL degree of
    # freedom a 2D model has none of, and so what resolves this answer. Thirteen
    # holds a metres-thick thermocline over a lake-deep column under the surface
    # zooming the grid plans, at this mesh's nodes times thirteen; too few for
    # the declared thermocline is a refusal naming the count that would work,
    # never a coarser answer. A user who wants a finer column sets the keyword
    # by its own name.
    NUMBER_OF_HORIZONTAL_LEVELS = 13
    # The dictionary's own default here is YES, and a non-hydrostatic solve of a
    # basin-scale column buys nothing the hydrostatic one does not already show.
    NON_HYDROSTATIC_VERSION = False

    # THE FREE SURFACE the run opens at: flat, and at the level the gauge
    # OBSERVED. The dictionary's own zero is the chart datum, which is where a
    # charted bed is counted from - water left there has none on its rim at all.
    INITIAL_CONDITIONS = "CONSTANT ELEVATION"
    INITIAL_ELEVATION = Ref("settled.level_m")

    # The ONE pair that cannot be left to the dictionary, measured both ways:
    # LECDON stops on "THE LAW OF BOTTOM FRICTION 5 IS ASKED / GIVE THE
    # CORRESPONDING FRICTION COEFFICIENT" when only the law is defaulted, and on
    # "NO FRICTION LAW IS PRESCRIBED!" when only the coefficient is written. It
    # reads the two jointly and takes neither half from the dictionary once the
    # other exists. Both values ARE the dictionary's own; what the engine demands
    # is that they be written.
    LAW_OF_BOTTOM_FRICTION = 5
    FRICTION_COEFFICIENT_FOR_THE_BOTTOM = 0.01

    # The diffusivities a screening column is stable under. The dictionary's own
    # 1e-6 is molecular; water at these scales is not solved at molecular
    # viscosity, and the tracer pair has no default at all.
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_TRACERS = [1.0e-4]
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_TRACERS = [1.0e-4]

    # THE TRACER IS THE PHYSICS: temperature drives the density and the density
    # drives the flow. The engine finds the temperature by the first sixteen
    # characters of its name; the unit rides in the next sixteen, as the
    # dictionary spells a tracer name, and is what the result file carries. The
    # initial values keyword is mandatory even where the Fortran hook overrides
    # it - the solver stops asking for it by name.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]
    INITIAL_VALUES_OF_TRACERS = [0.0]
    DENSITY_LAW = 1
    AVERAGE_WATER_DENSITY = 1000.0

    # HOW THE TEMPERATURE IS CARRIED, and the ceiling that carriage runs under.
    # The dictionary gives the 3D tracer scheme no default, so an unstated deck
    # advects the temperature by whatever the VELOCITIES are advected by (5, MURD
    # PSI), which stops a baroclinic solve at its first tracer step. 13 is the
    # NERD family the telemac2d dictionary defaults this same keyword to and the
    # only one monotone across a thermocline; the ceiling governs schemes 13 and
    # 14 and nothing else, so the two are one statement and are written together.
    SCHEME_FOR_ADVECTION_OF_TRACERS = [13]
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_ADVECTION_SCHEMES = 50
    # AND UNDER WHICH OPTION. The dictionary's own default here is 4, implicit,
    # and murd3d_pos answers to 1 and 2 only - an unstated deck loops "UNKNOWN
    # OPTION IN MURD3D_POS: 4 / OPTION 1 TAKEN INSTEAD" and stops on the explicit
    # option's own iteration ceiling. 2 is the predictor-corrector, which the
    # dictionary's own help calls the faster of the two where there are no tidal
    # flats; a closed body has none.
    SCHEME_OPTION_FOR_ADVECTION_OF_TRACERS = [2]

    #: The sigma grid that can HOLD the declared thermocline over this domain's
    #: own deepest column, or the refusal that says how many planes would. The
    #: plane count is READ off the keyword above, so a run the user states
    #: another one on is planned on the column it is actually solved over.
    vertical_grid = VerticalGrid(levels=Ref("NUMBER_OF_HORIZONTAL_LEVELS"),
                                 max_depth_m=Ref("settled.max_depth_m"),
                                 thermocline_depth_m=P.thermocline_depth_m)
    #: The column the run OPENS with, written into the engine's own initial-
    #: condition hook because no keyword carries a non-uniform tracer field.
    column = Column(levels=Ref("NUMBER_OF_HORIZONTAL_LEVELS"),
                    max_depth_m=Ref("settled.max_depth_m"),
                    thermocline_depth_m=P.thermocline_depth_m,
                    warm_c=P.warm_temp_c, cold_c=P.cold_temp_c,
                    surface_m=Ref("settled.level_m"))
    #: CALM: the half of the pair in which the thermocline persists, and what
    #: this question is asked from. A zero speed writes no wind keyword at all,
    #: so the deck states none; the wind that mixes the column is TELEMAC-3D's
    #: own WIND plus WIND VELOCITY ALONG X / Y, set by those names.
    wind = Wind(speed_mps=0.0, from_deg=270.0)


#: What this question PLACES: the column at the deepest node as the chart, the
#: prescribed initial column drawn beside what survived. The run exchanges no
#: heat with the atmosphere, so the two curves enclose the same heat and a
#: surface that fell is the warm layer mixed downward.
OUTPUTS = [
    column("T1").chart(reference=column("T1", t=0)),
]
CAPTIONS = {"T1": "water temperature", "level": "a water level"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above or of the velocity column beside them: the
#: top-to-bottom difference that survived against the one prescribed, the
#: depth-weighted column means whose drift is the numerical error bar on the
#: mixing, and the surface-downwind / return-flow-at-depth pair a stated wind
#: drove, whose depth average a 2D model reports as nothing.
ANSWER = {
    "stratification_dt": column("T1").measure("top_minus_bottom"),
    "stratification_dt_init": column("T1", t=0).measure("top_minus_bottom"),
    "column_mean_final_c": column("T1").measure("mean"),
    "column_mean_init_c": column("T1", t=0).measure("mean"),
    "column_depth_m": column("T1").measure("depth_m"),
    "u_surface": column("U").measure("top"),
    "u_bottom": column("U").measure("bottom"),
    "depth_avg_u": column("U").measure("mean"),
    "planes": mesh().measure("planes"),
    "mesh_size_m": mesh().measure("size_m"),
}


_TELEMAC3D_RES_SPEC = ResolutionSpec(
    param="NUMBER_OF_HORIZONTAL_LEVELS",
    unit="planes",
    min_value=5.0,
    native_hint="the thermocline the run declares, which the grid plan must hold",
    constraint_source="solver",
    rationale=(
        "the VERTICAL degree of freedom, which is the one a 2D model has none of, "
        "and the module's own keyword rather than a lever beside it. Too few "
        "planes for the declared thermocline over this domain's deepest column is "
        "a refusal naming the count that would work, not a coarser answer"
    ),
)

_TELEMAC3D_METADATA = AtomicToolMetadata(
    name="telemac3d_stratified_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC3D_RES_SPEC,),
)


#: The two files the run has to write. Stated rather than read off the deck
#: because a 3D deck names them 3D RESULT FILE and 2D RESULT FILE, and what
#: the workflow reads back is the single RESULTS FILE a 2D deck states.
RESULTS = (_RESULT_3D, _RESULT_2D)

#: A 3D SELAFIN is no mesh format MDAL opens: the module writes the 2D
#: result over the same mesh, and a plane of a 3D field is drawn onto it.
DISPLAY_FILE = _RESULT_2D

#: What the run directory calls the deck, and where the staged files live.
PREFIX = "telemac3d"

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the prescribed column, the deck and the mesh"


telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, sys.modules[__name__],
    provenance=(("thermocline_depth_m", "thermocline_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # The surface-to-bottom temperature difference is read ACROSS the thermocline,
    # the steepest gradient in the domain, and the planes are what resolve it.
    sensitivity=(("stratification_dt", "gradient"),
                 ("u_surface", "gradient"),
                 ("u_bottom", "gradient")),
    coerce=(
        point_arg("seed", tool="telemac3d_stratified_flow",
                  prompt="Click on the body of water this run solves over",
                  code="TELEMAC3D_PARAMS_INVALID"),
        event_time(),
    ),
)
