"""Engine template ``telemac_micropollutant_release`` - a sorbing substance in water."""

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
    Boundaries, Sources,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "STEERING", "telemac_micropollutant_release"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at release_fraction along the domain the mesh was "
            "built over; the travelled distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. On a river with no domain "
             "it is also the seed the reach is walked downstream from")
    release_fraction = Param(
        door=doors.SCENARIO, default=0.1, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary. It sits "
             "near the top so the substance has water left to sorb and settle in")
    release_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite injection window; the substance is released over it and the "
             "rest of the run is what happens to what was released")

    monitoring_point = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the history is read at monitoring_fraction along the domain the "
            "mesh was built over"),
        desc="Where the dissolved history is read, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first")
    monitoring_fraction = Param(
        door=doors.SCENARIO, default=0.85, bounds=(0.1, 0.95),
        consequence="scenario",
        desc="Along-domain position the history is read at when no point was "
             "given, 0=inflow..1=outflow")


_RELEASE = Placed("source", point="release", fraction="release_fraction",
                  label="Release point")

_MONITORING = Placed("monitoring", point="monitoring_point",
                     fraction="monitoring_fraction",
                     label="Monitoring point")

_REACH_LENGTH_KM = 6.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

# ADDTRACER matches sixteen characters, so micropol adopts this tracer
_DISSOLVED = "MICRO POLLUTANT MG/L"


class DATA:
    domain = Data.need("hydrography", at="release",
                       span_km=_REACH_LENGTH_KM)
    bed = Data.need("bathymetry")
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this "
        "domain at that cycle")
    level = Data.need("water level series").optional()


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
    GRAPHIC_PRINTOUT_PERIOD = 5000

    DURATION = 172800.0

    # micropol appends SS, bed sediment, two sorbed phases: arrays carry five
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = [_DISSOLVED]
    INITIAL_VALUES_OF_TRACERS = [0.0, 0.03, 0.0, 0.0, 0.0]

    boundaries = Boundaries(tracers=[0.0, 0.03, 0.0, 0.0, 0.0])

    WATER_DISCHARGE_OF_SOURCES = [1.0]
    VALUES_OF_THE_TRACERS_AT_THE_SOURCES = [100.0, 0.0, 0.0, 0.0, 0.0]
    sources = Sources(window_s="release_duration_s")

    coupling = [WAQTEL.micropollutant()]


OUTPUTS = [
    series("T1", at="monitoring").chart(),
]
CAPTIONS = {"T1": "dissolved micropollutant", "discharge": "a streamflow",
            "level": "a water level"}

_METADATA = AtomicToolMetadata(
    name="telemac_micropollutant_release",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the release and the sediment it partitions onto"

DOC = dict(
    summary="A SORBING substance released into water: how much stays DISSOLVED and how much ends up ON THE BED.",
    routing=(
        "THE tool for \"where does this pollutant END UP\" - a metal, a PCB, a "
        "pesticide, anything that ATTACHES TO SEDIMENT: how much travels "
        "dissolved and how much rides the sediment onto the bed. "
        "TELEMAC-2D + WAQTEL micropol over a reach walked from the release "
        "point, a basin or harbour on the canvas, or a supplied polygon: "
        "a finite point release, the water's own suspended sediment as the "
        "sorbent. Deck opinion: DURATION 172800 s, two "
        "days against a partition that equilibrates in hours; WAQTEL's own "
        "sorption and decay constants stand. Give `release` as a pick "
        "or a pair, or supply `domain`."
    ),
    not_for=(
        "a CONSERVATIVE tracer that only dilutes (`telemac_dye_release`); an OIL "
        "slick (`telemac_oil_spill`); bed SCOUR (`telemac_bed_scour`); a "
        "suspended SEDIMENT plume carrying nothing (`telemac_sediment_plume`); "
        "a dissolved-oxygen sag (`telemac_do_sag`); groundwater contamination"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point and the monitoring point on the canvas before the '
         'solve, and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the dissolved history at the monitoring point charted. On "
        "failure a dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_micropollutant_release = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_micropollutant", "peak"),),
    coerce=(
        point_arg("release", tool="telemac_micropollutant_release",
                  prompt="Click on the water where the substance enters it",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("monitoring_point", tool="telemac_micropollutant_release",
                  prompt="Click where downstream to read the dissolved history",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
