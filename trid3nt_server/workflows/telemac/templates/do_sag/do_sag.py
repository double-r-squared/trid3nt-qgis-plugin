"""Engine template ``telemac_do_sag`` - the dissolved-oxygen sag below a discharge."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources,
)
from trid3nt_server.workflows.telemac.templates.do_sag import streeter_phelps
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "STEERING", "telemac_do_sag"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    outfall_coords = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "with a domain supplied the outfall sits near the top of its "
            "centerline and the sag distance is measured downstream from there; "
            "with no domain either, the run refuses rather than inventing where "
            "a permitted discharge is"),
        desc="Where the discharge enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. On a river with no domain "
             "supplied it is also the seed the reach is walked downstream from")

    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the sag is judged against; 5 is a "
             "common warm-water aquatic-life criterion")


_RESULT = "r2d_domain.slf"

_REACH_LENGTH_KM = 12.0

# holds the source node off the inflow face when no outfall is placed
_OUTFALL_FRAC = 0.02

_OUTFALL = Placed("source", point="outfall_coords", fraction=_OUTFALL_FRAC,
                  label="Outfall")

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

# one set of kinetics: the closed-form overlay grades nothing at other rates
_WATER_TEMPERATURE_C = 20.0
_K1_PER_DAY = 0.3
_K2_PER_DAY = 0.9
# saturation at 20 C
_SATURATION_MGL = 9.022


class DATA:
    domain = Data.need("hydrography", at="outfall_coords",
                       span_km=_REACH_LENGTH_KM)
    line = Data.supplied(geometry="polyline")
    bed = Data.need("bathymetry")
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this domain "
        "at that cycle")
    level = Data.need("water level series").optional()


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

DOC = dict(
    summary="DISSOLVED-OXYGEN SAG below a discharge (US TMDL / permit question).",
    routing=(
        "THE tool for \"where does dissolved oxygen bottom out below this discharge\", "
        "\"will the DO sag violate the standard\", \"Streeter-Phelps oxygen sag\", \"BOD "
        "loading downstream of a WWTP / outfall\". TELEMAC-2D + WAQTEL O2 over a reach "
        "walked downstream from the outfall, or a polygon you supply: clean water in "
        "at the inflow, CBOD decaying and reaeration recovering downstream of the "
        "DISCHARGE. Produces the along-channel oxygen profile against the closed "
        "form. Deck opinions, by keyword: CONSTANT OF DEGRADATION OF ORGANIC LOAD K1, "
        "K2 REAERATION COEFFICIENT, O2 SATURATION DENSITY OF WATER (CS), WATER "
        "TEMPERATURE, DURATION, and the outfall's own discharge and load. Give "
        "`outfall_coords` or `domain`."
    ),
    not_for=(
        "a conservative dye/tracer plume that only dilutes "
        "(`telemac_dye_release`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`); a closed body with no through-flow, which "
        "has no sag"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and bed source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the oxygen profile on its Streeter-Phelps curve. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_do_sag = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_oxygen", "location"),),
    coerce=(
        point_arg("outfall_coords", tool="telemac_do_sag",
                  prompt="Click on the water where the outfall discharges",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
