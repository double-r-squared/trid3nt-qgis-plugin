"""Engine template ``telemac_eutrophication`` - what one pass through"""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.outputs import (
    profile, reference_line,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "STEERING", "telemac_eutrophication"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the channel the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer. Geocode a place name first. The stretch walked "
             "downstream of it is the water one pass is measured over; supply "
             "the domain polygon instead and this is not read")

    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the two history charts are read as the domain-wide maximum at each "
            "instant rather than at one place"),
        desc="Where to watch the biomass and the oxygen over time, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon' or a point layer (geocode a place name first). The "
             "profiles are all longitudinal and do not move with it")

    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the water is judged against; 5 is a "
             "common warm-water aquatic-life criterion. It never reaches the "
             "deck: no keyword names a standard, and the oxygen profile carries "
             "it as a reference line")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target element edge length the domain is triangulated at; it also "
             "sets the CFL time step, so it is what decides whether a long "
             "window finishes")


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


class DATA:
    domain = Data.need("hydrography", at="seed",
                       span_km=_REACH_LENGTH_KM)
    line = Data.supplied(geometry="polyline")

    bed = Data.need("bathymetry")

    discharge = Data.need("discharge series").optional()

    observe = Data.need("water quality sample").context(
        "no water-quality site near this domain reports a water "
        "temperature; the stated value stands")
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
    series("T1", at="station").chart(),
    series("T8", at="station").chart(),
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

DOC = dict(
    summary="NUTRIENT ENRICHMENT in a body of water: algal growth, nutrient "
            "drawdown and the oxygen response over one pass through it.",
    routing=(
        "THE tool for \"what does this water do to its nutrient load\", \"how much "
        "algae grows down this river\", \"eutrophication of this stream\", \"does "
        "the algal growth pull the oxygen down\". Solves TELEMAC-2D + WAQTEL "
        "EUTRO: phytoplankton growing on STATED nitrate and phosphate under stated "
        "light and temperature, dying back, and the oxygen budget that drives. "
        "Answers the LONGITUDINAL change over one pass. Supply the domain polygon, "
        "or `seed` a point on it. Opinions, as keywords: DURATION, INITIAL VALUES "
        "OF TRACERS (eight, in process order), WATER TEMPERATURE, SUNSHINE FLUX "
        "DENSITY ON WATER SURFACE, GRAPHIC PRINTOUT PERIOD."
    ),
    not_for=(
        "the oxygen sag below a WASTEWATER DISCHARGE (`telemac_do_sag`); a "
        "conservative plume that only dilutes (`telemac_dye_release`); a SEASONAL "
        "bloom in standing water - this measures ONE PASS. Nutrients are STATED"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and the deck the '
         'run is written on for review/edit before the solve and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a "
         "physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote - the eight tracers the eutrophication process "
        "appends among them - styled on one mesh layer, animated where it "
        "varies, plus the biomass and the oxygen along the water's path as "
        "charts and their history where the station was picked. On failure "
        "a dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_eutrophication = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("dissolved_o2", "location"),
                 ("phyto_biomass", "location")),
    coerce=(
        point_arg("seed", tool="telemac_eutrophication",
                  prompt="Click on the channel where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_eutrophication",
                  prompt="Click where you want the water watched",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
