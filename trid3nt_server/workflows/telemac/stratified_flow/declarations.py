"""The CONTRACT of ``telemac3d_stratified_flow``: its declared params and its prose.

One file over from the recipe. ``stratified_flow.py`` reads on one page - the
question, the data, the plan, the answer, the chart - because the fourteen rows
that describe every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Param, doors

__all__ = ["DOC", "PARAMS"]


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Lake or basin place near the AOI (e.g. 'Lake Superior'), geocoded"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - deep open "
               "water inside a lake for the real-bathymetry path"),
    Param("flow_mode", door=doors.QUESTION, default="stratification",
          consequence="scenario",
          desc="Which 3D question: stratification (does the thermocline survive) | "
               "wind_circulation (surface downwind, return flow at depth) | "
               "salt_wedge (a density-driven bottom gravity current)"),

    # -- the column --------------------------------------------------------- #
    Param("warm_temp_c", door=doors.SCENARIO, default=25.0, bounds=(-2.0, 40.0),
          units="C", consequence="physics",
          desc="Epilimnion (warm surface layer) temperature - a PRESCRIBED demo "
               "column, since no met-forcing fetcher exists yet"),
    Param("cold_temp_c", door=doors.SCENARIO, default=15.0, bounds=(-2.0, 40.0),
          units="C", consequence="physics",
          desc="Hypolimnion (cold bottom layer) temperature; the initial "
               "top-to-bottom difference is what the run either keeps or mixes away"),
    Param("thermocline_depth_m", door=doors.SCENARIO, default=8.0,
          bounds=(0.5, 200.0), units="m", consequence="physics",
          desc="Depth of the thermocline below the surface"),
    Param("wind_speed_mps", door=doors.SCENARIO, default=0.0, bounds=(0.0, 40.0),
          units="m/s", consequence="physics",
          desc="Sustained wind speed; 0 is CALM - the half of the pair in which the "
               "thermocline persists - and a nonzero value mixes the column and "
               "drives the circulation"),
    Param("wind_direction_deg", door=doors.SCENARIO, default=270.0,
          bounds=(0.0, 360.0), units="deg", consequence="scenario",
          desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W)"),

    # -- the numerics (the advanced fold) ----------------------------------- #
    Param("nplan", door=doors.SCENARIO, default=13, bounds=(5.0, 30.0),
          type=int, consequence="numerical",
          desc="Number of vertical sigma levels - the degree of freedom a 2D model "
               "does not have, so it is the resolution lever that matters here"),
    Param("non_hydrostatic", door=doors.USER, optional=True, type=bool,
          consequence="physics",
          derived_when_absent="the hydrostatic solver runs",
          desc="Force the non-hydrostatic solver - the dam-break-3D fidelity rung a "
               "salt wedge's front needs"),
    Param("bathy_source", door=doors.SCENARIO, default="auto",
          consequence="physics",
          desc="Bed source: auto (a Great Lakes AOI samples the real NOAA lake-datum "
               "bathymetry, anywhere else runs the idealized basin) | noaa_greatlakes "
               "| idealized"),
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(50.0, 20000.0), units="m", consequence="numerical",
          derived_when_absent=(
              "the horizontal grid is laid at the labeled default spacing - 2000 m "
              "over a real lake, 250 m in the idealized basin"),
          desc="Explicit HORIZONTAL grid node spacing; the vertical is nplan"),
    Param("sim_duration_hours", door=doors.SCENARIO, default=5.0, bounds=(1.0, 24.0),
          units="h", consequence="numerical",
          desc="Simulated duration - long enough for the column to settle or mix"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a water body a 2D depth-averaged model cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"epilimnion over hypolimnion\", "
        "\"wind-driven vertical circulation / return flow in a lake\", \"surface-vs-"
        "bottom current structure\", \"salt wedge / salinity intrusion in an estuary\", "
        "\"density-driven bottom gravity current\". TELEMAC-3D with active-tracer "
        "baroclinic coupling over sigma layers. THREE question classes via "
        "`flow_mode`: `stratification` (default), `wind_circulation`, `salt_wedge`. "
        "Returns the SURFACE field map with a BOTTOM companion beside it and the "
        "vertical profile. Supply a lake `location` OR a `bbox`."
    ),
    not_for=(
        "a 2D river dye/contaminant plume (`telemac_river_dye`); inundation DEPTH "
        "(`sfincs_flood` / `geoclaw_inundation`); coastal storm-tide flooding "
        "(`coastal_tidal_surge`); the surface wave field (`tomawac_wave_field` / "
        "`artemis_harbor_agitation`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved column and wind for review/edit before '
         'the solve and WAITS; "auto" (session default) proceeds with every '
         "assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step."),
    ),
    returns=(
        "On success a `Telemac3dLayerURI` (a `LayerURI` subtype) - the SURFACE-layer "
        "field COG, with the BOTTOM companion emitted beside it. It carries "
        "`stratification_metric` / `stratification_dt` / `flow_mode` / `nplan` and "
        "the vertical `profile_*` arrays; narrate those typed numbers. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)
