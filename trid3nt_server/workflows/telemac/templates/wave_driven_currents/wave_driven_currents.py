"""Engine template ``tomawac_wave_driven_currents`` - the current the waves drive."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.slots import BED, DOMAIN, EXTENT, LEVEL, WAVE
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.modules import T2D, WAC, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["CAPTIONS", "MESH", "OUTPUTS", "INPUTS", "STEERING",
           "tomawac_wave_driven_currents"]


_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_RESULT = "t2d_coast.slf"
_STEERING_FILE = "t2d_wave_driven.cas"

_DURATION_S = 3600.0
# both decks march this clock; modules stepping apart are two runs on one mesh
_TIME_STEP_S = 1.0
# in host steps
_COUPLING_PERIOD = 60
_WAVE_TIME_STEP_S = _TIME_STEP_S * _COUPLING_PERIOD
_WAVE_STEPS = int(_DURATION_S / _WAVE_TIME_STEP_S)
_HOST_FRAMES = int(_DURATION_S / _TIME_STEP_S) // _WAVE_STEPS

# Nikuradse: the coefficient is a grain roughness in metres
_FRICTION_LAW = 5
_FRICTION_COEFFICIENT = 0.05


class INPUTS:
    seed = Input().stated()

    mesh_resolution_m = RESOLUTION

    open_depth_threshold_m = Input()

    extent = Input(EXTENT).need(geometry="rectangle")
    domain = Input(DOMAIN).need("hydrography", kind="coastline", geometry="polyline")
    bed = Input(BED).need("bathymetry")
    wave = Input(WAVE).need("wave series", at="seed")
    level = Input(LEVEL).need("water level series")


class STEERING(T2D):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT

    TIME_STEP = _TIME_STEP_S
    DURATION = _DURATION_S
    GRAPHIC_PRINTOUT_PERIOD = _HOST_FRAMES
    LISTING_PRINTOUT_PERIOD = _HOST_FRAMES

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 500
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6

    # the swash dries and wets every wave
    TREATMENT_OF_NEGATIVE_DEPTHS = 1

    MASS_BALANCE = True

    boundaries = Boundaries(tracers=[])

    coupling = [WAC.wave(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        TIME_STEP=_WAVE_TIME_STEP_S,
        NUMBER_OF_TIME_STEP=_WAVE_STEPS,
        NUMBER_OF_DIRECTIONS=24,
        NUMBER_OF_FREQUENCIES=25,
        MINIMAL_FREQUENCY=0.04,
        TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM=6,
        DEPTH_INDUCED_BREAKING_DISSIPATION=1,
        BOTTOM_FRICTION_DISSIPATION=1)]

    COUPLING_PERIOD_FOR_TOMAWAC = _COUPLING_PERIOD


MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[
        mesh_op("feature_sizing_function"),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source="bed"),
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold="open_depth_threshold_m"),
    ],
)

OUTPUTS = [
    series("M").chart(),
    series("HM0", module="tomawac").chart(),
]
CAPTIONS = {"M": "current speed", "U": "current along x", "V": "current along y",
            "HM0": "significant wave height", "BETA": "breaking rate",
            "wave": "a sea state",
            "level": "the tide the open edge holds, over the run's window"}

_METADATA = AtomicToolMetadata(
    name="tomawac_wave_driven_currents",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

MESH_ON = "domain"

RESULTS = (_RESULT, RESULT_FILENAME)

STEERING_FILE = _STEERING_FILE
PREFIX = "t2d"

REVIEW_TITLE = "Review the sea state, the tide and the water they drive"

tomawac_wave_driven_currents = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    sensitivity=(("current_speed", "peak"),),
    coerce=(
        point_arg("seed", tool="tomawac_wave_driven_currents",
                  prompt="Click offshore, where the incoming sea state is "
                         "measured",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
