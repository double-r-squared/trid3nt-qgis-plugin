"""The CONTRACT of ``telemac_do_sag``: its declared params and its prose.

One file over from the recipe. ``do_sag.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the forty rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an outfall run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what an outfall reach can be handed as a mesh;
#: the discharge enters the water at a POINT, which is the one release geometry
#: the sag pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))

_STEPS = "trid3nt_server.workflows.telemac.steps"


PARAMS: tuple[Param, ...] = (
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name near the discharge, geocoded to the reach"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place"),
    Param("outfall_coords", door=doors.USER, optional=True, consequence="scenario",
          user_lever=True, type=tuple[float, float] | list[float],
          derived_when_absent=(
              "the release is seeded at the reach point the pipeline derives "
              "(mid-reach on the fetched flowline, else the geocoded centroid); the "
              "sag distance is measured downstream from there"),
          desc="Where the discharge enters the water, (lon, lat); unset seeds the "
               "reach at the derived reach point"),

    Param("discharge_bod_mgl", door=doors.SCENARIO, default=20.0,
          bounds=(0.1, 5000.0), units="mg/L", consequence="scenario",
          desc="Fully-mixed ultimate carbonaceous BOD at the top of the reach - "
               "the pollutant source-term question"),
    Param("water_temp_c", door=doors.SCENARIO, default=20.0, bounds=(0.0, 40.0),
          units="C", consequence="scenario",
          desc="Water temperature, which sets the DO saturation the deficit is "
               "measured against; 20 C is the standard Streeter-Phelps condition"),
    Param("do_standard_mgl", door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
          units="mg/L", consequence="scenario",
          desc="The DO water-quality standard the sag is judged against; 5 is a "
               "common warm-water aquatic-life criterion"),
    Param("k1_per_day", door=doors.SCENARIO, default=0.3, bounds=(0.01, 20.0),
          units="1/day", consequence="numerical",
          desc="CBOD deoxygenation rate - a documented rate coefficient"),
    Param("k2_per_day", door=doors.SCENARIO, default=0.9, bounds=(0.01, 50.0),
          units="1/day", consequence="numerical",
          desc="Surface reaeration rate - a documented rate coefficient"),
    Param("reach_length_km", door=doors.SCENARIO, default=12.0, bounds=(0.5, 15.0),
          units="km", consequence="aoi",
          desc="Modeled reach length downstream of the discharge; the sag critical "
               "point is often several km down"),

    Param("do_saturation_mgl", door=doors.DERIVED,
          resolve=f"{_STEPS}.water_quality.do_saturation_mgl",
          user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
          desc="DO saturation Cs; derived from water temperature unless supplied"),
    Param("upstream_do_mgl", door=doors.DERIVED,
          resolve=f"{_STEPS}.water_quality.upstream_do_mgl",
          user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
          desc="DO carried in at the top of the reach; derived as saturation unless supplied"),

    Param("channel_width_m", door=doors.CONSTANT, default=60.0, bounds=(1.0, 5000.0),
          units="m", consequence="numerical",
          desc="Modeled channel width, used for the mesh node estimate"),
    Param("sim_duration_s", door=doors.CONSTANT, default=10800.0,
          bounds=(60.0, 864000.0), units="s", consequence="numerical",
          desc="Simulated time to reach the steady-state sag"),
    Param("mesh_resolution", door=doors.CONSTANT, default="auto",
          consequence="numerical",
          desc="Mesh sizing mode: auto | fine | coarse"),
    Param("mesh_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(3.0, 5000.0), units="m", consequence="numerical",
          desc="Explicit target element edge length, overriding the sizing mode"),
    Param("bank_source", door=doors.CONSTANT, default="nhd_area",
          consequence="scenario",
          desc="Bank geometry source: nhd_area - the real mapped water polygon. An "
               "unmapped reach refuses; there is no assumed-width rung"),
    Param("output_interval_min", door=doors.USER, optional=True, bounds=(0.1, 1440.0),
          units="min", consequence="numerical",
          desc="Result-writing cadence; unset keeps the deck's own graphic period"),
    Param("discharge_m3s", door=doors.USER, optional=True, units="m^3/s",
          bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
          desc="Steady carrier discharge; unset resolves from the NOAA National "
               "Water Model at the reach"),
    Param("event_time", door=doors.QUESTION, optional=True, consequence="scenario",
          derived_when_absent=(
              "the carrier discharge is read at the MOST RECENT published NWM "
              "cycle"),
          desc="The storm/event moment to read the carrier discharge cycle at - "
               "from phrasing like 'during last Tuesday's storm'; an ISO date "
               "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
               "Unset reads the most recent published NWM cycle. The NWM PDS "
               "bucket retains only the last ~30 days of history; a deeper "
               "request refuses typed rather than silently reading a "
               "different cycle."),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


DOC = dict(
    summary="DISSOLVED-OXYGEN SAG below a discharge in a river (US TMDL / permit question).",
    routing=(
        "THE tool for \"where does dissolved oxygen bottom out below this discharge\", "
        "\"will the DO sag violate the standard\", \"Streeter-Phelps oxygen sag\", \"BOD "
        "loading / oxygen demand downstream of a WWTP / outfall\", \"DO TMDL for this "
        "reach\". Solves TELEMAC-2D + WAQTEL O2 over a REAL NHDPlus reach modeled "
        "STARTING at the fully-mixed discharge: the mixed carbonaceous BOD + DO enter "
        "at the top of the reach, CBOD decays downstream (deoxygenation k1) consuming "
        "oxygen, and surface reaeration (k2) recovers it. Produces a DISSOLVED-O2 field "
        "map + the along-reach DO-sag curve + the sag-minimum location/value. Supply a "
        "place `location` (geocoded) OR an explicit `bbox`."
    ),
    not_for=(
        "a conservative dye/tracer/contaminant plume that only dilutes "
        "(`telemac_river_dye`); groundwater plumes (`modflow_*`); flood depth "
        "(`sfincs_flood` / `hecras_riverine_flood`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and bank source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step. A run that completed is marked complete and is never "
         "replayed, so a fresh invocation always re-solves against live upstream "
         "data."),
    ),
    returns=(
        "On success a `TelemacDoLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the DISSOLVED-O2 field map and animates the SELAFIN sibling. It carries "
        "`do_min_mgl` / `do_min_distance_m` / `do_violates_standard` + `sag_curve_*`; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
