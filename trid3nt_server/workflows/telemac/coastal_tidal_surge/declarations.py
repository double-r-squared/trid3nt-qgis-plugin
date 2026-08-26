"""The CONTRACT of ``coastal_tidal_surge``: its declared params and its prose.

One file over from the recipe. ``coastal_tidal_surge.py`` reads on one page - the
question, the data, the plan, the answer, the chart - because the fourteen rows
that describe every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Param, Validity, doors

__all__ = ["DEFAULT_GRID_SPACING_M", "DOC", "FRICTION_LAW_NAMES", "PARAMS",
           "SYNTHETIC_WINDOW_HOURS", "VALIDITY"]


#: The grid spacing a coastal run is laid at when nobody names one. It lives HERE,
#: beside the param whose ``derived_when_absent`` describes it, because the number
#: and the sentence that promises it have to be one thing.
DEFAULT_GRID_SPACING_M = 180.0

#: The window a SYNTHETIC plane-beach run simulates. A real run takes its window
#: from the gauge series it was handed; the analytic bed has no series, so this is
#: the only rung left - about two full tidal cycles. Declared rather than sitting
#: in the deck writer, because a silent third rung is a window nobody chose.
SYNTHETIC_WINDOW_HOURS = 30.0


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Coastal place near the AOI, geocoded to a shoreline-spanning extent"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 spanning the "
               "shoreline - open water on one side, low land on the other"),
    Param("series_type", door=doors.QUESTION, default="observed",
          consequence="scenario",
          desc="Which water-level record drives the boundary: observed (the storm-surge "
               "record) | prediction (the astronomical tide, the calm-tide control that "
               "isolates the surge)"),

    # -- the gauge series --------------------------------------------------- #
    Param("station", door=doors.USER, optional=True, consequence="physics",
          derived_when_absent=(
              "the CO-OPS station nearest the AOI centre drives the boundary"),
          desc="NOAA CO-OPS station id (e.g. '8728690'); unset uses the nearest "
               "in-AOI gauge"),
    Param("start_date", door=doors.QUESTION, optional=True, consequence="scenario",
          desc="ISO YYYY-MM-DD start of the gauge window - the storm the question "
               "is about"),
    Param("end_date", door=doors.QUESTION, optional=True, consequence="scenario",
          desc="ISO YYYY-MM-DD end of the gauge window"),
    Param("datum_offset_m", door=doors.SCENARIO, optional=True,
          bounds=(-10.0, 10.0), units="m", consequence="physics",
          derived_when_absent="the gauge's OWN published tidal-to-geodetic offset "
                              "reconciles the series with the DEM_all bed",
          desc="Metres ADDED to every series value to reconcile the tide datum (MLLW) "
               "with the bed datum (DEM_all over US coasts is served NAVD 88); "
               "supplying 0 is an explicit override that leaves them unreconciled"),

    # -- the domain --------------------------------------------------------- #
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(20.0, 5000.0), units="m", consequence="numerical",
          derived_when_absent=(f"the grid is laid at the labeled "
                               f"{DEFAULT_GRID_SPACING_M:g} m default spacing"),
          desc="Explicit grid node spacing; the coastal grid floor is 20 m and a wide "
               "AOI is coarsened under the node budget"),
    Param("ocean_edge", door=doors.USER, optional=True, consequence="numerical",
          derived_when_absent=(
              "the seaward boundary is placed on the DEEPEST-mean bbox edge"),
          desc="Which bbox edge carries the seaward liquid boundary: N | S | E | W; "
               "unset picks the deepest edge"),
    Param("bathy_source", door=doors.SCENARIO, default="noaa_demall",
          consequence="physics",
          desc="Bed source: noaa_demall (real topobathy) | synthetic (an analytic "
               "plane beach - the deterministic offline path, not a real coast)"),

    # -- the bed and the air (worker knobs, now reachable) ------------------ #
    # These four were CoastalConfig fields the deck writer never filled, so the
    # image's own defaults were the only values a coastal run could ever have.
    # The declared defaults reproduce them exactly; what changes is that they are
    # now on the form, in provenance, and tunable without an image rebuild.
    Param("friction_law", door=doors.CONSTANT, default=3, type=int,
          bounds=(1.0, 7.0), consequence="physics",
          desc="TELEMAC bottom-friction law: 2 = Chezy, 3 = Strickler (default), "
               "4 = Manning. The law fixes what friction_coefficient MEANS"),
    # The declared band spans all three laws because the coefficient's plausible
    # range is LAW-dependent and a Param bound is about one value: a Manning n
    # (0.011-0.1) and a Strickler Ks (15-90) cannot share a band. The floor is
    # the only law-independent physical fact - a friction coefficient is
    # positive. Which side of the crossover the value belongs on is the coupled
    # rule below, not a bound.
    Param("friction_coefficient", door=doors.SCENARIO, default=40.0,
          bounds=(0.001, 200.0), units="m^(1/3)/s (Strickler Ks) or s/m^(1/3) "
                                       "(Manning n)",
          consequence="physics",
          desc="Bed-friction coefficient, read under friction_law: Strickler/Chezy "
               "Ks ~15-90 (higher is SMOOTHER; ~40 is mixed sand and marsh) or "
               "Manning n ~0.011-0.1 (higher is ROUGHER; n = 1/Ks)"),
    Param("wind_speed_mps", door=doors.SCENARIO, default=0.0, bounds=(0.0, 80.0),
          units="m/s", consequence="physics",
          desc="Constant wind over the domain, which adds local set-up on top of "
               "the boundary series; 0 runs with no wind block at all"),
    Param("wind_direction_from_deg", door=doors.SCENARIO, default=0.0,
          bounds=(0.0, 360.0), units="deg", consequence="physics",
          desc="Compass direction the wind blows FROM; only read when "
               "wind_speed_mps is above zero"),

    # -- numerics (the advanced fold) --------------------------------------- #
    Param("duration_hours", door=doors.USER, optional=True, bounds=(0.1, 720.0),
          units="h", consequence="numerical",
          derived_when_absent=(
              "the simulated window is the fetched series' OWN span; a synthetic "
              "plane-beach run, which has no series, takes the labeled "
              f"{SYNTHETIC_WINDOW_HOURS:g} h window"),
          desc="Simulated window; unset runs the whole gauge series"),
    Param("time_step_s", door=doors.CONSTANT, default=20.0, bounds=(1.0, 600.0),
          units="s", consequence="numerical", desc="Solver time step"),
    Param("output_interval_min", door=doors.USER, optional=True, bounds=(0.1, 1440.0),
          units="min", consequence="numerical",
          desc="Result-writing cadence; unset keeps the deck's own graphic period"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: TELEMAC's LAW OF BOTTOM FRICTION ids, as the coefficient's own meaning.
FRICTION_LAW_NAMES: dict[int, str] = {2: "Chezy", 3: "Strickler", 4: "Manning"}

#: Where a Strickler/Chezy coefficient stops and a Manning one begins. The two
#: are RECIPROCALS (n = 1/Ks), so the plausible bands sit either side of 1: a
#: rough floodplain is Ks ~10 and n ~0.1, and nothing physical lands on both
#: sides. Comparing against the crossover rather than against each law's band is
#: deliberate - an ATYPICAL value the caller means (a glass-smooth Ks of 120) is
#: theirs to set, while a value on the wrong side of the crossover is not
#: atypical, it is the other quantity.
_FRICTION_CROSSOVER = 1.0


def _friction_matches_law(v) -> bool:  # noqa: ANN001 - a ParamValues view
    """Is the coefficient the quantity this law reads it as?

    The classic TELEMAC error: switch LAW OF BOTTOM FRICTION to Manning and leave
    FRICTION COEFFICIENT at a Strickler 40. The deck is valid, the solve runs, and
    it models a bed roughness three orders of magnitude off what was asked for.
    """
    if int(v.friction_law) not in FRICTION_LAW_NAMES:
        return True             # a law this rule says nothing about
    manning = int(v.friction_law) == 4
    return (float(v.friction_coefficient) < _FRICTION_CROSSOVER) is manning


#: The coupled rules this template's sheet has to satisfy - see workflows/lib/validity.py.
VALIDITY = (
    Validity(
        name="friction_coefficient_matches_law",
        reads=("friction_law", "friction_coefficient"),
        holds=_friction_matches_law,
        message=(
            "friction_law={friction_law} with friction_coefficient="
            "{friction_coefficient} reads the coefficient as the WRONG quantity. "
            "Laws 2 and 3 (Chezy, Strickler) take a Ks around 15-90 where higher "
            "is smoother; law 4 (Manning) takes an n around 0.011-0.1 where "
            "higher is rougher, and the two are reciprocals (n = 1/Ks). This "
            "pair sits on the wrong side of that crossover, so the run would "
            "model a bed roughness nobody asked for. Re-confirm the coefficient "
            "in the units the law you chose reads - naming BOTH values is the "
            "only way past this."
        ),
    ),
)


DOC = dict(
    summary="How far an OBSERVED or PREDICTED coastal water-level series FLOODS this coast.",
    routing=(
        "THE tool for \"how far does the storm surge flood inland\", \"map the coastal "
        "inundation from this tide-gauge record\", \"which low land does the storm tide "
        "reach\", \"surge vs calm-tide flooded area at this coast\". TELEMAC-2D shallow "
        "water with TIDAL FLATS wetting/drying over real NOAA DEM_all topobathy, one "
        "seaward liquid boundary driven in time by a NOAA CO-OPS series. TWO question "
        "classes via `series_type`: `observed` (the storm-surge record floods the low "
        "coast) and `prediction` (the astronomical tide over the SAME domain - the "
        "control isolating the surge). Produces a peak-inundation-DEPTH map + the "
        "newly-flooded land area. Supply a coastal `location` OR a `bbox` spanning "
        "the shoreline."
    ),
    not_for=(
        "a spectral WAVE-HEIGHT field (`tomawac_wave_field`); harbour agitation "
        "(`artemis_harbor_agitation`); a river dye/contaminant plume "
        "(`telemac_river_dye`); regional compound-flood screening (`sfincs_flood`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved window, station and datum offset for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step. A run that completed is marked complete and is never "
         "replayed, so a fresh invocation always re-solves against live upstream data."),
    ),
    returns=(
        "On success a `TelemacCoastalLayerURI` (a `LayerURI` subtype) - the emitter "
        "loads the peak-inundation-depth COG and animates the coastal SELAFIN sibling. "
        "It carries `peak_depth_m` / `flooded_land_km2` / `wet_area_km2` / `sl_peak_m` "
        "/ `series_type`; narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
