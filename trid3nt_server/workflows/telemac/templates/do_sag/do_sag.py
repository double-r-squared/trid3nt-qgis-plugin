"""Engine template ``telemac_do_sag`` - the dissolved-oxygen sag below a discharge."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, LINE
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL
from trid3nt_server.workflows.runtime.primitives import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources,
)
from trid3nt_server.workflows.telemac.templates.do_sag import streeter_phelps
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "STEERING", "telemac_do_sag"]


_RESULT = "r2d_domain.slf"

_REACH_LENGTH_KM = 12.0


_OUTFALL = Placed("source", point="outfall_coords", label="Outfall")

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

# one set of kinetics: the closed-form overlay grades nothing at other rates
_WATER_TEMPERATURE_C = 20.0
_K1_PER_DAY = 0.3
_K2_PER_DAY = 0.9
# saturation at 20 C
_SATURATION_MGL = 9.022


class INPUTS:
    outfall_coords = Input()

    do_standard_mgl = Input()

    domain = Input(DOMAIN).need("hydrography", at="outfall_coords",
                                span_km=_REACH_LENGTH_KM)
    line = Input(LINE).need(geometry="polyline")
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

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 5000

    # two days: several travel times, and long against 1/k1
    DURATION = 172800.0

    # WAQTEL O2 appends O2, ORGANIC LOAD, NH4 LOAD: tracer arrays carry four
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0, _SATURATION_MGL, 0.0, 0.0]

    boundaries = Boundaries(tracers=[0.0, _SATURATION_MGL, 0.0, 0.0])

    WATER_DISCHARGE_OF_SOURCES = [1.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [0.0, 2.0, 250.0, 0.0]
    sources = Sources()

    coupling = [WAQTEL.o2(
        WATER_TEMPERATURE=_WATER_TEMPERATURE_C, WATER_SALINITY=0.0,
        CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1=_K1_PER_DAY,
        CONSTANT_OF_NITRIFICATION_KINETIC_K4=0.0,
        FORMULA_FOR_COMPUTING_K2=0, K2_REAERATION_COEFFICIENT=_K2_PER_DAY,
        O2_SATURATION_DENSITY_OF_WATER__CS_=_SATURATION_MGL,
        BENTHIC_DEMAND=0.0, PHOTOSYNTHESIS_P=0.0, VEGETAL_RESPIRATION_R=0.0)]


OUTPUTS = [profile("T2", along="line").chart(
    reference=streeter_phelps.overlay(saturation_mgl=_SATURATION_MGL,
                                      k1_per_day=_K1_PER_DAY,
                                      k2_per_day=_K2_PER_DAY))]
CAPTIONS = {"T2": "dissolved oxygen", "discharge": "a streamflow",
           "level": "a water level"}

_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the outfall and the water it discharges to"

telemac_do_sag = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_oxygen", "location"),),
    coerce=(
        point_arg("outfall_coords", tool="telemac_do_sag",
                  prompt="Click on the water where the outfall discharges",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
