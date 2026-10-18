"""Engine template ``telemac_eutrophication`` - what one pass through"""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, LINE, OBSERVE
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.runtime.primitives import profile
from trid3nt_server.workflows.telemac.modules.outputs import reference_line
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["CAPTIONS", "OUTPUTS", "INPUTS",
           "STEERING", "telemac_eutrophication"]


# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

# also the length of the one pass the water grows algae over
_REACH_LENGTH_KM = 12.0

_CENTERLINE = "line"

# oxygen saturation follows the stated water temperature
_SATURATION_FROM_TEMPERATURE = 1

# PHY (ug/L), then PO4, POR, NO3, NOR, NH4, organic load, O2 (mg/L)
_ENTERING = [2.0, 0.05, 0.02, 1.0, 0.5, 0.05, 2.0, 8.667]
_PHYTO_IN, _PO4_IN, _NO3_IN = _ENTERING[0], _ENTERING[1], _ENTERING[3]


class INPUTS:
    seed = Input().stated()

    do_standard_mgl = Input()

    mesh_resolution_m = RESOLUTION

    domain = Input(DOMAIN).need("hydrography", at="seed",
                                span_km=_REACH_LENGTH_KM)
    line = Input(LINE).need(geometry="polyline")

    bed = Input(BED).need("bathymetry")

    discharge = Input(DISCHARGE).need("discharge series")

    observe = Input(OBSERVE).need("water quality sample")
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
    GRAPHIC_PRINTOUT_PERIOD = 3600
    DURATION = 172800.0

    INITIAL_VALUES_OF_TRACERS = _ENTERING

    boundaries = Boundaries(tracers=_ENTERING)

    coupling = [WAQTEL.eutrophication(
        oxygen=True,
        WATER_TEMPERATURE=22.0,
        SUNSHINE_FLUX_DENSITY_ON_WATER_SURFACE=100.0,
        FORMULA_FOR_COMPUTING_CS=_SATURATION_FROM_TEMPERATURE)]


OUTPUTS = [
    profile("T1", along=_CENTERLINE).chart(
        reference=reference_line(_PHYTO_IN, label="entering")),
    profile("T2", along=_CENTERLINE).chart(
        reference=reference_line(_PO4_IN, label="entering")),
    profile("T4", along=_CENTERLINE).chart(
        reference=reference_line(_NO3_IN, label="entering")),
    profile("T8", along=_CENTERLINE).chart(
        reference=reference_line("do_standard_mgl", label="standard")),
    series("T1", at="observe").chart(),
    series("T8", at="observe").chart(),
]
CAPTIONS = {"T1": "phyto biomass", "T2": "phosphate", "T4": "nitrate",
            "T8": "dissolved o2",
            "discharge": "a streamflow", "level": "a water-surface elevation",
            "observe": "a water temperature"}

_METADATA = AtomicToolMetadata(
    name="telemac_eutrophication",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the water this run is carrying"

telemac_eutrophication = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_o2", "location"),
                 ("phyto_biomass", "location")),
    coerce=(
        point_arg("seed", tool="telemac_eutrophication",
                  prompt="Click on the channel where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
    ),
)
