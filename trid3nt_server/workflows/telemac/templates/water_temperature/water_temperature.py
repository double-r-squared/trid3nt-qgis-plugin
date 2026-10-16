"""Engine template ``telemac_water_temperature`` - how warm a body of water gets."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, OBSERVE, WEATHER
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Atmosphere, Boundaries,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "STEERING", "telemac_water_temperature"]


# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_REACH_LENGTH_KM = 12.0

# the far end, which has been under the weather longest
_STATION_FRAC = 0.98

_STATION = Placed("station", point="station", fraction=_STATION_FRAC,
                  label="Temperature station")


class INPUTS:
    seed = Input()
    station = Input()

    mesh_resolution_m = RESOLUTION

    domain = Input(DOMAIN).need("hydrography", at="seed",
                                span_km=_REACH_LENGTH_KM)

    bed = Input(BED).need("bathymetry")

    discharge = Input(DISCHARGE).need("discharge series")
    level = Input(LEVEL).need("water level series")

    weather = Input(WEATHER).need("weather forcing")
    observe = Input(OBSERVE).need("water quality sample", of="TEMPERATURE",
                                  at="station")


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
    GRAPHIC_PRINTOUT_PERIOD = 12000

    DURATION = 604800.0

    # the thermic process attaches its budget to this tracer by name
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]

    boundaries = Boundaries(tracers="INITIAL_VALUES_OF_TRACERS")

    atmosphere = Atmosphere(observed="weather")

    coupling = [WAQTEL.thermal()]


OUTPUTS = [
    series("T1", at="station").chart(),
    series("T1", at="station").station(),
]
CAPTIONS = {"T1": "water temperature", "discharge": "a streamflow",
            "level": "a water-surface elevation", "observe": "a water temperature"}

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the water, the week of weather, and what it opens at"

telemac_water_temperature = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("water_temperature", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_water_temperature",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_water_temperature",
                  prompt="Click on the water where the temperature should be read",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
