"""Engine template ``telemac_oil_spill`` - an oil slick on the water it is spilled onto."""

from __future__ import annotations

import sys

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import T2D, series
from trid3nt_server.workflows.telemac.modules.outputs import drogues
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    DROGUES_FILENAME, Boundaries, Oil, Sources, TracerNames,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OIL_PRESETS", "OUTPUTS",
           "PARAMS", "STEERING", "telemac_oil_spill"]


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

ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the slick's drift is measured from there"),
        desc="Where the oil enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. Its name becomes the "
             "tracer's name, "
             "and on a river with no domain supplied it is also the seed the "
             "reach is walked downstream from")

    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary")
    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")

    oil_type = Param(
        door=doors.QUESTION, default="light_crude", consequence="scenario",
        desc="Which preset was spilled: light_crude | diesel | heavy_fuel - the "
             "module's own composition, density and viscosity. Crude runs as "
             "light_crude, gasoline and petrol as diesel, bunker as heavy_fuel")

    oil_release_step = Param(
        door=doors.SCENARIO, default=600, bounds=(1.0, 1.0e6), type=int,
        consequence="scenario",
        desc="The solver step the floats are released at, compiled into the "
             "module's own release routine; it lets the flow field establish "
             "before the slick is put on it")


_RELEASE = Placed("source", point="release", fraction="spill_fraction",
                  label="Release point")

_RESULT = "r2d_domain.slf"
_RESTART = "restart_domain.slf"

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


class DATA:
    domain = Data.need("hydrography", at="release",
                       span_km=_REACH_LENGTH_KM)
    bed = Data.need("bathymetry")
    discharge = Data.need("discharge series")
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

DOC = dict(
    summary="An OIL SLICK released onto a body of surface water: floating particles plus the dissolved fraction.",
    routing=(
        "THE tool for \"an oil spill - where does the slick go\": a barge, "
        "pipeline, terminal or vessel release of crude, diesel, gasoline or "
        "heavy fuel onto water. TELEMAC-2D over a reach walked from the "
        "release, a harbour or lake you draw, or a polygon you supply, with the "
        "engine's oil-spill module on the solve: the floats draw the slick and "
        "the dissolved fraction advects as a tracer. Give "
        "`release` as a pick or a pair, or supply `domain`. Source: "
        "ABSCISSAE/ORDINATES/DISCHARGE/TRACER. Deck: DURATION 3600 s, "
        "MAXIMUM NUMBER OF DROGUES 100, PRINTOUT PERIOD FOR DROGUES 60, "
        "no WIND, no RAIN."
    ),
    not_for=(
        "a conservative dye or contaminant plume with no slick "
        "(`telemac_dye_release`); bed SCOUR (`telemac_bed_scour`); a "
        "SUSPENDED sediment plume (`telemac_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`). Weathering, evaporation and "
        "beaching are the module's own, uncalibrated here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the filled sheet for review/edit before the solve '
         'and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "plus the dissolved-oil series charted and the slick track from the "
        "floats. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_oil_spill = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_oil_concentration", "peak"),
                 ("oil_slick_track", "location")),
    coerce=(
        point_arg("release", tool="telemac_oil_spill",
                  prompt="Click on the water where the oil enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
