"""The CONTRACT of ``telemac_rain_on_grid``: what only a runoff question asks.

The domain, its bed, its boundary runs and the granularity are the runtime's own
slots and levers; the deck's friction, its clock and its cadence are keywords the
module's dictionary describes. Every number below is this question's own."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Param, doors, lever

__all__ = [
    "DOC",
    "LANDCOVER_CN_MANNING",
    "LANDCOVER_UNMAPPED",
    "NLCD_NATIVE_RESOLUTION_M",
    "PARAMS",
]


#: NLCD's own grid. Land cover is a CATEGORICAL raster, so asking for any other
#: spacing resamples class labels - which is the one resampling the temporal and
#: spatial doctrine refuses outright. Declared as a constant rather than a knob
#: because there is no honest value other than the product's native one.
NLCD_NATIVE_RESOLUTION_M: int = 30

#: The infiltration surface, per NLCD class: the curve number (AMC II, the mid
#: hydrologic soil group) and the Manning n the rain-on-grid study of Godara,
#: Bruland and Alfredsen (2024, Front. Water 6:1384205) tabulates per land-cover
#: class, mapped onto the NLCD legend. ONE STUDY, BOTH COLUMNS: the curve number
#: and the roughness are read off the same table for the same class, so a run's
#: infiltration and its friction are parameterised together. Substituting
#: another published roughness table - one that agrees with this on none of the
#: shared codes - would mix two calibrations in one field and move every number
#: this template has produced. That is an author's declared choice, never a shim:
#: swap the pair, or carry both as a declared lever, but never half of one. The
#: true curve number depends on the soil group and is a calibration lever.
#:
#: nlcd_code -> (curve_number_amc2, manning_n, class_label)
LANDCOVER_CN_MANNING: dict[int, tuple[float, float, str]] = {
    11: (100.0, 0.040, "river/open-water"),
    12: (100.0, 0.040, "river/open-water"),
    21: (75.0, 0.050, "open-land"),
    22: (89.0, 0.100, "urban"),
    23: (89.0, 0.100, "urban"),
    24: (89.0, 0.100, "urban"),
    31: (85.0, 0.020, "bare-rock/scarce-veg"),
    41: (80.0, 0.200, "forest"),
    42: (80.0, 0.200, "forest"),
    43: (80.0, 0.200, "forest"),
    51: (75.0, 0.050, "open-land"),
    52: (75.0, 0.050, "open-land"),
    71: (75.0, 0.050, "open-land"),
    72: (75.0, 0.050, "open-land"),
    81: (80.0, 0.050, "open-land"),
    82: (80.0, 0.050, "open-land"),
    90: (90.0, 0.200, "marsh"),
    95: (90.0, 0.200, "marsh"),
}

#: The row a class outside the table takes: the open-land one. Never silently a
#: low curve number, which over-produces runoff, nor 100, which zeroes
#: infiltration.
LANDCOVER_UNMAPPED: tuple[float, float, str] = (75.0, 0.050, "open-land")


class PARAMS:
    """What only a rainfall-runoff question asks: where the water leaves, the
    storm that falls, and how much of it the ground takes."""

    # -- the question ------------------------------------------------------- #
    pour_point = Param(
        door=doors.USER, consequence="scenario", type=Point,
        desc="The catchment OUTLET, as a Point: the pick's {coordinates, name} "
             "verbatim, a (lon, lat) pair, 'lat,lon', a point layer, or a place "
             "name - the point the runoff drains to. It decides which basin is "
             "modelled at all, so it is asked for (picked on the canvas or passed "
             "explicitly) and NEVER invented. It is snapped onto the traced "
             "channel, so a click beside the stream still delineates its basin")

    # -- the storm ---------------------------------------------------------- #
    rain_series_mm = Param(
        door=doors.USER, optional=True, type=list[float] | None,
        units="mm", consequence="physics",
        derived_when_absent=(
            "the constant design storm below drives the run, labeled as the "
            "hypothetical it is"),
        desc="A MEASURED storm, as hourly GROSS millimetres in the order the "
             "record reported them - what fetch_aorc_precip returns over this "
             "catchment under `precip_mm` for any CONUS window since 1979. It is "
             "the true intensity structure, which is what resolves the hydrograph "
             "SHAPE; a record that stops inside the simulated window stops in the "
             "run too, so the recession limb appears")
    rain_start_date = Param(
        door=doors.USER, optional=True, type=str | None,
        consequence="scenario",
        derived_when_absent=(
            "no record is looked for, and the stated series or the design storm "
            "drives the run"),
        desc="First day of the MEASURED storm to look for over this catchment, "
             "ISO yyyy-mm-dd. With rain_end_date it reads the hourly analysis of "
             "record; a catchment with no published hours keeps the design storm "
             "and the run says so")
    rain_end_date = Param(
        door=doors.USER, optional=True, type=str | None,
        consequence="scenario",
        derived_when_absent=(
            "no record is looked for, and the stated series or the design storm "
            "drives the run"),
        desc="Last day of the measured storm window, ISO yyyy-mm-dd. At most 92 "
             "days after rain_start_date, and no later than about ten days ago - "
             "the record is an analysis, not a forecast")
    design_storm_mm_per_hr = Param(
        door=doors.SCENARIO, default=25.0,
        bounds=(0.1, 500.0), units="mm/h", consequence="physics",
        desc="Constant design-storm intensity, used when no measured series is "
             "given. A hypothetical storm, labeled as one")
    storm_duration_hr = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.1, 240.0),
        units="h", consequence="scenario",
        desc="How long the design storm rains for; a window shorter than the "
             "simulated one is what lets the recession limb appear")
    # The simulated window is a SCENARIO choice, not a numerics fact. Unlike the
    # wave and 3D windows (CONSTANT: "long enough to reach steady state") and
    # unlike the coastal window (USER: the gauge record defines it), how long you
    # watch a catchment respond decides whether the hydrograph carries its peak
    # and how much of the recession - which is part of the question being asked.
    sim_duration_s = lever(
        "sim_duration_s", default=43200.0, bounds=(360.0, 2.592e6),
        desc="Total simulated window; longer than the rain, to watch the "
             "recession. A window that closes while the discharge is still "
             "rising is reported as such and its peak is a LOWER BOUND")

    # -- infiltration ------------------------------------------------------- #
    antecedent_moisture = Param(
        door=doors.SCENARIO, default="normal",
        consequence="physics",
        desc="How wet the catchment already is: dry (SCS AMC I) | normal (AMC II) | "
             "wet (AMC III). The dominant infiltration lever - a wet basin absorbs "
             "far less and the hydrograph peaks higher and sooner")
    curve_number = Param(
        door=doors.USER, optional=True, bounds=(30.0, 100.0),
        consequence="physics",
        derived_when_absent=(
            "curve numbers are distributed PER NODE from the land-cover raster"),
        desc="A UNIFORM SCS curve number over the whole catchment, overriding the "
             "land-cover-distributed field. Roughness stays per-node either way - "
             "Manning n is a separate physical property, not the CN knob")
    steep_slope_correction = Param(
        door=doors.SCENARIO, default=False,
        consequence="physics",
        desc="Apply the Huang (2006) steep-slope correction to the curve numbers "
             "using the mesh's own bed gradients. The engine's native branch is "
             "compiled off in the installed 9.0.0 build, so the correction is "
             "applied to the CN field before it is written")

    # -- the granularity band ----------------------------------------------- #
    # The runtime declares mesh_resolution_m for every template; a catchment
    # triangulates a BAND rather than one edge, so this row states the band's
    # fine end at the default a hillslope basin is screened at and the row below
    # states its coarse end.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=40.0, bounds=(5.0, 500.0),
        desc="Finest triangle edge, reached where the mesh refines toward the "
             "channel network. THE granularity lever: peak depth and flooded "
             "extent are resolution-bound classes and a coarse mesh reads both "
             "low")
    mesh_max_edge_m = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(20.0, 5000.0), units="m", consequence="numerical",
        desc="Coarsest triangle edge, reached far from the channels on the "
             "hillslopes")


DOC = dict(
    summary="How much RUNOFF a storm produces from the catchment a point drains, "
            "as an outlet hydrograph and a flood-depth map.",
    routing=(
        "THE tool for \"peak discharge and runoff volume from a storm over this "
        "catchment\", \"rain falls on the basin and floods the valley\", "
        "\"rainfall-runoff hydrograph at the outlet\", \"flash flood from an intense "
        "storm on a watershed\". "
        "TELEMAC-2D shallow-water RAIN-ON-GRID over the domain this run solves on "
        "- the catchment traced upslope of `pour_point`, or a basin drawn or "
        "supplied as `domain` - meshed fine along its channels, bed from a "
        "bare-earth DEM, SCS curve-number infiltration per node from land cover. "
        "Produces an outlet HYDROGRAPH + a peak flood-DEPTH map. For a REAL event "
        "call `fetch_aorc_precip` over the catchment and pass its `precip_mm` as "
        "`rain_series_mm`; else the labeled design storm drives it."
    ),
    not_for=(
        "coastal or pluvial inundation depth; a channel dye or sediment plume "
        "(`telemac_dye_release`); urban pipe drainage; storm-surge coastal flooding"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved storm, catchment and mesh band for '
         'review/edit before the solve and WAITS, and asks for the pour point on the '
         'canvas; "auto" (session default) proceeds with every assumption labeled and '
         "REFUSES if no pour point was passed - an outlet is never invented. Not a "
         "physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "plus the outlet hydrograph charted at the pour point and placed there "
        "as a station. Its `answer` carries "
        "`peak_discharge_m3s` / `peak_discharge_time_s` / `runoff_volume_m3` / "
        "`rainfall_volume_m3` / `runoff_coefficient` / `catchment_area_km2` / "
        "`continuity_rel_error`, plus `max_depth_peak_m` beside `max_depth_p99_m` "
        "(the extreme and the field - one terrain pit can set the maximum) and "
        "`peak_is_window_truncated` (True means the discharge was still rising "
        "when the window closed, so the peak and the volumes are LOWER BOUNDS and "
        "must be narrated as such); narrate those typed numbers. Applicability: "
        "SINGLE-STORM flash-flood events in small steep catchments. Infiltrated "
        "water is permanently lost, so there is no baseflow and no inter-peak "
        "recovery. On failure a dict with `status=\"error\"` "
        "+ `error_code`."
    ),
)
