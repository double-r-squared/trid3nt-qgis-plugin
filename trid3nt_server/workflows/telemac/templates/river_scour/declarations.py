"""The CONTRACT of ``telemac_river_scour``: what only a mobile-bed question asks."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors
from trid3nt_server.workflows.telemac.modules.gaia import GRAIN_UM_MAX, GRAIN_UM_MIN

__all__ = ["ACCEPTS", "DOC", "GRADATION_PRESETS", "PARAMS"]

#: Named gradations (d50 in microns, initial fraction) a mixture can be asked for
#: by name - honest demo mixes, never a measured site sieve curve. The fractions
#: are renormalized on use.
GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}

#: What a mobile-bed run can be HANDED. The bed evolves on the same triangulation
#: the hydrodynamics runs on, so a lattice is refused at the door.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a scouring-bed question asks."""
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

    # -- the release -------------------------------------------------------- #
    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-reach release position, 0=upstream..1=downstream; the source "
             "must sit strictly INSIDE the reach, never on a boundary")
    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "river's carrier flow")
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")
    rainfall_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 2000.0),
        units="mm/day", consequence="scenario",
        desc="Distributed ON-MESH rainfall applied at every wet node, independent "
             "of the inflow hydrograph")
    evaporation_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.0, 50.0),
        units="mm/day", consequence="scenario",
        desc="Distributed evaporation, subtracted from the net rain flux")
    rainfall_gridmet_window = Param(
        door=doors.USER, optional=True,
        consequence="scenario",
        desc="Real-storm source: an ISO window 'YYYY-MM-DD:YYYY-MM-DD' whose "
             "gridMET domain-mean daily precipitation supersedes rainfall_mm_per_day")

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the meshed reach; the "
            "downstream distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name")

    # -- the bed ------------------------------------------------------------ #
    grain_size_um = Param(
        door=doors.SCENARIO, default=200.0,
        bounds=(GRAIN_UM_MIN, GRAIN_UM_MAX), units="um", user_lever=True,
        consequence="scenario",
        desc="Median grain diameter d50 of the bed - ~200 um fine sand, ~20 um "
             "silt, ~8 um mud (all modeled non-cohesive); read only when no "
             "gradation is given")
    bed_thickness_m = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.05, 50.0),
        units="m", consequence="scenario",
        desc="Depth of the erodible sediment stock the bed can scour into")
    bedload_formula = Param(
        door=doors.SCENARIO, default=1, type=int, consequence="numerical",
        desc="GAIA bed-load law: 1=Meyer-Peter-Mueller, 2=Einstein-Brown, "
             "7=van Rijn")
    morphological_factor = Param(
        door=doors.SCENARIO, default=10.0, bounds=(1.0, 100.0),
        user_lever=True, consequence="numerical",
        desc="Amplifies bed change per hydraulic step so a short hydrograph "
             "yields a readable depth; a speed-up lever, not a rate")
    sediment_gradation = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=list | str,
        derived_when_absent=(
            "the bed is ONE class at grain_size_um, which is uniform by "
            "construction and cannot sort"),
        desc="Multi-class GRADED sediment: a preset name (graded_sand | "
             "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list "
             "of [d50_um, fraction] pairs; a mixture sorts under a hiding factor")
    tracer_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the marker tracer released at the source, which "
             "is what the deposited fraction is measured against")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time; the morphological factor is what makes a "
             "short window produce a readable bed change")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; scour "
             "depth is a resolution-bound class and a coarse mesh reads it low")


DOC = dict(
    summary="Bed SCOUR and DEPOSITION in a river reach: a mobile bed under a flow.",
    routing=(
        "THE tool for \"where does the bed scour and where does it re-deposit\" - "
        "erodible-bed morphodynamics below a dam, weir or bridge contraction, "
        "bedload transport and bed evolution under a flood, and how a GRADED grain "
        "mixture sorts and armors. TELEMAC-2D coupled with GAIA over a REAL "
        "NHDPlus reach with real NHDArea banks. Returns a bed-evolution map plus "
        "the time-stepped mesh. Supply `location` OR `bbox`."
    ),
    not_for=(
        "a SUSPENDED sediment plume settling onto an inert bed "
        "(`telemac_river_sediment_plume`); a conservative dye or contaminant "
        "plume (`telemac_river_dye`); an OIL slick "
        "(`telemac_river_oil_spill`); dissolved-oxygen sag (`telemac_do_sag`); "
        "rainfall-runoff flood depth (`telemac_rain_on_grid`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the filled sheet for review/edit before the solve '
         'and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top."),
    ),
    returns=(
        "On success the bed-evolution layer (a `LayerURI`, metres, deposition "
        "positive and scour negative) - the emitter loads the map and animates "
        "the bed beside it - whose `answer` carries `bed_evolution_max_m` / "
        "`bed_evolution_min_m` / `net_bed_mass_kg` / `surface_d50_spread_m` (a "
        "mixture's sorting signature, nothing on a single class) / "
        "`marker_cmax_mgl`; narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
