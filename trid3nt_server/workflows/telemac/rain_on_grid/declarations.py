"""The CONTRACT of ``telemac_rain_on_grid``: its declared params and its prose.

One file over from the recipe. ``rain_on_grid.py`` reads on one page - the
question, the data, the plan, the answer, the chart - because the twenty-odd rows
that describe every value it can take live here instead of in front of them.

Every number the run uses is on this page or is a labeled constant below it. The
migration inventory recorded seventeen bare signature defaults, an invented
pour point, an unreachable slope correction and a duplicated UTM formula in the
composer this replaces; the point of the shape is that a value with nowhere to be
declared has nowhere to hide.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Param, doors

__all__ = [
    "DEFAULT_MAX_EDGE_M",
    "DEFAULT_MIN_EDGE_M",
    "DOC",
    "NLCD_NATIVE_RESOLUTION_M",
    "PARAMS",
    "POUR_POINT_BUFFER_DEG",
]


#: Half-side (deg) of the AOI a catchment is delineated inside, centred on the
#: OUTLET. +-0.14 deg is a 0.28-deg box: comfortably under the 0.3-deg D8
#: watershed-primitive clamp, and generous enough to contain one headwater basin
#: upstream of its outlet. The delineation TRUNCATES at the box edge, so this must
#: over-cover; a box that clips the basin mid-hillslope answers a smaller
#: question and says nothing. It is a labeled constant rather than a form row for
#: the same reason the other four AOI half-widths in this engine are: the
#: granularity-gate wave owns turning the extent knobs into user levers, and five
#: new rows across five templates belong to that wave rather than to this one.
POUR_POINT_BUFFER_DEG: float = 0.14

#: The edge-length BAND a catchment interior is triangulated between: fine in the
#: channel band, coarse on the hillslopes, and how fast the edge may grow between
#: the two. Labeled defaults on this sheet and nowhere else - the ask reaches the
#: mesher through the MESH declaration, so there is one number per dial.
DEFAULT_MIN_EDGE_M: float = 40.0
DEFAULT_MAX_EDGE_M: float = 300.0
DEFAULT_GRADE: float = 0.20

#: Ground resolution the BARE-EARTH bed is sampled at the mesh nodes from.
DEFAULT_BED_RESOLUTION_M: int = 10

#: The channel network mesh refinement is sized by distance to.
DEFAULT_RIVER_SOURCE: str = "nhdplus_hr"

#: NLCD's own grid. Land cover is a CATEGORICAL raster, so asking for any other
#: spacing resamples class labels - which is the one resampling the temporal and
#: spatial doctrine refuses outright. Declared as a constant rather than a knob
#: because there is no honest value other than the product's native one.
NLCD_NATIVE_RESOLUTION_M: int = 30


class PARAMS:
    # -- the question ------------------------------------------------------- #
    pour_point = Param(
        door=doors.USER, consequence="aoi",
        type=tuple[float, float] | list[float] | str,
        desc="The catchment OUTLET as (lon, lat) EPSG:4326 - the point the runoff "
             "drains to. It decides which basin is modelled at all, so it is asked "
             "for (drawn on the canvas or passed explicitly) and NEVER invented")
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        derived_when_absent=(
            "the run is named for its domain rather than for a place"),
        desc="Place naming the catchment. It names the run and the published "
             "layers; the basin's SHAPE is the terrain's answer at the pour "
             "point, never the geocoder's bbox")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        derived_when_absent=(f"the analysis window is a +-{POUR_POINT_BUFFER_DEG:g} "
                             "deg buffer around the outlet"),
        desc="Explicit analysis AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 the "
             "catchment is delineated INSIDE; it must contain the whole upstream "
             "basin, because delineation truncates at its edge")

    # -- the storm ---------------------------------------------------------- #
    rain_window = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the run is driven by the labeled constant design storm below"),
        desc="A REAL storm window as 'YYYY-MM-DD/YYYY-MM-DD'. Drives the run with "
             "the hourly AORC hyetograph over the catchment - the true intensity "
             "structure, which is what resolves the hydrograph SHAPE. AORC rather "
             "than MRMS because MRMS only covers ~2020-10 onward")
    design_storm_mm_per_hr = Param(
        door=doors.SCENARIO, default=25.0,
        bounds=(0.1, 500.0), units="mm/h", consequence="physics",
        desc="Constant design-storm intensity, used when no rain_window names a "
             "real event. A hypothetical storm, labeled as one")
    storm_duration_hr = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.1, 240.0),
        units="h", consequence="scenario",
        desc="How long the design storm rains for")
    # The simulated window is a SCENARIO choice, not a numerics fact. Unlike the
    # wave and 3D windows (CONSTANT: "long enough to reach steady state") and
    # unlike the coastal window (USER: the gauge record defines it), how long you
    # watch a catchment respond decides whether the hydrograph carries its peak
    # and how much of the recession - which is part of the question being asked.
    # It stays on the model-facing wire for exactly that reason.
    sim_duration_hr = Param(
        door=doors.SCENARIO, optional=True, bounds=(0.1, 720.0),
        units="h", consequence="numerical",
        derived_when_absent=(
            "the simulated window is the fetched hyetograph's OWN span, or - for a "
            "design storm, which has no record to take a span from - the storm's "
            "own duration"),
        desc="Total simulated window; longer than the rain to watch the recession")

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
    landcover_dataset = Param(
        door=doors.CONSTANT, default="nlcd_2021",
        consequence="physics",
        desc="Land-cover product the per-node curve numbers and Manning n are "
             "keyed to")

    # -- the domain (the granularity lever) --------------------------------- #
    mesh_min_edge_m = Param(
        door=doors.SCENARIO, default=DEFAULT_MIN_EDGE_M,
        bounds=(5.0, 500.0), units="m", user_lever=True, consequence="numerical",
        desc="Finest triangle edge, used where the mesh refines toward the channel "
             "network. THE granularity lever: peak depth and flooded extent are "
             "resolution-bound classes and a coarse mesh reads both low")
    mesh_max_edge_m = Param(
        door=doors.SCENARIO, default=DEFAULT_MAX_EDGE_M,
        bounds=(20.0, 5000.0), units="m", consequence="numerical",
        desc="Coarsest triangle edge, reached far from the channels on the "
             "hillslopes")
    mesh_grade = Param(
        door=doors.CONSTANT, default=DEFAULT_GRADE, bounds=(0.05, 0.5),
        consequence="numerical",
        desc="Mesh gradation: how fast the edge length may grow between the "
             "channel band and the hillslopes")
    # A SAMPLING spacing, so numerical: which bed the nodes are sampled from is
    # the physics (bare earth against a canopy-inclusive surface model, declared
    # on the bed_dem ladder), and how finely that bed is read is a solver-side
    # discretization the answer converges under.
    bed_dem_resolution_m = Param(
        door=doors.CONSTANT,
        default=DEFAULT_BED_RESOLUTION_M, type=int,
        bounds=(1.0, 90.0), units="m", consequence="numerical",
        desc="Ground resolution the BARE-EARTH bed is sampled at the mesh nodes "
             "from; a surface model would put the bed on the forest canopy")
    river_source = Param(
        door=doors.CONSTANT, default=DEFAULT_RIVER_SOURCE,
        consequence="numerical",
        desc="Channel network the mesh refinement is sized by distance to")

    # -- numerics (the advanced fold) --------------------------------------- #
    time_step_s = Param(
        door=doors.CONSTANT, default=3.0, bounds=(0.1, 60.0),
        units="s", consequence="numerical",
        desc="Solver time step. Overland sheet flow on a fine catchment mesh is "
             "CFL-tight, which is why it is seconds rather than tens of seconds")
    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        derived_when_absent="the worker's own graphic period stands",
        desc="Result-writing cadence; a hydrograph resolved in six frames cannot "
             "be spot-checked against, so a short window wants a short interval")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


DOC = dict(
    summary="How much RUNOFF a storm produces from this WATERSHED, as an outlet "
            "hydrograph and a flood-depth map.",
    routing=(
        "THE tool for \"peak discharge and runoff volume from a storm over this "
        "catchment\", \"rain falls on the basin and floods the valley\", "
        "\"rainfall-runoff hydrograph at the outlet\", \"flash flood from an intense "
        "storm on a watershed\", \"dry vs wet antecedent soil\". "
        "TELEMAC-2D full shallow-water RAIN-ON-GRID over a catchment "
        "DELINEATED at a pour point and triangulated from a real DEM, with SCS "
        "curve-number infiltration distributed per node from land cover. Produces "
        "an outlet HYDROGRAPH + a peak flood-DEPTH map. Supply the outlet as "
        "`pour_point` (lon, lat); a dated `rain_window` drives it with the real "
        "hourly hyetograph instead of the design storm."
    ),
    not_for=(
        "coastal or pluvial inundation depth; a channel dye or sediment plume "
        "(`telemac_river_dye`); urban pipe drainage; storm-surge coastal flooding"
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
        "On success a `TelemacRainOnGridLayerURI` (a `LayerURI` subtype) - the "
        "emitter loads the peak flood-depth COG and animates the rain-on-grid "
        "SELAFIN sibling. It carries `peak_discharge_m3s` / `peak_discharge_time_s` "
        "/ `runoff_volume_m3` / `runoff_coefficient` / `catchment_area_km2` / "
        "`continuity_rel_error`; narrate those typed numbers. Applicability: "
        "SINGLE-STORM flash-flood events in small steep catchments. Infiltrated "
        "water is permanently lost, so there is no baseflow and no inter-peak "
        "recovery. On failure a dict with `status=\"error\"` "
        "+ `error_code`."
    ),
)
