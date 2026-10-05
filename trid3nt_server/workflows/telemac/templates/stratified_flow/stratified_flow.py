"""Engine template ``telemac3d_stratified_flow`` - what a 2D model cannot see."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Param, doors, lever,
)
from trid3nt_server.workflows.runtime.resolution import SensitivityDecl
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.authoring.accepted_mesh import (
    BASIN_BOUNDARY, BASIN_GEOMETRY,
)
from trid3nt_server.workflows.telemac.modules import column
from trid3nt_server.workflows.telemac.modules.telemac3d import (
    T3D, Column, VerticalGrid,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS", "STEERING",
           "telemac3d_stratified_flow"]


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the domain polygon supplied on the call is the body of water the "
            "run solves over"),
        desc="A point ON or beside the body of water this question is about, "
             "as a Point: the pick's {coordinates, name} verbatim, a (lon, lat) "
             "pair, 'lat,lon' or a point layer (geocode a place name first). "
             "The mapped "
             "outline that point names becomes the domain; a domain supplied "
             "directly supersedes it, and a body nobody mapped is drawn")

    warm_temp_c = Param(
        door=doors.SCENARIO, default=25.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Epilimnion (warm surface layer) temperature the column OPENS at. "
             "The run exchanges no heat with the atmosphere, so what happens to "
             "this difference is the whole answer")
    cold_temp_c = Param(
        door=doors.SCENARIO, default=15.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Hypolimnion (cold bottom layer) temperature; the initial "
             "top-to-bottom difference is what the run either keeps or mixes away")
    thermocline_depth_m = Param(
        door=doors.SCENARIO, default=8.0,
        bounds=(0.5, 200.0), units="m", consequence="physics",
        desc="Depth of the thermocline below the free surface. The vertical grid "
             "is planned to HOLD it and REFUSES when no admissible sigma stretch "
             "over the domain's deepest column can")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target triangle edge the water body's interior is meshed at. The "
             "horizontal spends its budget on COVERING the body rather than on "
             "detail; the 3D node count is this mesh's nodes times the planes "
             "the deck states")


_RESULT_3D = "res3d_basin.slf"
_RESULT_2D = "res2d_basin.slf"

# in solver steps, not seconds
_LISTING_PERIOD = 100


class DATA:
    domain = Data.need("hydrography", at="seed")
    bed = Data.need("bathymetry")
    level = Data.need("water level series").context(
        "no water-level gauge published a reading over this domain that day; "
        "the free surface opens at the zero the bed is counted from")


class STEERING(T3D):
    GEOMETRY_FILE = BASIN_GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BASIN_BOUNDARY
    RD_RESULT_FILE = _RESULT_3D
    ED_RESULT_FILE = _RESULT_2D

    DURATION = 18000.0
    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 360
    LISTING_PRINTOUT_PERIOD = _LISTING_PERIOD
    MASS_BALANCE = True

    NUMBER_OF_HORIZONTAL_LEVELS = 13
    # the dictionary's own default is YES
    NON_HYDROSTATIC_VERSION = False

    # flat at the gauge-observed level; the dictionary's zero is chart datum
    INITIAL_CONDITIONS = "CONSTANT ELEVATION"
    # LECDON stops unless the law and its coefficient are stated together
    LAW_OF_BOTTOM_FRICTION = 5
    FRICTION_COEFFICIENT_FOR_THE_BOTTOM = 0.01

    # the dictionary's 1e-6 is molecular; the tracer pair has no default
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_TRACERS = [1.0e-4]
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_TRACERS = [1.0e-4]

    # the engine finds the temperature by the first sixteen characters
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]
    INITIAL_VALUES_OF_TRACERS = [0.0]
    DENSITY_LAW = 1
    AVERAGE_WATER_DENSITY = 1000.0

    # no 3D default; MURD PSI stops a baroclinic solve, NERD is monotone
    SCHEME_FOR_ADVECTION_OF_TRACERS = [13]
    # the dictionary's 4 is unknown to murd3d_pos, which answers to 1 and 2
    SCHEME_OPTION_FOR_ADVECTION_OF_TRACERS = [2]

    vertical_grid = VerticalGrid(thermocline_depth_m="thermocline_depth_m")
    column = Column(thermocline_depth_m="thermocline_depth_m",
                    warm_c="warm_temp_c", cold_c="cold_temp_c")


OUTPUTS = [
    column("T1").chart(reference=column("T1", t=0)),
]
CAPTIONS = {"T1": "water temperature", "level": "a water level"}

_TELEMAC3D_METADATA = AtomicToolMetadata(
    name="telemac3d_stratified_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT_3D, _RESULT_2D)

DISPLAY_FILE = _RESULT_2D

PREFIX = "telemac3d"

REVIEW_TITLE = "Review the prescribed column, the deck and the mesh"

DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a body of water a 2D depth-averaged "
            "model cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"wind-driven vertical "
        "circulation\". TELEMAC-3D baroclinic coupling over sigma layers on a "
        "CLOSED body of water - a lake, a reservoir, a pond, a pit - naming no "
        "liquid boundary. ONE run gives both the temperature difference that "
        "SURVIVES and the opposed surface / bottom velocities a stated wind "
        "drives. `seed` names or points at the body and finds its mapped "
        "outline; `domain` takes a polygon, `bed` a survey raster, soundings "
        "or a depth in metres. Deck: DURATION 18000 s, NUMBER OF HORIZONTAL "
        "LEVELS 13, calm; set each by its keyword name."
    ),
    not_for=(
        "a 2D depth-averaged plume or transport question; inundation "
        "DEPTH; coastal storm-tide flooding; harbour wave agitation "
        "(`artemis_harbor_agitation`); a salinity intrusion up an estuary, "
        "which needs a tidal liquid boundary a closed body has none of"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved column, the deck it is solved '
         'under and the authored mesh for review/edit before the solve and '
         'WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "plus the temperature column at the deepest node charted against "
        "the prescribed initial column. The run exchanges NO heat with the "
        "atmosphere, so a falling surface temperature is downward MIXING "
        "and never the water cooling - narrate it that way. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)

telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, sys.modules[__name__],
    sensitivity=SensitivityDecl((("water_temperature", "gradient"),
                                 ("velocity_u", "gradient")),
                                lever="NUMBER_OF_HORIZONTAL_LEVELS"),
    coerce=(
        point_arg("seed", tool="telemac3d_stratified_flow",
                  prompt="Click on the body of water this run solves over",
                  code="TELEMAC3D_PARAMS_INVALID"),
        event_time(),
    ),
)
