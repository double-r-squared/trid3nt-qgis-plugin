"""Engine template ``telemac_micropollutant_release`` - a sorbing substance in water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "STEERING", "telemac_micropollutant_release"]


_RELEASE = Placed("source", point="release", fraction="release_fraction",
                  label="Release point")

_MONITORING = Placed("monitoring", point="monitoring_point",
                     fraction="monitoring_fraction",
                     label="Monitoring point")

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

# ADDTRACER matches sixteen characters, so micropol adopts this tracer
_DISSOLVED = "MICRO POLLUTANT MG/L"


class INPUTS:
    release = Input()
    release_fraction = Input()
    release_duration_s = Input()

    monitoring_point = Input()
    monitoring_fraction = Input()

    domain = Input(DOMAIN).need("hydrography", at="release",
                                span_km=_REACH_LENGTH_KM)
    bed = Input(BED).need("bathymetry")
    discharge = Input(DISCHARGE).need("discharge series")
    level = Input(LEVEL).need("water level series")


class STEERING(T2D):
    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = "r2d_domain.slf"
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
    GRAPHIC_PRINTOUT_PERIOD = 5000

    DURATION = 172800.0

    # micropol appends SS, bed sediment, two sorbed phases: arrays carry five
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = [_DISSOLVED]
    INITIAL_VALUES_OF_TRACERS = [0.0, 0.03, 0.0, 0.0, 0.0]

    boundaries = Boundaries(tracers=[0.0, 0.03, 0.0, 0.0, 0.0])

    WATER_DISCHARGE_OF_SOURCES = [1.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0, 0.0, 0.0, 0.0, 0.0]
    sources = Sources(window_s="release_duration_s")

    coupling = [WAQTEL.micropollutant()]


OUTPUTS = [
    series("T1", at="monitoring").chart(),
]
CAPTIONS = {"T1": "dissolved micropollutant", "discharge": "a streamflow",
            "level": "a water level"}

_METADATA = AtomicToolMetadata(
    name="telemac_micropollutant_release",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the release and the sediment it partitions onto"

telemac_micropollutant_release = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_micropollutant", "peak"),),
    coerce=(
        point_arg("release", tool="telemac_micropollutant_release",
                  prompt="Click on the water where the substance enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("monitoring_point", tool="telemac_micropollutant_release",
                  prompt="Click where downstream to read the dissolved history",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
