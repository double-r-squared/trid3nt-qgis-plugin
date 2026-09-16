"""The CONTRACT of the water-temperature question: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a temperature run can be HANDED. TELEMAC-2D solves on triangles, and the
#: heat budget is a SURFACE exchange over the whole domain rather than anything
#: released at a place, so a triangulation is the whole of it.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a water-temperature question asks: where to cut a stretch of
    channel when no domain is handed in, the week of weather the water is driven
    over, and where the series is read. The domain, the bed, what the water opens
    at and the granularity are the runtime's own slots and levers, and the deck's
    roughness and cadence are keywords the module's dictionary describes."""

    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the channel the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer, or a place name geocoded first. It seeds the reach "
             "the domain is cut from; supply the domain polygon - a lake, a "
             "pond, a harbour - instead and this is not read")
    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is resolved from the NOAA National "
            "Water Model over the domain; no NWM coverage refuses typed rather "
            "than falling back to a constant"),
        desc="Steady upstream discharge - the flow whose depth and travel time "
             "set how much heat the water picks up per kilometre")
    # THE WEATHER WINDOW is the question's own scenario: the water warms under a
    # WEEK that happened, so the dates are what make this a record rather than a
    # hypothetical. There is no default - a temperature nobody dated is nobody's -
    # and the window is TWO dates rather than one string because it is two dates
    # the observation fetch is asked for.
    weather_start = Param(
        door=doors.QUESTION, consequence="scenario", user_lever=True,
        desc="First day of the observed weather the water is driven over, "
             "'YYYY-MM-DD' - from phrasing like 'last week' or 'the first week "
             "of August'. The station record is hourly and the network holds the "
             "last two weeks, so an earlier day refuses typed")
    weather_end = Param(
        door=doors.QUESTION, consequence="scenario", user_lever=True,
        desc="Last day of the observed weather, 'YYYY-MM-DD'. The run is "
             "sim_duration_s long from the first observation, so this day has to "
             "be far enough past weather_start to cover it; at most 14 days after")
    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the series is read at the point 98% of the way down the modelled "
            "domain, which is the water that has been exposed to the weather "
            "longest"),
        desc="Where the temperature series and its diurnal range are read, as a "
             "Point: the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer, or a place name")

    sim_duration_s = Param(
        door=doors.SCENARIO, default=604800.0,
        bounds=(3600.0, 1209600.0), units="s", consequence="numerical",
        user_lever=True,
        desc="Simulated time. A DIURNAL RANGE needs whole days, and this defaults "
             "to seven; a shorter run answers what the water did over that "
             "window and no more. The weather record has to span every second of "
             "it - a run longer than its own forcing refuses rather than "
             "extrapolating")
    # The runtime's own granularity lever, restated ONLY for its default: a
    # surface heat budget is a domain-scale answer watched over a week, and the
    # edge also sets the CFL step, so the runtime's 14 m spends the whole compute
    # budget resolving a planform this question does not read. Bounds and meaning
    # are the lever's.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=20.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the domain is triangulated at; a surface "
             "heat budget is divided by the local DEPTH, so what this has to "
             "resolve is how deep the water is rather than its planform")


DOC = dict(
    summary="WATER TEMPERATURE over a body of water under a week of real weather.",
    routing=(
        "THE tool for \"how warm does this water get this week\", \"water "
        "temperature under the heat wave\", \"too warm for salmon / trout\", "
        "\"diurnal temperature swing in the water\". WAQTEL THERMIC on "
        "TELEMAC-2D over the domain it is given - a drawn pond, a picked lake, "
        "or the reach the seed stands on: the full surface heat budget, "
        "shortwave in, longwave out, evaporation and sensible heat, under the "
        "hourly RAWS record over the days you name. "
        "Produces the TEMPERATURE field, animated, and the series at a point. "
        "Supply the domain or `seed` a point, and `weather_start` / "
        "`weather_end`; the water opens at the nearest sample unless "
        "`water_temperature` states the number."
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
