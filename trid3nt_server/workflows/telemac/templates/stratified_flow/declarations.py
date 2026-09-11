"""The CONTRACT of ``telemac3d_stratified_flow``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Param, doors

__all__ = ["BASIN_HALF_DEG", "DEFAULT_MIN_EDGE_M", "DOC", "PARAMS"]

#: A lake basin runs wider than it is tall in degrees, so a geocoded place is
#: squared off asymmetrically (~0.06 deg of longitude, ~0.04 of latitude). A 3D
#: solve is NPOIN2 x NPLAN, so the box is the one a screening column can afford
#: rather than the whole lake.
BASIN_HALF_DEG = (0.06, 0.04)

#: The triangle edge a basin interior is meshed at when the ask states none. A
#: vertical-structure question is resolution-bound in the VERTICAL - the planes -
#: rather than in the horizontal, so the horizontal spends its budget on covering
#: the basin.
DEFAULT_MIN_EDGE_M: float = 120.0


class PARAMS:
    # -- the question ------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Lake or basin place near the AOI (e.g. 'Marquette, Michigan'), "
             "geocoded")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - the "
             "stretch of the water body the column is solved over")

    # -- the column --------------------------------------------------------- #
    warm_temp_c = Param(
        door=doors.SCENARIO, default=25.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Epilimnion (warm surface layer) temperature - a PRESCRIBED demo "
             "column, since no met-forcing fetcher exists yet")
    cold_temp_c = Param(
        door=doors.SCENARIO, default=15.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Hypolimnion (cold bottom layer) temperature; the initial "
             "top-to-bottom difference is what the run either keeps or mixes away")
    thermocline_depth_m = Param(
        door=doors.SCENARIO, default=8.0,
        bounds=(0.5, 200.0), units="m", consequence="physics",
        desc="Depth of the thermocline below the surface. The vertical grid is "
             "planned to HOLD it and REFUSES when no admissible sigma stretch "
             "over the basin's deepest column can")
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 40.0),
        units="m/s", consequence="physics",
        desc="Sustained wind speed; 0 is CALM - the half of the pair in which the "
             "thermocline persists - and a nonzero value both mixes the column "
             "and drives the surface-downwind / return-flow-at-depth circulation "
             "reported beside the temperature")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=270.0,
        bounds=(0.0, 360.0), units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W)")

    # -- the domain --------------------------------------------------------- #
    levels = Param(
        door=doors.SCENARIO, default=13, bounds=(5.0, 30.0),
        type=int, user_lever=True, consequence="numerical",
        desc="Number of vertical sigma levels - the degree of freedom a 2D model "
             "does not have, so it is THE resolution lever here; too few for the "
             "declared thermocline is a refusal, not a coarser answer")
    mesh_min_edge_m = Param(
        door=doors.SCENARIO, default=DEFAULT_MIN_EDGE_M,
        bounds=(20.0, 5000.0), units="m", consequence="numerical",
        desc="Triangle edge the basin interior is meshed at; the 3D node count is "
             "this mesh's nodes times the sigma levels")

    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the lake level is the most recent reading the gauge has published "
            "today"),
        desc="The day the basin's OBSERVED lake level is read at the nearest "
             "CO-OPS gauge - from phrasing like 'during last Tuesday's blow'; "
             "an ISO date (e.g. '2026-09-05'). The run opens at the level that "
             "day closed on, so this is what makes a past event replayable")

    # -- the window (the advanced fold) ------------------------------------- #
    # CONSTANT, not SCENARIO: the window is a settling time, not a scenario. The
    # answer is the column's SETTLED state, so this is "long enough". The user
    # keeps the lever.
    sim_duration_hours = Param(
        door=doors.CONSTANT, default=5.0, bounds=(1.0, 24.0),
        units="h", consequence="numerical",
        desc="Simulated duration - long enough for the column to settle or mix")
    time_step_s = Param(
        door=doors.USER, optional=True, bounds=(0.2, 300.0),
        units="s", consequence="numerical", user_lever=True,
        derived_when_absent=(
            "the step follows the edge the ACCEPTED mesh was BUILT at, through "
            "the same CFL producer the river reach's step comes from - a step "
            "asserted independently of the mesh is a stability claim about a "
            "domain nobody measured"),
        desc="Solver time step; unset derives it from the accepted mesh")
    tracer_advection_scheme = Param(
        door=doors.CONSTANT, default=13, type=int, user_lever=True,
        consequence="numerical",
        desc="SCHEME FOR ADVECTION OF TRACERS - the NERD family (13, 14), which "
             "is the distributive scheme the iteration ceiling below governs and "
             "the only one that is monotone across a thermocline. 13 is the "
             "value the engine's own telemac2d dictionary defaults this same "
             "keyword to; the dictionary gives the 3D one no default at all, so "
             "unstated it falls back to the VELOCITIES scheme (5, MURD PSI), "
             "which is what stopped a baroclinic basin at its first tracer step")
    max_advection_iterations = Param(
        door=doors.CONSTANT, default=50, type=int, user_lever=True,
        consequence="numerical",
        desc="MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES - the ceiling "
             "the distributive schemes 13 and 14 sub-iterate under. Stated "
             "rather than defaulted because it is the number a run that stops on "
             "'ITERATION NO. REACHED' is bounded by, and a reader of the deck "
             "cannot see a ceiling the deck does not write")
    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        derived_when_absent="the deck writes a frame every solver step block",
        desc="Result-writing cadence; the profile is read off the last frame, so "
             "this decides how much of the column's history is animatable")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a water body a 2D depth-averaged model "
            "cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"epilimnion over hypolimnion\", "
        "\"wind-driven vertical circulation / return flow in a lake\", "
        "\"surface-vs-bottom current structure\". TELEMAC-3D baroclinic coupling "
        "over sigma layers, on a mesh cut from the water body's own polygon - a "
        "CLOSED basin, which is what a lake is. ONE run answers both halves: the "
        "temperature difference that SURVIVES, and the surface-downwind / "
        "return-flow-at-depth velocities the same wind drives. Returns the "
        "SURFACE map with a BOTTOM companion and the vertical profile. Supply a "
        "lake `location` OR a `bbox`."
    ),
    not_for=(
        "a 2D river dye/contaminant plume (`telemac_river_dye`); inundation "
        "DEPTH; coastal storm-tide flooding; harbour wave agitation "
        "(`artemis_harbor_agitation`); a salinity intrusion up an estuary, which "
        "needs a tidal liquid boundary this closed basin does not carry"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved column, the wind and the authored '
         'mesh for review/edit before the solve and WAITS; "auto" (session '
         "default) proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success an `AnswerLayerURI` - the SURFACE-plane water temperature "
        "COG the run leads with, the BOTTOM-plane companion beside it on the same "
        "scale, and the column at the deepest node charted against the "
        "prescribed initial column. Its `answer` carries `stratification_dt` "
        "(the surviving top-to-bottom difference) against `stratification_dt_init`, "
        "the depth-weighted `column_mean_final_c` / `column_mean_init_c` whose "
        "drift is the numerical error bar on the mixing, `column_depth_m`, and the "
        "`u_surface` / `u_bottom` / `depth_avg_u` triple the same wind drove; "
        "narrate those typed numbers. The run exchanges NO heat with the "
        "atmosphere, so a falling surface temperature is downward MIXING and never "
        "the lake cooling - narrate it that way. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
