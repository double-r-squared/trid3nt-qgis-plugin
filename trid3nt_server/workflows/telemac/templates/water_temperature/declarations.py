"""The CONTRACT of the water-temperature question: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a temperature run can be HANDED. TELEMAC-2D solves on triangles, and the
#: heat budget is a SURFACE exchange over the whole domain rather than anything
#: released at a place, so a triangulation is the whole of it.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a water-temperature question asks: which kind of water the
    seed is on and where to cut it when no domain is handed in, and where the
    series is read. The domain, the bed, what the water opens at, the moment the
    week of weather is read at and the granularity are the runtime's own slots
    and levers, and the clock, the roughness and the cadence are keywords the
    module's dictionary describes and the deck states by their own names."""

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
    # WHICH KIND OF WATER the question is asked of, where no domain is handed
    # in. Both are hydrography and both are mapped at the same seed, so the
    # feature the domain row asks for is what tells them apart: a reach arrives
    # cut to length with its two end transects, which is where the inflow and
    # the outflow are prescribed, and a closed body arrives as one outline that
    # states no run and whose whole edge is wall.
    body = Param(
        door=doors.QUESTION, optional=True, default="reach",
        consequence="aoi", user_lever=True,
        desc="Which kind of water the seed is on: 'reach' (default) cuts a "
             "stretch of river from the mapped channel; 'waterbody' takes the "
             "lake, pond or reservoir the seed stands in. Read only when no "
             "domain polygon is supplied")
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

    # The runtime's own granularity lever, restated ONLY for its default: a
    # surface heat budget is a domain-scale answer watched over a week, and the
    # edge also sets the CFL step, so the runtime's 14 m spends the whole compute
    # budget resolving a planform this question does not read. Bounds and meaning
    # are the lever's.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=20.0,
        desc="Target element edge length the domain is triangulated at; a "
             "surface heat budget is divided by the local DEPTH, so what this "
             "has to resolve is how deep the water is rather than its planform")


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
        "point, and `event_time` - the moment the week opens at; a lake or "
        "pond is `body='waterbody'`."
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
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data."),
    ),
    returns=(
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, plus "
        "the temperature series charted at the station. `answer` carries "
        "`peak_temperature_c` / `peak_temperature_time_s` (the warmest the water "
        "at the point got and when) / `final_temperature_c` / `diurnal_range_c` / "
        "`temperature_spread_c` (how far apart the warmest and coolest water in "
        "the domain ended up) / `mean_velocity_mps`; narrate those typed "
        "numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
