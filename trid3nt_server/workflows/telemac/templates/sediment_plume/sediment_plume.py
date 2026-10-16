"""Engine template ``telemac_sediment_plume`` - a settling plume in a body of water."""

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

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "SEDIMENT_CONCENTRATION_MGL", "SOURCE_Q_M3S", "STEERING",
           "telemac_sediment_plume"]


SOURCE_Q_M3S = 8.0
SEDIMENT_CONCENTRATION_MGL = 100.0


_RELEASE = Placed("source", point="release", fraction="spill_fraction",
                  label="Release point")

_GEOMETRY = "domain.slf"
_BOUNDARY = "domain.cli"
_RESULT = "r2d_domain.slf"

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


class INPUTS:
    release = Input()

    spill_fraction = Input()
    spill_duration_s = Input()

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

    DURATION = 3600.0

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    tracer_names = TracerNames(names=["MARKER          MG/L"])
    INITIAL_VALUES_OF_TRACERS = [0.0]

    boundaries = Boundaries(tracers=[0.0, 0.0])

    WATER_DISCHARGE_OF_SOURCES = [SOURCE_Q_M3S]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [SEDIMENT_CONCENTRATION_MGL]
    sources = Sources(window_s="spill_duration_s")

    coupling = [GAIA.suspended(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        concentration_mgl=SEDIMENT_CONCENTRATION_MGL,
        CLASSES_SEDIMENT_DIAMETERS=[3.0e-5],
        SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=3,
        SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=[1],
        MASS_BALANCE=True)]


OUTPUTS = [
    series("T2").chart(),
]
CAPTIONS = {"T2": "suspended sediment concentration", "discharge": "a streamflow"}

_METADATA = AtomicToolMetadata(
    name="telemac_sediment_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, RESULT_FILENAME)

REVIEW_TITLE = "Review the sediment-plume scenario"

telemac_sediment_plume = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("suspended_sediment_concentration", "peak"),
                 ("cumul_bed_evol", "peak")),
    coerce=(
        point_arg("release", tool="telemac_sediment_plume",
                  prompt="Click on the water where the sediment enters it",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
