"""Engine template ``telemac_channel_dredging`` - a maintenance dredge of a channel."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, LINE
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import GAIA, T2D, mass_balance
from trid3nt_server.workflows.telemac.modules.gaia import (
    Dig, Dredging, RESULT_FILENAME,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "STEERING", "telemac_channel_dredging"]


_GEOMETRY = "channel.slf"
_BOUNDARY = "channel.cli"
_RESULT = "r2d_channel.slf"

_REACH_LENGTH_KM = 2.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_BED_STOCK_M = 5.0

# NESTOR dates every action against this origin
_TIME_ORIGIN = [2000, 1, 1, 0, 0, 0]

# levels read off the authored profile file, the opening water surface
_REFERENCE_LEVEL = "SECTIONS"


class INPUTS:
    seed_point = Input()

    design_depth_m = Input()
    trigger_depth_m = Input()

    dredge_start_s = Input()
    dredge_end_s = Input()
    dredge_repeat_s = Input()

    dig_rate_m_per_s = Input()
    dump_rate_m_per_s = Input()
    min_volume_m3 = Input()

    domain = Input(DOMAIN).need("hydrography", at="seed_point",
                                span_km=_REACH_LENGTH_KM)
    line = Input(LINE).need(geometry="polyline")
    bed = Input(BED).need("channel survey")
    discharge = Input(DISCHARGE).need("discharge series")
    dredge_area = Input().need(geometry="polygon")
    dump_area = Input().need(geometry="polygon")
    level = Input(LEVEL).need("water level series")


_DREDGE = Measured(
    "dredge", kind="dredge",
    reads={"areas": {"dredge_area": "dredge_area", "dump_area": "dump_area"},
           "grade_depth_m": "design_depth_m"},
    asked={"dug_area": "dredge_area", "stock_m": _BED_STOCK_M})


class STEERING(T2D):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    LISTING_PRINTOUT_PERIOD = 500

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

    MASS_BALANCE = True

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    # hydraulic seconds; the bed clock is this times the morphological factor
    DURATION = 3600.0

    ORIGINAL_DATE_OF_TIME = _TIME_ORIGIN[:3]
    ORIGINAL_HOUR_OF_TIME = _TIME_ORIGIN[3:]

    boundaries = Boundaries(tracers=[])

    coupling = [GAIA.bed(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        MASS_BALANCE=True,
        CLASSES_SEDIMENT_DIAMETERS=[2.0e-4],
        LAYERS_INITIAL_THICKNESS=[_BED_STOCK_M],
        MORPHOLOGICAL_FACTOR=10.0,
        dredging=Dredging(
            measured="dredge",
            actions=[Dig(field="dredge_area",
                         level=_REFERENCE_LEVEL,
                         start="dredge_start_s", end="dredge_end_s",
                         repeat="dredge_repeat_s",
                         rate="dig_rate_m_per_s",
                         depth="design_depth_m",
                         crit_depth="trigger_depth_m",
                         min_volume="min_volume_m3",
                         dump="dump_area",
                         dump_rate="dump_rate_m_per_s")],
            origin=_TIME_ORIGIN))]


OUTPUTS = [mass_balance(module="gaia").note()]
CAPTIONS = {"discharge": "a streamflow", "level": "a water level",
            "mass_balance": "the sediment closure and the dredged and dumped volumes"}

_METADATA = AtomicToolMetadata(
    name="telemac_channel_dredging",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, RESULT_FILENAME)

REVIEW_TITLE = "Review the dredge, the bed and the mesh"

telemac_channel_dredging = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("cumul_bed_evol", "peak"),),
    coerce=(
        point_arg("seed_point", tool="telemac_channel_dredging",
                  prompt="Click on the channel this dredge works in",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
