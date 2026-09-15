"""The CONTRACT of ``telemac_river_temperature``: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a temperature run can be HANDED. TELEMAC-2D solves on triangles, and the
#: heat budget is a SURFACE exchange over the whole reach rather than anything
#: released at a place, so a triangulation is the whole of it.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Place name on the river, geocoded to the reach")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place")
    river_geometry_uri = Param(
        door=doors.USER, optional=True, consequence="aoi",
        derived_when_absent="the reach flowline is fetched fresh for the AOI",
        desc="Reuse an already-fetched river flowline for this reach instead of "
             "re-fetching it")
    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is resolved from the NOAA National "
            "Water Model at the reach; no NWM coverage refuses typed rather "
            "than falling back to a constant"),
        desc="Steady upstream discharge - the flow whose depth and travel time "
             "set how much heat the reach picks up per kilometre")
    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The moment to read the carrier discharge cycle at - an ISO date "
             "or datetime. The NWM PDS bucket retains only the last ~30 days "
             "of history; a deeper request refuses typed.")
    # THE WEATHER WINDOW is the question's own scenario: the reach warms under a
    # WEEK that happened, so the dates are what make this a record rather than a
    # hypothetical. There is no default - a temperature nobody dated is nobody's -
    # and the window is TWO dates rather than one string because it is two dates
    # the observation fetch is asked for.
    weather_start = Param(
        door=doors.QUESTION, consequence="scenario", user_lever=True,
        desc="First day of the observed weather the reach is driven over, "
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
            "reach, which is the water that has been exposed to the weather "
            "longest"),
        desc="Where the temperature series and its diurnal range are read, as a "
             "Point: the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer, or a place name")
    initial_water_temp_c = Param(
        door=doors.USER, optional=True, bounds=(0.0, 40.0), units="C",
        user_lever=True, consequence="physics",
        derived_when_absent=(
            "the reach opens at the water temperature the nearest USGS / EPA "
            "Water Quality Portal sample site reports, with that site and its "
            "sample date on the provenance note; no sample site refuses typed "
            "rather than opening at a guessed temperature"),
        desc="Water temperature the whole reach opens at, and the value carried "
             "in at the upstream boundary")
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            "the reach is solved at Strickler 33.0, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical", type=int,
        derived_when_absent="the coefficient is read as a Strickler one",
        desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, "
             "4=Manning")
    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        desc="Result-writing cadence; unset keeps the steering file's own period")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")

    reach_length_km = Param(
        door=doors.SCENARIO, default=12.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modelled reach length downstream of the geocoded point; the water "
             "warms with the time it spends in the reach, so the length is how "
             "much exposure the question asks about")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=604800.0,
        bounds=(3600.0, 1209600.0), units="s", consequence="numerical",
        user_lever=True,
        desc="Simulated time. A DIURNAL RANGE needs whole days, and this defaults "
             "to seven; a shorter run answers what the reach did over that "
             "window and no more. The weather record has to span every second of "
             "it - a run longer than its own forcing refuses rather than "
             "extrapolating")
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=20.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; a surface "
             "heat budget is a reach-scale answer, so this is coarser than a "
             "plume question asks for")


DOC = dict(
    summary="WATER TEMPERATURE down a river reach under a week of real weather.",
    routing=(
        "THE tool for \"how warm does this river get this week\", \"water "
        "temperature under the heat wave\", \"is the reach too warm for salmon / "
        "trout\", \"thermal regime of this stretch\", \"diurnal temperature "
        "swing in the river\". Solves TELEMAC-2D + WAQTEL THERMIC (process 11) "
        "over a REAL NHDPlus reach: the full surface heat budget - shortwave in, "
        "longwave out, evaporation and sensible heat - driven by the hourly RAWS "
        "station record over the days you name. Produces a TEMPERATURE field on "
        "the mesh, animated over the week, plus the temperature series at a point. "
        "Supply `location` OR `bbox`, and the two dates `weather_start` / "
        "`weather_end`."
    ),
    not_for=(
        "dissolved oxygen below a discharge (`telemac_do_sag`); a dye or "
        "contaminant plume (`telemac_river_dye`); lake stratification over depth "
        "(`telemac3d_stratified_flow`); air temperature or a forecast, which the "
        "weather fetchers answer directly"
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
        "`warming_along_reach_c` / `mean_velocity_mps`; narrate those typed "
        "numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
