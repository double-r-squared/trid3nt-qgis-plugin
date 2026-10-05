"""Engine template ``telemac_water_temperature`` - how warm a body of water gets."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL, series
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Atmosphere, Boundaries,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "STEERING", "telemac_water_temperature"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the channel the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer. Geocode a place name first. It seeds the reach "
             "the domain is cut from; supply the domain polygon - a lake, a "
             "pond, a harbour - instead and this is not read")
    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the series is read at the point 98% of the way down the modelled "
            "domain, which is the water that has been exposed to the weather "
            "longest"),
        desc="Where the temperature series and its diurnal range are read, as a "
             "Point: the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon' or a point layer. Geocode a place name first")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target element edge length the domain is triangulated at; a "
             "surface heat budget is divided by the local DEPTH, so what this "
             "has to resolve is how deep the water is rather than its planform")


# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_REACH_LENGTH_KM = 12.0

# the far end, which has been under the weather longest
_STATION_FRAC = 0.98

_STATION = Placed("station", point="station", fraction=_STATION_FRAC,
                  label="Temperature station")


class DATA:
    domain = Data.need("hydrography", at="seed",
                       span_km=_REACH_LENGTH_KM)

    bed = Data.need("bathymetry")

    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this "
        "domain at that cycle")
    level = Data.need("water level series").optional()

    weather = Data.need("weather forcing")
    observe = Data.need("water quality sample", of="TEMPERATURE",
                        at="station")


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
    GRAPHIC_PRINTOUT_PERIOD = 12000

    DURATION = 604800.0

    # the thermic process attaches its budget to this tracer by name
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]

    boundaries = Boundaries(tracers="INITIAL_VALUES_OF_TRACERS")

    atmosphere = Atmosphere(observed="weather", at="station",
                            duration_s="DURATION",
                            event_time="event_time")

    coupling = [WAQTEL.thermal()]


OUTPUTS = [
    series("T1", at="station").chart(),
    series("T1", at="station").station(),
]
CAPTIONS = {"T1": "water temperature", "discharge": "a streamflow",
            "level": "a water-surface elevation", "observe": "a water temperature"}

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the water, the week of weather, and what it opens at"

DOC = dict(
    summary="WATER TEMPERATURE over a body of water under a week of real weather.",
    routing=(
        "THE tool for \"how warm does this water get this week\", \"water "
        "temperature under the heat wave\", \"too warm for salmon / trout\", "
        "\"diurnal temperature swing in the water\". WAQTEL THERMIC on "
        "TELEMAC-2D over the domain it is given - a drawn pond, a picked lake, "
        "or the reach the seed stands on: the surface heat budget under the "
        "hourly RAWS record over the run's window. Produces the TEMPERATURE "
        "field, animated, and the series at a point. Deck opinions, by "
        "keyword: DURATION (seven days), GRAPHIC PRINTOUT PERIOD, LAW OF "
        "BOTTOM FRICTION, FRICTION COEFFICIENT. Supply the domain or `seed` a "
        "point, and `event_time` - the moment the week opens at."
    ),
    not_for=(
        "dissolved oxygen below a discharge (`telemac_do_sag`); a dye or "
        "contaminant plume (`telemac_dye_release`); stratification over depth "
        "(`telemac3d_stratified_flow`); air temperature or a forecast, which "
        "the weather fetchers answer"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved discharge, the weather station and '
         'the opening water temperature for review/edit before the solve and '
         'WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the temperature series charted at the station. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_water_temperature = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("water_temperature", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_water_temperature",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_water_temperature",
                  prompt="Click on the water where the temperature should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
