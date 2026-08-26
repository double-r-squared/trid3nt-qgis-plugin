"""The CONTRACT of ``artemis_harbor_agitation``: its declared params and its prose.

One file over from the recipe. ``agitation.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the eleven rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Param, doors

__all__ = ["DOC", "PARAMS"]


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Harbour or coastal place near the AOI (e.g. 'Marquette, Michigan'), "
               "geocoded"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - the "
               "open-water harbour approach for the real-bathymetry path"),
    Param("wave_mode", door=doors.QUESTION, default="diffraction",
          consequence="scenario",
          desc="Which agitation question: diffraction (a breakwater shelters the "
               "berths) | resonance (a narrow-mouth basin rings at its seiche "
               "periods) | shoal (a reef refracts and focuses waves)"),

    # -- the incident wave -------------------------------------------------- #
    Param("wave_period_s", door=doors.SCENARIO, default=8.0, bounds=(1.0, 300.0),
          units="s", consequence="physics",
          desc="Incident monochromatic wave period - a PRESCRIBED demo forcing, "
               "since no wave-forcing fetcher exists yet"),
    Param("wave_height_m", door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
          units="m", consequence="physics",
          desc="Incident wave height H0 at the open boundary; Kd is measured "
               "against it, so it sets the scale of every narrated height"),
    Param("wave_direction_deg", door=doors.SCENARIO, default=90.0,
          bounds=(0.0, 360.0), units="deg", consequence="scenario",
          desc="Incident wave direction in the TRIG convention (0 = +X east, "
               "90 = +Y north) - not the compass bearing"),
    Param("reflection_coef", door=doors.SCENARIO, default=1.0, bounds=(0.0, 1.0),
          consequence="physics",
          desc="Structure / quay-wall reflection coefficient: 1 fully reflecting "
               "(a vertical quay), 0 fully absorbing (a rubble slope)"),

    # -- the structure ------------------------------------------------------ #
    Param("breakwater", door=doors.USER, optional=True, consequence="scenario",
          type=tuple[float, float, float, float] | list[float],
          derived_when_absent=(
              "the ACTUAL surveyed breakwater is fetched from OpenStreetMap and "
              "meshed as a thin solid barrier; if OSM has none, a LABELED "
              "schematic segment stands in and says so"),
          desc="Pin the barrier as a segment (lon0, lat0, lon1, lat1) EPSG:4326; "
               "supplying it suppresses the OSM lookup"),

    # -- the domain --------------------------------------------------------- #
    Param("bathy_source", door=doors.SCENARIO, default="auto",
          consequence="physics",
          desc="Bed source: auto (a Great Lakes DIFFRACTION AOI samples the real "
               "NOAA lake-datum bathymetry, everything else runs the analytic "
               "domain) | noaa_greatlakes | idealized"),
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(20.0, 2000.0), units="m", consequence="numerical",
          derived_when_absent=(
              "the grid is laid at the labeled default spacing - 40 m over a real "
              "harbour, 8 m in the analytic domain"),
          desc="Explicit grid node spacing; a phase-resolving solve needs several "
               "nodes per WAVELENGTH, so this is much finer than a spectral run"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) inside a harbour or around a coastal structure.",
    routing=(
        "THE tool for \"how much does swell amplify inside this harbour\", \"wave "
        "agitation / tranquility in the basin\", \"does this breakwater shelter the "
        "berths\", \"harbour resonance / seiche\", \"diffraction behind a breakwater\", "
        "\"reef/shoal wave sheltering or focusing\". ARTEMIS phase-RESOLVING elliptic "
        "mild-slope (Berkhoff) - diffraction fringes, standing waves and resonance are "
        "the answer, not an average. THREE question classes via `wave_mode`: "
        "`diffraction` (default; on a real Great Lakes harbour the ACTUAL surveyed "
        "OSM breakwater is meshed), `resonance`, `shoal`. Returns a dimensionless "
        "agitation field. Supply a harbour `location` OR a `bbox`."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth "
        "(`tomawac_wave_field`, the phase-averaged tier); coastal storm-tide "
        "flooding (`coastal_tidal_surge`); inundation DEPTH (`sfincs_flood`); a "
        "river plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved incident wave and the structure for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step."),
    ),
    returns=(
        "On success an `ArtemisAgitationLayerURI` (a `LayerURI` subtype) - the emitter "
        "loads the Kd COG and animates the ARTEMIS SELAFIN sibling. It carries "
        "`kd_max` / `kd_sheltered` / `kd_exposed` / `resonant_period_s` / "
        "`response_at_resonance` / `wave_mode`; narrate those typed numbers. On "
        "failure a dict with `status=\"error\"` + `error_code`."
    ),
)
