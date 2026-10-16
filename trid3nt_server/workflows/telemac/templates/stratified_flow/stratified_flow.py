"""Engine template ``telemac3d_stratified_flow`` - what a 2D model cannot see."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DOMAIN, LEVEL
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.workflows.runtime.resolution import SensitivityDecl
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.authoring.accepted_mesh import (
    BASIN_BOUNDARY, BASIN_GEOMETRY,
)
from trid3nt_server.workflows.telemac.modules import column
from trid3nt_server.workflows.telemac.modules.telemac3d import (
    T3D, Column, VerticalGrid,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS", "STEERING",
           "telemac3d_stratified_flow"]


_RESULT_3D = "res3d_basin.slf"
_RESULT_2D = "res2d_basin.slf"

# in solver steps, not seconds
_LISTING_PERIOD = 100


class INPUTS:
    seed = Input()

    warm_temp_c = Input()
    cold_temp_c = Input()
    thermocline_depth_m = Input()

    mesh_resolution_m = RESOLUTION

    domain = Input(DOMAIN).need("hydrography", at="seed")
    bed = Input(BED).need("bathymetry")
    level = Input(LEVEL).need("water level series")


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

telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, sys.modules[__name__],
    sensitivity=SensitivityDecl((("water_temperature", "gradient"),
                                 ("velocity_u", "gradient")),
                                lever="NUMBER_OF_HORIZONTAL_LEVELS"),
    coerce=(
        point_arg("seed", tool="telemac3d_stratified_flow",
                  prompt="Click on the body of water this run solves over",
                  code="TELEMAC3D_PARAMS_INVALID"),
    ),
)
