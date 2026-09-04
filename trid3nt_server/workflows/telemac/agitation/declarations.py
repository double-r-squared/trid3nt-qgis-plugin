"""The CONTRACT of ``artemis_harbor_agitation``: its declared params and its prose.

One file over from the recipe. ``agitation.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the eleven rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DEFAULT_IDEALIZED_RES_M", "DEFAULT_REAL_RES_M", "DOC",
           "PARAMS"]

#: What an agitation run can be HANDED. ARTEMIS reads either mesh shape: the
#: default build is the uniform lattice the open-water run lays over the AOI, and
#: the BYO path - an adaptive triangulation with the breakwater cut in conformally
#: and a seaward boundary designated - is the proven one. Nothing is released into
#: a harbour agitation field, so there is no release row to write.
ACCEPTS = Accepts(mesh=("structured_grid", "unstructured_tri"))

#: The grid spacings a run is laid at when the caller names none. They live HERE,
#: beside the param whose ``derived_when_absent`` sentence promises them, because
#: a number in the step and a sentence in the contract are two sources of truth
#: for one fact and they drift. The step imports these.
DEFAULT_REAL_RES_M = 40.0
DEFAULT_IDEALIZED_RES_M = 8.0



class PARAMS:
    # -- the question ------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Harbour or coastal place near the AOI (e.g. 'Marquette, Michigan'), "
             "geocoded")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - the "
             "open-water harbour approach for the real-bathymetry path")
    wave_mode = Param(
        door=doors.QUESTION, default="diffraction",
        consequence="scenario",
        desc="Which agitation question: diffraction (a breakwater shelters the "
             "berths) | resonance (a narrow-mouth basin rings at its seiche "
             "periods) | shoal (a reef refracts and focuses waves)")

    # -- the incident wave -------------------------------------------------- #
    wave_period_s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(1.0, 300.0),
        units="s", consequence="physics",
        desc="Incident monochromatic wave period - a PRESCRIBED demo forcing, "
             "since no wave-forcing fetcher exists yet")
    wave_height_m = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
        units="m", consequence="physics",
        desc="Incident wave height H0 at the open boundary; Kd is measured "
             "against it, so it sets the scale of every narrated height")
    wave_direction_deg = Param(
        door=doors.SCENARIO, default=90.0,
        bounds=(0.0, 360.0), units="deg", consequence="scenario",
        desc="Incident wave direction in the TRIG convention (0 = +X east, "
             "90 = +Y north) - not the compass bearing")
    reflection_coef = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.0, 1.0),
        consequence="physics",
        desc="Structure / quay-wall reflection coefficient: 1 fully reflecting "
             "(a vertical quay), 0 fully absorbing (a rubble slope)")

    # -- the structure -----------------------------------------------------  #
    # NOT a Param. The thing that shelters is a CONTEXT SLOT (``DATA`` in
    # agitation.py): the template says it accepts a polyline and says nothing
    # about where one comes from, because naming a default source for somebody's
    # breakwater is an opinion the question does not carry.

    # -- the domain --------------------------------------------------------- #
    bathy_source = Param(
        door=doors.SCENARIO, default="auto",
        consequence="physics",
        desc="Bed source: auto (a Great Lakes DIFFRACTION AOI samples the real "
             "NOAA lake-datum bathymetry, everything else runs the analytic "
             "domain) | noaa_greatlakes | idealized")
    target_resolution_m = Param(
        door=doors.USER, optional=True, user_lever=True,
        bounds=(20.0, 2000.0), units="m", consequence="numerical",
        derived_when_absent=(
            f"the grid is laid at the labeled default spacing - "
            f"{DEFAULT_REAL_RES_M:g} m over a real harbour, "
            f"{DEFAULT_IDEALIZED_RES_M:g} m in the analytic domain"),
        desc="Explicit grid node spacing; a phase-resolving solve needs several "
             "nodes per WAVELENGTH, so this is much finer than a spectral run")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) inside a harbour or around a coastal structure.",
    routing=(
        "THE tool for \"how much does swell amplify inside this harbour\", \"wave "
        "agitation / tranquility in the basin\", \"does this breakwater shelter the "
        "berths\", \"harbour resonance / seiche\", \"diffraction behind a "
        "breakwater\", \"reef/shoal sheltering or focusing\". ARTEMIS "
        "phase-RESOLVING elliptic mild-slope (Berkhoff): diffraction fringes, "
        "standing waves and resonance are the answer, not an average. THREE "
        "classes via `wave_mode`: `diffraction` (default), `resonance`, `shoal`. "
        "Supply a harbour `location` or `bbox`. THE STRUCTURE IS A SLOT: pass "
        "`structure=` a breakwater layer (`fetch_osm_breakwaters`) or a drawn "
        "line; omit it and the domain solves as OPEN WATER, labeled."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth; coastal "
        "storm-tide flooding; a river plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("structure",
         "The barrier to mesh as a thin solid obstacle: a polyline LAYER (the uri "
         "or handle from fetch_osm_breakwaters, or any line layer the user has) or "
         "a drawn/typed line as [[lon, lat], ...]. Producer-less BY DESIGN - this "
         "tool will never go and find a structure you did not name. Absent = an "
         "open-water solve, and the run says so."),
        ("mesh",
         "The DOMAIN to solve on: a mesh `build_mesh` built (its uri or handle) - "
         "adaptive sizing, the structure cut in conformally, a seaward boundary "
         "designated. Supplied, it IS the domain and the grid lever stops "
         "describing anything the solve did; absent, the worker lays its uniform "
         "grid over the AOI and the run says which it ran on. An ANALYTIC "
         "wave_mode (resonance, shoal) refuses a mesh - its geometry is the "
         "physics."),
        ("input_mode",
         '"user_gated" presents the resolved incident wave and the structure for '
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
        "On success an `ArtemisAgitationLayerURI` (a `LayerURI` subtype) - the emitter "
        "loads the Kd COG and animates the ARTEMIS SELAFIN sibling. It carries "
        "`kd_max` / `kd_sheltered` / `kd_exposed` / `resonant_period_s` / "
        "`response_at_resonance` / `wave_mode`; narrate those typed numbers. On "
        "failure a dict with `status=\"error\"` + `error_code`."
    ),
)
