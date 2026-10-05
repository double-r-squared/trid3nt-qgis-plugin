"""Engine template ``telemac_bed_scour`` - a mobile bed under moving water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import GAIA, T2D, series
from trid3nt_server.workflows.telemac.modules.gaia import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries, Sources, TracerNames,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "GRADATION_PRESETS",
           "OUTPUTS", "PARAMS", "STEERING", "telemac_bed_scour"]


GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}

ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the marker sits at spill_fraction along the domain the mesh was "
            "built over"),
        desc="Where the marker enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. Its name becomes the "
             "marker's name, "
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

    sediment_gradation = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=list | str,
        derived_when_absent=(
            "the bed is ONE class at the CLASSES SEDIMENT DIAMETERS the deck "
            "states, which is uniform by construction and cannot sort"),
        desc="Multi-class GRADED sediment: a preset name (graded_sand | "
             "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list "
             "of [d50_um, fraction] pairs; a mixture sorts under a hiding factor")


_RELEASE = Placed("source", point="release", fraction="spill_fraction",
                  label="Release point")

_GEOMETRY = "domain.slf"
_BOUNDARY = "domain.cli"
_RESULT = "r2d_domain.slf"

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0


class DATA:
    domain = Data.need("hydrography", at="release",
                       span_km=_REACH_LENGTH_KM)
    bed = Data.need("bathymetry")
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this domain "
        "at that cycle")
    level = Data.need("water level series").optional()


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

    # times the morphological factor, this hour is ten hours of bed
    DURATION = 3600.0

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    NUMBER_OF_TRACERS = 1
    tracer_names = TracerNames(names=["MARKER          MG/L"])
    INITIAL_VALUES_OF_TRACERS = [0.0]

    boundaries = Boundaries(tracers=[0.0])

    WATER_DISCHARGE_OF_SOURCES = [8.0]
    # the deposited fraction is read against this; the bed change is not
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0]
    sources = Sources(window_s="spill_duration_s")

    coupling = [GAIA.bed(geometry=_GEOMETRY, boundary=_BOUNDARY,
                         gradation="sediment_gradation", presets=GRADATION_PRESETS,
                         CLASSES_SEDIMENT_DIAMETERS=[1.0e-4],
                         LAYERS_INITIAL_THICKNESS=[5.0],
                         HIDING_FACTOR_FORMULA=1,
                         MORPHOLOGICAL_FACTOR=10.0,
                         MASS_BALANCE=True)]


OUTPUTS = [
    series("T1").chart(),
]
CAPTIONS = {"T1": "marker concentration", "discharge": "a streamflow"}

_METADATA = AtomicToolMetadata(
    name="telemac_bed_scour",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, RESULT_FILENAME)

REVIEW_TITLE = "Review the mobile-bed scenario"

DOC = dict(
    summary="Bed SCOUR and DEPOSITION: a mobile bed under moving water.",
    routing=(
        "THE tool for \"where does the bed scour and where does it re-deposit\" - "
        "erodible-bed morphodynamics below a dam, weir or bridge, bedload "
        "under a flood, and how a GRADED mixture sorts and armors. "
        "TELEMAC-2D + GAIA over a reach walked from the release point, an "
        "estuary you draw, or a polygon you supply, its bed painted from a "
        "published channel survey where one covers it. Deck opinions, by "
        "keyword: DURATION 3600 s, MORPHOLOGICAL FACTOR 10, CLASSES SEDIMENT "
        "DIAMETERS 2e-4 m, LAYERS INITIAL THICKNESS 5 m, BED-LOAD TRANSPORT "
        "FORMULA FOR ALL SANDS 1. Give `release`, or supply `domain`."
    ),
    not_for=(
        "a SUSPENDED plume settling onto an inert bed "
        "(`telemac_sediment_plume`); a conservative dye or contaminant plume "
        "(`telemac_dye_release`); an OIL slick (`telemac_oil_spill`); a "
        "maintenance DREDGE (`telemac_channel_dredging`); dissolved-oxygen sag "
        "(`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`)"
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
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "the bed evolution in metres among them (deposition positive, scour "
        "negative), plus the marker series charted. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

telemac_bed_scour = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("cumul_bed_evol", "peak"),),
    coerce=(
        point_arg("release", tool="telemac_bed_scour",
                  prompt="Click on the water where the sediment marker is injected",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
