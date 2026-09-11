"""The CONTRACT of ``telemac_do_sag``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.workflows.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an outfall run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what an outfall reach can be handed as a mesh;
#: the discharge enters the water at a POINT, which is the one release geometry
#: the sag pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


class PARAMS:
    # -- the reach ---------------------------------------------------------- #
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
        desc="Steady upstream CARRIER discharge - the river flow that dilutes "
             "and transports the release")
    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The storm/event moment to read the carrier discharge cycle at - "
             "from phrasing like 'during last Tuesday's storm'; an ISO date "
             "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
             "Unset reads the most recent published NWM cycle. The NWM PDS "
             "bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a "
             "different cycle.")
    # The friction the reach is solved at when the ask states none is named on
    # the row because the outflow stage is a normal depth AT this roughness: a
    # stage derived at one number under a deck written at another is a level
    # the run never sits at. The assembler reads the same number.
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            "the reach is solved at Strickler 33.0, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
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

    outfall_coords = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the release is seeded at the reach point the pipeline derives "
            "(mid-reach on the fetched flowline, else the geocoded centroid); the "
            "sag distance is measured downstream from there"),
        desc="Where the discharge enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name; unset seeds the reach at the derived "
             "reach point")

    effluent_bod_mgl = Param(
        door=doors.SCENARIO, default=250.0,
        bounds=(0.1, 5000.0), units="mg/L", consequence="scenario",
        desc="Ultimate carbonaceous BOD IN THE DISCHARGE ITSELF - what leaves the "
             "outfall pipe, before any dilution; the reach's mixed load is what "
             "the solve computes from this and the carrier flow")
    effluent_q_m3s = Param(
        door=doors.SCENARIO, default=1.0,
        bounds=(0.0001, 1000.0), units="m^3/s", consequence="scenario",
        desc="Discharge rate at the outfall - with the carrier flow this sets the "
             "dilution, and so how much of the effluent load the river carries")
    effluent_do_mgl = Param(
        door=doors.SCENARIO, default=2.0,
        bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="Dissolved oxygen in the discharge itself; a treated effluent arrives "
             "oxygen-poor, which is the initial deficit the sag starts from")
    water_temp_c = Param(
        door=doors.SCENARIO, default=20.0, bounds=(0.0, 40.0),
        units="C", consequence="scenario",
        desc="Water temperature, which sets the DO saturation the deficit is "
             "measured against; 20 C is the standard Streeter-Phelps condition")
    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the sag is judged against; 5 is a "
             "common warm-water aquatic-life criterion")
    k1_per_day = Param(
        door=doors.SCENARIO, default=0.3, bounds=(0.01, 20.0),
        units="1/day", consequence="numerical",
        desc="CBOD deoxygenation rate - a documented rate coefficient")
    k2_per_day = Param(
        door=doors.SCENARIO, default=0.9, bounds=(0.01, 50.0),
        units="1/day", consequence="numerical",
        desc="Surface reaeration rate - a documented rate coefficient")
    reach_length_km = Param(
        door=doors.SCENARIO, default=12.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the discharge; the sag critical "
             "point is often several km down")

    do_saturation_mgl = Param(
        door=doors.DERIVED,
        resolve=f"{_HELPERS}.water_quality.do_saturation_mgl",
        user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="DO saturation Cs; derived from water temperature unless supplied")
    upstream_do_mgl = Param(
        door=doors.DERIVED,
        resolve=f"{_HELPERS}.water_quality.upstream_do_mgl",
        user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="DO carried in at the top of the reach; derived as saturation unless supplied")

    sim_duration_s = Param(
        door=doors.SCENARIO, default=172800.0,
        bounds=(60.0, 864000.0), units="s", consequence="numerical",
        user_lever=True,
        desc="Simulated time. A sag is a STEADY-STATE answer, so this has to cover "
             "several travel times through the reach AND be long against 1/k1 - a "
             "window shorter than that reports a sag that has not developed yet")
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; where the "
             "sag bottoms out is a local feature and moves with the element that "
             "resolves it")

DOC = dict(
    summary="DISSOLVED-OXYGEN SAG below a discharge in a river (US TMDL / permit question).",
    routing=(
        "THE tool for \"where does dissolved oxygen bottom out below this discharge\", "
        "\"will the DO sag violate the standard\", \"Streeter-Phelps oxygen sag\", \"BOD "
        "loading / oxygen demand downstream of a WWTP / outfall\", \"DO TMDL for this "
        "reach\". Solves TELEMAC-2D + WAQTEL O2 over a REAL NHDPlus reach starting "
        "at the outfall: clean river in at the top, the DISCHARGE ITSELF a continuous "
        "point source of organic load and low oxygen, CBOD decaying downstream (k1) "
        "and reaeration (k2) recovering it. Produces a DISSOLVED-O2 field map + the "
        "along-reach oxygen profile against the Streeter-Phelps closed form and "
        "the standard + the minimum's location/value. Supply `location` OR `bbox`."
    ),
    not_for=(
        "a conservative dye/tracer/contaminant plume that only dilutes "
        "(`telemac_river_dye`); rainfall-runoff flood depth from a storm "
        "(`telemac_rain_on_grid`). Groundwater plumes are not currently modeled here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and bank source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success the dissolved-oxygen layer (a `LayerURI`) - the emitter loads "
        "the field map and animates the result mesh beside it - whose `answer` "
        "carries `do_min_mgl` / `do_min_distance_m` / `bod_mixed_mgl` / "
        "`mean_velocity_mps`; judge the minimum against `do_standard_mgl` on the "
        "sheet and narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
