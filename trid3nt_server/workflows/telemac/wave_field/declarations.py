"""The CONTRACT of ``tomawac_wave_field``: its declared params and its prose.

One file over from the recipe. ``wave_field.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the thirteen rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Param, doors

__all__ = ["DEFAULT_IDEALIZED_RES_M", "DEFAULT_REAL_RES_M", "DOC", "PARAMS"]

#: The grid spacings a run is laid at when the caller names none. They live HERE,
#: beside the param whose ``derived_when_absent`` sentence promises them, because
#: a number in the step and a sentence in the contract are two sources of truth
#: for one fact and they drift. The step imports these.
DEFAULT_REAL_RES_M = 2000.0
DEFAULT_IDEALIZED_RES_M = 1500.0



class PARAMS:
    # -- the question ------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Lake or coastal place near the AOI (e.g. 'Lake Superior'), geocoded")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - open water "
             "inside a lake for the real-bathymetry path")
    wave_mode = Param(
        door=doors.QUESTION, default="fetch_growth",
        consequence="scenario",
        desc="Which wave question: fetch_growth (wind-wave growth across the fetch) "
             "| shoaling (swell steepens and depth-breaks) | bottom_friction (a "
             "shallow shelf dissipates energy) | wave_current (a current amplifies "
             "or damps the swell)")

    # -- the storm ---------------------------------------------------------- #
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=20.0, bounds=(0.0, 60.0),
        units="m/s", consequence="physics",
        desc="Sustained storm wind speed - a PRESCRIBED demo forcing, since no "
             "wave-forcing fetcher exists yet")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=270.0,
        bounds=(0.0, 360.0), units="deg", consequence="physics",
        desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W); the fetch "
             "runs downwind of it")
    boundary_hs_m = Param(
        door=doors.SCENARIO, default=1.5, bounds=(0.0, 20.0),
        units="m", consequence="scenario",
        desc="Incident swell significant wave height at the open boundary - the "
             "shoaling and wave_current question classes")
    boundary_period_s = Param(
        door=doors.SCENARIO, default=10.0, bounds=(1.0, 30.0),
        units="s", consequence="scenario",
        desc="Incident swell peak period")
    current_speed_mps = Param(
        door=doors.SCENARIO, default=-2.5,
        bounds=(-10.0, 10.0), units="m/s", consequence="scenario",
        desc="wave_current only - the current ramped across the domain; NEGATIVE "
             "opposes the swell (amplifies Hs), POSITIVE follows it (damps it)")
    bottom_friction = Param(
        door=doors.USER, optional=True, type=bool,
        consequence="physics",
        derived_when_absent=(
            "bottom-friction dissipation arms itself for the bottom_friction "
            "question class and stays off for every other one"),
        desc="Force bottom-friction dissipation on or off")

    # -- the domain --------------------------------------------------------- #
    bathy_source = Param(
        door=doors.SCENARIO, default="auto",
        consequence="physics",
        desc="Bed source: auto (a Great Lakes AOI samples the real NOAA lake-datum "
             "bathymetry, anywhere else runs the idealized basin) | noaa_greatlakes "
             "| idealized")
    target_resolution_m = Param(
        door=doors.USER, optional=True, user_lever=True,
        bounds=(150.0, 20000.0), units="m", consequence="numerical",
        derived_when_absent=(
            f"the grid is laid at the labeled default spacing - "
            f"{DEFAULT_REAL_RES_M:g} m over a real lake, "
            f"{DEFAULT_IDEALIZED_RES_M:g} m in the idealized basin"),
        desc="Explicit grid node spacing; 150 m is the finest the wave grid authors "
             "and a large lake is coarsened under the node budget")

    # -- numerics (the advanced fold) --------------------------------------- #
    # CONSTANT, not SCENARIO: this window is not a choice about the storm, it is
    # how long the sea needs to STOP CHANGING. The answer is the fetch-limited
    # steady state, so a shorter window reports an unconverged sea and a longer
    # one reports the same numbers more slowly - neither is a different question.
    # The user keeps the lever; the model is not offered it.
    sim_duration_hours = Param(
        door=doors.CONSTANT, default=4.0, bounds=(1.0, 24.0),
        units="h", consequence="numerical",
        desc="Simulated storm duration - long enough for the sea to reach its "
             "fetch-limited steady state")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


DOC = dict(
    summary="The SPECTRAL WAVE FIELD (significant wave height Hs) a storm builds over a lake or coast.",
    routing=(
        "THE tool for \"how big do the waves get\", \"significant wave height\", \"wave "
        "field / sea state\", \"fetch-limited wave growth across the lake\", \"swell "
        "shoaling / breaking at the beach\", \"wave-current interaction\", \"wave energy "
        "dissipation on a shallow shelf\". TOMAWAC third-generation spectral wave-action "
        "solver - wind-wave generation (WAM cycle 4), shoaling/breaking, wave-current "
        "interaction, bottom friction. FOUR question classes via `wave_mode`: "
        "`fetch_growth` (default), `shoaling`, `bottom_friction`, `wave_current`. "
        "Produces an Hs field map + the along-fetch growth curve + the upwind/downwind "
        "shore pair. Supply a lake/coastal `location` OR a `bbox`."
    ),
    not_for=(
        "inundation DEPTH (`sfincs_flood` / `geoclaw_inundation`); coastal storm-tide "
        "flooding (`coastal_tidal_surge`); harbour agitation inside a breakwater "
        "(`artemis_harbor_agitation`); a river plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved storm forcing and bed source for '
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
        "On success a `TelemacWaveLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the Hs COG and animates the TOMAWAC SELAFIN sibling. It carries `hs_max_m` / "
        "`hs_mean_m` / `hs_upwind_m` / `hs_downwind_m` / `wave_mode`; narrate those "
        "typed numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
