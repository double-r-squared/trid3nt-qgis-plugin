"""Engine template ``telemac_dye_release`` - a conservative plume through a body of water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources, TracerNames,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DECAY_PRESETS", "DOC", "OUTPUTS",
           "PARAMS", "STEERING", "telemac_dye_release"]


DECAY_PRESETS: dict[str, dict[str, float]] = {
    "sewage": {"law": 1, "coef": 2.0},
    "e. coli": {"law": 1, "coef": 2.0},
    "e.coli": {"law": 1, "coef": 2.0},
    "e coli": {"law": 1, "coef": 2.0},
    "ecoli": {"law": 1, "coef": 2.0},
    "coliform": {"law": 1, "coef": 2.0},
    "coli": {"law": 1, "coef": 2.0},
    "bacteria": {"law": 1, "coef": 2.0},
    "bacterial": {"law": 1, "coef": 2.0},
    "effluent": {"law": 1, "coef": 2.0},
    "wastewater": {"law": 1, "coef": 2.0},
    "die-off": {"law": 1, "coef": 2.0},
    "decaying": {"law": 2, "coef": 0.35},
    "half-life": {"law": 2, "coef": 0.35},
}

ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the travelled distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
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

    decaying_substance = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the tracer is CONSERVATIVE - it dilutes and advects and nothing "
            "removes it"),
        desc="Name a substance whose tracer DECAYS - sewage | E. coli | coliform "
             "| bacteria | effluent | wastewater - and its narrated literature "
             "die-off is applied as a first-order sink on the plume")


_RELEASE = Placed("source", point="release", fraction="spill_fraction",
                  label="Release point", continues=True)

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
    discharge = Data.need("discharge series").optional()
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

    DURATION = 3600.0

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    tracer_names = TracerNames(names=["DYE             MG/L"])
    INITIAL_VALUES_OF_TRACERS = [0.0]

    boundaries = Boundaries(tracers=[0.0])

    WATER_DISCHARGE_OF_SOURCES = [8.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    sources = Sources(window_s="spill_duration_s")

    coupling = [WAQTEL.degradation(substance="decaying_substance",
                                   presets=DECAY_PRESETS)]


OUTPUTS = [
    series("T1").chart(),
]
CAPTIONS = {"T1": "dye concentration", "discharge": "a streamflow",
            "level": "a water-surface elevation"}

_METADATA = AtomicToolMetadata(
    name="telemac_dye_release",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, _RESTART)

REVIEW_TITLE = "Review the tracer-release scenario"

DOC = dict(
    summary="A DYE / TRACER / CONTAMINANT plume released into a body of surface water and carried by its flow.",
    routing=(
        "THE tool for \"a spill in the water - how far does it travel, how "
        "concentrated\": a dye / contaminant / pollutant / chemical plume carried "
        "by the current, sewage or E.coli effluent DECAYING as it goes (name it "
        "in `decaying_substance`). TELEMAC-2D over a reach walked from the "
        "release, a lake drawn on the canvas, or a supplied polygon: a finite "
        "pulse is carried by the flow and dilutes. Give `release` as a pick or a "
        "pair, or supply `domain`. Source: ABSCISSAE/ORDINATES/DISCHARGE/TRACER "
        "keywords. Deck: DURATION 3600 s, no WIND, no RAIN."
    ),
    not_for=(
        "an OIL slick (`telemac_oil_spill`); bed SCOUR, deposition, grain "
        "sorting (`telemac_bed_scour`); a SUSPENDED sediment plume settling "
        "onto the bed (`telemac_sediment_plume`); dissolved-oxygen sag "
        "(`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`). Groundwater plumes, dam-break and tsunami "
        "run-up are not modeled here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point on the canvas before the solve, and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the dye series charted. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

telemac_dye_release = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dye_concentration", "peak"),),
    coerce=(
        point_arg("release", tool="telemac_dye_release",
                  prompt="Click on the water where the substance enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
