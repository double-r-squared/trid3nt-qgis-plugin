"""Engine template ``telemac_ice_cover`` - when a body of water freezes over."""

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
from trid3nt_server.workflows.telemac.modules import KHIONE, T2D, series
from trid3nt_server.workflows.telemac.modules.khione import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.outputs import reference_line
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Atmosphere, Boundaries,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "ICE_THICKNESS", "OUTPUTS",
           "INPUTS", "STEERING", "telemac_ice_cover"]


# total thickness: the border ice and the dynamic cover over it
ICE_THICKNESS = "COV_THT"

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_REACH_LENGTH_KM = 12.0


_STATION = Placed("station", point="station", label="Ice station")

# one value per KHIONE tracer, in the order it appends them
_INFLOW_ICE = [0.0, 0.0, 0.0]


class INPUTS:
    seed = Input().stated()
    station = Input().stated()
    cover_threshold = Input()

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

    boundaries = Boundaries(tracers=["observe", *_INFLOW_ICE])

    atmosphere = Atmosphere(observed="weather")

    coupling = [KHIONE.ice(
        geometry=GEOMETRY_FILE, boundary=BOUNDARY_CONDITIONS_FILE,
        ATMOSPHERE_WATER_EXCHANGE_MODEL=1,
        DYNAMIC_ICE_COVER=True,
        MODEL_FOR_MASS_EXCHANGE_BETWEEN_FRAZIL_AND_ICE_COVER=1,
        BORDER_ICE_COVER=True,
        GRAPHIC_PRINTOUT_PERIOD=GRAPHIC_PRINTOUT_PERIOD,
        LISTING_PRINTOUT_PERIOD=LISTING_PRINTOUT_PERIOD)]


OUTPUTS = [
    series("DYNCOVC", at="station", module="khione").chart(
        reference=reference_line("cover_threshold", label="cover threshold")),
    series("DYNCOVT", at="station", module="khione").chart(),
]
CAPTIONS = {"DYNCOVC": "ice cover fraction", "DYNCOVT": "ice cover thickness",
           "discharge": "a streamflow", "level": "a water-surface elevation",
           "observe": "a water temperature"}

_METADATA = AtomicToolMetadata(
    name="telemac_ice_cover",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (STEERING.RESULTS_FILE, RESULT_FILENAME)

REVIEW_TITLE = "Review the water, the cold snap, and what it opens at"

telemac_ice_cover = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("ice_cover_thickness", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_ice_cover",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_ice_cover",
                  prompt="Click on the water where the ice should be read",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
