"""Engine template ``telemac_sediment_plume`` - a settling plume in a body of water."""

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

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "SEDIMENT_CONCENTRATION_MGL", "SOURCE_Q_M3S", "STEERING",
           "telemac_sediment_plume"]


SOURCE_Q_M3S = 8.0
SEDIMENT_CONCENTRATION_MGL = 100.0

ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the plume's travel is measured from there"),
        desc="Where the sediment enters the water, as a Point: the pick's "
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

DOC = dict(
    summary="A SUSPENDED SEDIMENT plume in a body of water: it settles and deposits on the bed.",
    routing=(
        "THE tool for \"sediment / silt / a turbidity plume released into the "
        "water, where does it settle out\" - a slurry spill, a construction or "
        "dredging discharge, an upstream sediment supply. TELEMAC-2D + GAIA "
        "over a reach walked from the release, a lake or harbour drawn on the "
        "canvas, or a supplied polygon: ONE settling class over a bed with NO "
        "stock, so nothing erodes and only what was injected deposits. Give "
        "`release` as a pick or a pair, or supply `domain`. Deck: "
        "DURATION 3600 s, CLASSES SEDIMENT DIAMETERS 1.0e-4 m, SUSPENSION "
        "TRANSPORT FORMULA FOR ALL SANDS 3, SCHEME FOR ADVECTION OF SUSPENDED "
        "SEDIMENTS 1, no WIND and no RAIN OR EVAPORATION - set each by its "
        "keyword name."
    ),
    not_for=(
        "bed SCOUR or an erodible bed (`telemac_bed_scour`); a conservative dye "
        "or contaminant plume (`telemac_dye_release`); an OIL slick "
        "(`telemac_oil_spill`); dissolved-oxygen sag (`telemac_do_sag`)"
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
        "the bed evolution among them, plus the suspended-sediment series "
        "charted. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_sediment_plume = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("suspended_sediment_concentration", "peak"),
                 ("cumul_bed_evol", "peak")),
    coerce=(
        point_arg("release", tool="telemac_sediment_plume",
                  prompt="Click on the water where the sediment enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
