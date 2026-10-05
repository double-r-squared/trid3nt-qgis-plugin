"""Engine template ``telemac_ice_cover`` - when a body of water freezes over."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.workflows.telemac.modules import KHIONE, T2D, series
from trid3nt_server.workflows.telemac.modules.khione import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.outputs import reference_line
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Atmosphere, Boundaries,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "ICE_THICKNESS", "OUTPUTS",
           "PARAMS", "STEERING", "telemac_ice_cover"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the water the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer. Geocode a place name first. It seeds the reach the "
             "domain is cut from; supply the domain polygon - a lake, a pond, a "
             "reservoir - instead and this is not read")
    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the series is read at the point 98% of the way down the modelled "
            "domain, which is the water that has been under the cold longest"),
        desc="Where the ice cover and its thickness are read over time, as a "
             "Point: the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon' or a point layer. Geocode a place name first")
    cover_threshold = Param(
        door=doors.QUESTION, optional=True, default=0.5,
        consequence="scenario", user_lever=True, type=float,
        desc="Fraction of the surface under ice, 0 to 1, that counts as frozen "
             "over - the cover chart draws it as a reference line beside the "
             "series. A reporting threshold, not a physical constant: half the "
             "surface by default")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target element edge length the domain is triangulated at; the "
             "budget that makes the ice is divided by the local DEPTH, and the "
             "border ice grows from the BANK, so what this has to resolve is "
             "how deep the water is and where its edge runs")


# total thickness: the border ice and the dynamic cover over it
ICE_THICKNESS = "COV_THT"

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_REACH_LENGTH_KM = 12.0

# the far end, which has lost heat to the air longest
_STATION_FRAC = 0.98

_STATION = Placed("station", point="station", fraction=_STATION_FRAC,
                  label="Ice station")

# one value per KHIONE tracer, in the order it appends them
_INFLOW_ICE = [0.0, 0.0, 0.0]


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
                        at="station").context(
        "no sample near this domain in this window; the stated value stands")


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

    boundaries = Boundaries(tracers=["observe", *_INFLOW_ICE])

    atmosphere = Atmosphere(observed="weather", at="station",
                            duration_s="DURATION",
                            event_time="event_time")

    coupling = [KHIONE.ice(
        geometry=GEOMETRY_FILE, boundary=BOUNDARY_CONDITIONS_FILE,
        ATMOSPHERE_WATER_EXCHANGE_MODEL=1,
        DYNAMIC_ICE_COVER=True,
        MODEL_FOR_MASS_EXCHANGE_BETWEEN_FRAZIL_AND_ICE_COVER=1,
        BORDER_ICE_COVER=True,
        GRAPHIC_PRINTOUT_PERIOD=GRAPHIC_PRINTOUT_PERIOD,
        LISTING_PRINTOUT_PERIOD=LISTING_PRINTOUT_PERIOD)]


OUTPUTS = [
    series("DYNCOVC", at="station", module="khione").chart(
        reference=reference_line("cover_threshold", label="cover threshold")),
    series("DYNCOVT", at="station", module="khione").chart(),
]
CAPTIONS = {"DYNCOVC": "ice cover fraction", "DYNCOVT": "ice cover thickness",
           "discharge": "a streamflow", "level": "a water-surface elevation",
           "observe": "a water temperature"}

_METADATA = AtomicToolMetadata(
    name="telemac_ice_cover",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (STEERING.RESULTS_FILE, RESULT_FILENAME)

REVIEW_TITLE = "Review the water, the cold snap, and what it opens at"

DOC = dict(
    summary="ICE COVER under a cold snap: when water freezes over, and how "
            "thick.",
    routing=(
        "THE tool for \"when does this water freeze over\", \"how thick does "
        "the ice get\", \"frazil and border ice\". KHIONE on "
        "TELEMAC-2D over the domain it is given, drawn or matched at `seed`: "
        "the heat budget under the hourly airport record over the run's "
        "window, the frazil it makes, and the cover that grows from it. "
        "Produces cover fraction and thickness, animated and charted. "
        "Deck opinions, by keyword: DURATION (seven days), GRAPHIC "
        "PRINTOUT PERIOD, LAW OF BOTTOM FRICTION, FRICTION COEFFICIENT; on the "
        "ice deck ATMOSPHERE-WATER EXCHANGE MODEL, DYNAMIC ICE COVER, MODEL FOR "
        "MASS EXCHANGE BETWEEN FRAZIL AND ICE COVER, BORDER ICE COVER. Supply "
        "the domain or `seed`, and `event_time`, the moment the snap opens "
        "at."
    ),
    not_for=(
        "how warm the water gets with no ice in the question "
        "(`telemac_water_temperature`); snow or ice on LAND; air temperature "
        "or a forecast, which the weather fetchers answer"
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
        "plus the cover and thickness series charted at the station. Water "
        "that makes no ice at all charts zero thickness and zero cover, "
        "which is what that place did in that week. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

telemac_ice_cover = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("ice_cover_thickness", "peak"),),
    coerce=(
        point_arg("seed", tool="telemac_ice_cover",
                  prompt="Click on the water where the modelled stretch starts",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="telemac_ice_cover",
                  prompt="Click on the water where the ice should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
