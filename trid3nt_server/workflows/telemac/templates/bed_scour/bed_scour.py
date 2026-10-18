"""Engine template ``telemac_bed_scour`` - a mobile bed under moving water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import GAIA, T2D, series
from trid3nt_server.workflows.telemac.modules.gaia import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources, TracerNames,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "GRADATION_PRESETS",
           "OUTPUTS", "INPUTS", "STEERING", "telemac_bed_scour"]


GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}


_RELEASE = Placed("source", point="release", label="Release point")

_GEOMETRY = "domain.slf"
_BOUNDARY = "domain.cli"
_RESULT = "r2d_domain.slf"

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


class INPUTS:
    release = Input()

    spill_duration_s = Input()

    sediment_gradation = Input()

    domain = Input(DOMAIN).need("hydrography", at="release",
                                span_km=_REACH_LENGTH_KM)
    bed = Input(BED).need("bathymetry")
    discharge = Input(DISCHARGE).need("discharge series")
    level = Input(LEVEL).need("water level series")


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

    # times the morphological factor, this hour is ten hours of bed
    DURATION = 3600.0

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    tracer_names = TracerNames(names=["MARKER          MG/L"])
    INITIAL_VALUES_OF_TRACERS = [0.0]

    boundaries = Boundaries(tracers=[0.0])

    WATER_DISCHARGE_OF_SOURCES = [8.0]
    # the deposited fraction is read against this; the bed change is not
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    sources = Sources(window_s="spill_duration_s")

    coupling = [GAIA.bed(geometry=_GEOMETRY, boundary=_BOUNDARY,
                         gradation="sediment_gradation", presets=GRADATION_PRESETS,
                         CLASSES_SEDIMENT_DIAMETERS=[1.0e-4],
                         LAYERS_INITIAL_THICKNESS=[5.0],
                         HIDING_FACTOR_FORMULA=1,
                         MORPHOLOGICAL_FACTOR=10.0,
                         MASS_BALANCE=True)]


OUTPUTS = [
    series("T1").chart(),
]
CAPTIONS = {"T1": "marker concentration", "discharge": "a streamflow"}

_METADATA = AtomicToolMetadata(
    name="telemac_bed_scour",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, RESULT_FILENAME)

REVIEW_TITLE = "Review the mobile-bed scenario"

telemac_bed_scour = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("cumul_bed_evol", "peak"),),
    coerce=(
        point_arg("release", tool="telemac_bed_scour",
                  prompt="Click on the water where the sediment marker is injected",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
