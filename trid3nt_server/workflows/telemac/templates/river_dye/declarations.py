"""The CONTRACT of ``telemac_river_dye``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.workflows.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DECAY_PRESETS", "DOC", "PARAMS"]

#: The first-order die-off a named substance runs under, as the degradation law
#: and its coefficient: 1 is a T90 in hours, 2 a rate per hour. Bacterial words
#: carry T90 ~ 2 h, the daylight freshwater fecal-coliform die-off - a narrated
#: literature default, never a measured observation. A key matches as a
#: substring of the named word.
DECAY_PRESETS: dict[str, dict[str, float]] = {
    "sewage": {"law": 1, "coef": 2.0},
    "e. coli": {"law": 1, "coef": 2.0},
    "e.coli": {"law": 1, "coef": 2.0},
    "e coli": {"law": 1, "coef": 2.0},
    "ecoli": {"law": 1, "coef": 2.0},
    "coliform": {"law": 1, "coef": 2.0},
    "coli": {"law": 1, "coef": 2.0},
    "bacteria": {"law": 1, "coef": 2.0},
    "bacterial": {"law": 1, "coef": 2.0},
    "effluent": {"law": 1, "coef": 2.0},
    "wastewater": {"law": 1, "coef": 2.0},
    "die-off": {"law": 1, "coef": 2.0},
    "decaying": {"law": 2, "coef": 0.35},
    "half-life": {"law": 2, "coef": 0.35},
}

#: What a reach run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh; a
#: lattice is refused at the door rather than trusted into a run that assumes
#: edges. The release enters the water at a POINT - the one release geometry this
#: plume pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a conservative-plume question asks: the release, what is
    released and how much of it, whether it decays, and how much of the river
    is modelled for how long and at what edge."""
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
             "point layer, or a place name; its name becomes the tracer's name")

    # -- the scenario ------------------------------------------------------- #
    dye_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Source concentration of the released substance")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")

    # -- decay, the one optional coupling this question carries -------------- #
    decaying_substance = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the tracer is CONSERVATIVE - it dilutes and advects and nothing "
            "removes it"),
        desc="Name a substance whose tracer DECAYS - sewage | E. coli | coliform "
             "| bacteria | effluent | wastewater - and its narrated literature "
             "die-off is applied as a first-order sink on the plume")
    decay_half_life_hours = Param(
        door=doors.USER, optional=True, bounds=(0.1, 720.0),
        units="h", user_lever=True, consequence="scenario",
        desc="First-order half-life of the released substance; unset uses the "
             "narrated literature default for decaying_substance, and neither "
             "leaves the tracer conservative")
    decay_rate_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.01, 100.0),
        units="1/day", user_lever=True, consequence="scenario",
        desc="Decay rate per day, as an alternative to the half-life")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; peak "
             "concentration is a resolution-bound class and a coarse mesh reads "
             "it low")
    continue_from = Param(
        door=doors.USER, optional=True, type=str,
        consequence="numerical",
        derived_when_absent=(
            "the run starts from its own initial conditions - a constant depth "
            "at rest - rather than from another run's state"),
        desc="Continue a previous run: the URI of its restart_river.slf, the "
             "state at its last instant, which becomes this run's initial "
             "state - so sim_duration_s is the time added ON TOP of it and the "
             "same declared scenario carries on over the longer horizon (a "
             "release whose spill_duration_s has elapsed stays finished). The "
             "mesh must be the same one, and a run that couples WAQTEL refuses")


DOC = dict(
    summary="A DYE / TRACER / CONTAMINANT plume that TRAVELS DOWNSTREAM in a RIVER (surface water).",
    routing=(
        "THE tool for \"a spill in the river, how far downstream does it travel\" - a "
        "dye / contaminant / pollutant / chemical plume moving down the channel, "
        "sewage or E.coli effluent DECAYING downstream (name it in "
        "`decaying_substance`), and wind setup on a wide reach. TELEMAC-2D over a "
        "REAL NHDPlus reach with real NHDArea banks: a finite pulse is advected by "
        "the carrier discharge and dilutes. Returns a PEAK concentration map + the "
        "time-stepped mesh the client animates. Supply `location` OR `bbox`."
    ),
    not_for=(
        "an OIL slick (`telemac_river_oil_spill`); bed SCOUR, deposition, grain "
        "sorting or dredging (`telemac_river_scour`); a SUSPENDED sediment plume "
        "settling onto the bed (`telemac_river_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`). Groundwater plumes, and dam-break or tsunami "
        "run-up, are not modeled here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point on the canvas before the solve, and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success the peak-concentration layer (a `LayerURI`) - the emitter loads "
        "the map and animates the result mesh beside it - whose `answer` carries "
        "`dye_cmax_mgl` / `dye_peak_time_s` / `plume_reach_m` / `active_frames`; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
