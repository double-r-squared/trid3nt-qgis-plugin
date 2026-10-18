"""Engine template ``telemac_oil_spill`` - an oil slick on the water it is spilled onto."""

from __future__ import annotations

import sys

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import T2D, series
from trid3nt_server.workflows.telemac.modules.outputs import drogues
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    DROGUES_FILENAME, Boundaries, Oil, Sources, TracerNames,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "OIL_PRESETS", "OUTPUTS",
           "INPUTS", "STEERING", "telemac_oil_spill"]


OIL_PRESETS: dict[str, dict[str, Any]] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}


_RELEASE = Placed("source", point="release", label="Release point")

_RESULT = "r2d_domain.slf"
_RESTART = "restart_domain.slf"

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


class INPUTS:
    release = Input()

    spill_duration_s = Input()

    oil_type = Input()

    oil_release_step = Input()

    domain = Input(DOMAIN).need("hydrography", at="release",
                                span_km=_REACH_LENGTH_KM)
    bed = Input(BED).need("bathymetry")
    discharge = Input(DISCHARGE).need("discharge series")
    level = Input(LEVEL).need("water level series")


class STEERING(T2D):
    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
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

    RESTART_FILE = _RESTART
    # double precision, which a continuation reads
    PREVIOUS_COMPUTATION_FILE_FORMAT = "SERAFIND"

    DURATION = 3600.0

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    tracer_names = TracerNames(names=["OIL             MG/L"])
    INITIAL_VALUES_OF_TRACERS = [0.0]

    boundaries = Boundaries(tracers=[0.0])

    WATER_DISCHARGE_OF_SOURCES = [8.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    sources = Sources(window_s="spill_duration_s")

    oil = Oil(presets=OIL_PRESETS, named="oil_type",
              release_step="oil_release_step")

    MAXIMUM_NUMBER_OF_DROGUES = 100

    # in solver steps, not seconds
    PRINTOUT_PERIOD_FOR_DROGUES = 60


OUTPUTS = [
    series("T1").chart(),
    drogues().layer(),
]
CAPTIONS = {"T1": "dissolved oil concentration", "drogues": "oil slick track",
            "discharge": "a streamflow", "level": "a water-surface elevation"}

_METADATA = AtomicToolMetadata(
    name="telemac_oil_spill",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, _RESTART, DROGUES_FILENAME)

REVIEW_TITLE = "Review the oil spill scenario"

telemac_oil_spill = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_oil_concentration", "peak"),
                 ("oil_slick_track", "location")),
    coerce=(
        point_arg("release", tool="telemac_oil_spill",
                  prompt="Click on the water where the oil enters it",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
