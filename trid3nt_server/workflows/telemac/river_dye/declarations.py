"""The CONTRACT of ``telemac_river_dye``: its declared params and its prose.

One file over from the recipe. ``river_dye.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the fifty rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a reach run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh; a
#: lattice is refused at the door rather than trusted into a run that assumes
#: edges. The release enters the water at a POINT - the one release geometry this
#: plume pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    # -- the question ------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Place name on the river, geocoded to the reach")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place")
    substance = Param(
        door=doors.QUESTION, default="dye", consequence="scenario",
        desc="What was spilled - dye | oil/diesel/crude | sewage/E.coli | "
             "sediment/sand/silt | scour/erosion | graded/mixed-grain | dredging; "
             "the word picks the TELEMAC module family")
    release_coords = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=tuple[float, float] | list[float],
        derived_when_absent=(
            "the release sits at spill_fraction along the meshed reach; the "
            "downstream plume distance is measured from there"),
        desc="Where the substance enters the water, (lon, lat) EPSG:4326")

    # -- the scenario ------------------------------------------------------- #
    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-reach release position, 0=upstream..1=downstream; the source "
             "must sit strictly INSIDE the reach, never on a boundary")
    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")
    dye_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Source concentration of the released substance")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "river's carrier flow")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")

    # -- forcing levers ----------------------------------------------------- #
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

    # -- substance-class levers --------------------------------------------- #
    decay_half_life_hours = Param(
        door=doors.USER, optional=True, bounds=(0.1, 720.0),
        units="h", user_lever=True, consequence="scenario",
        desc="Decaying substances only - first-order half-life; unset uses the "
             "narrated literature default for the named substance")
    decay_rate_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.01, 100.0),
        units="1/day", user_lever=True, consequence="scenario",
        desc="Decaying substances only - decay rate per day, as an alternative to "
             "the half-life")
    grain_size_um = Param(
        door=doors.USER, optional=True, bounds=(5.0, 2000.0),
        units="um", user_lever=True, consequence="scenario",
        desc="Sediment only - median grain diameter d50; ~200 um fine sand settles "
             "within a few km, ~20 um silt mostly stays suspended")
    sediment_type = Param(
        door=doors.USER, optional=True, consequence="scenario",
        desc="Sediment alias - sand | silt | mud - picking the default grain size")
    erodible_bed = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=bool,
        derived_when_absent=(
            "the bed is erodible only when the substance names scour / erosion / "
            "a mobile bed, or a graded mixture or dredging rule needs one"),
        desc="Force GAIA erodible-bed morphodynamics on (True) or off (False): a "
             "real bed with active bedload, so it scours and re-deposits")
    bed_thickness_m = Param(
        door=doors.USER, optional=True, bounds=(0.05, 50.0),
        units="m", consequence="scenario",
        desc="Erodible bed only - depth of the erodible sediment stock")
    bedload_formula = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
        desc="Erodible bed only - GAIA bed-load law: 1=Meyer-Peter-Mueller "
             "(default), 2=Einstein-Brown, 7=van Rijn")
    morphological_factor = Param(
        door=doors.USER, optional=True, bounds=(1.0, 100.0),
        user_lever=True, consequence="numerical",
        desc="Erodible bed only - amplifies bed change per hydraulic step so a "
             "short hydrograph yields a readable depth; a speed-up lever, not a rate")
    sediment_gradation = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=list | str,
        desc="Multi-class graded sediment: a preset name (graded_sand | "
             "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list of "
             "[d50_um, fraction] pairs; forces a mobile bed so the mix can sort")
    dredging = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=bool,
        derived_when_absent=(
            "the NESTOR dig/dump rule arms only when the ask names dredging, "
            "channel maintenance, spoil disposal or shoaling"),
        desc="Force the NESTOR channel-maintenance dig/dump rule on or off; it "
             "layers onto the erodible-bed morphodynamics")
    dredge_mode = Param(
        door=doors.CONSTANT, default="scheduled",
        consequence="scenario",
        desc="Dredging rule: scheduled (remove a target volume over a window) | "
             "criterion (dig only where the bed silts within tolerance of grade)")
    dredge_volume_m3 = Param(
        door=doors.USER, optional=True, bounds=(1.0, 1.0e7),
        units="m^3", consequence="scenario",
        desc="Scheduled-mode target dredged volume")
    dredge_crit_depth_m = Param(
        door=doors.USER, optional=True, bounds=(0.01, 20.0),
        units="m", consequence="scenario",
        desc="Criterion-mode siltation tolerance above the design grade")
    dredge_dig_depth_m = Param(
        door=doors.USER, optional=True, bounds=(0.05, 30.0),
        units="m", consequence="scenario",
        desc="Criterion-mode dig target below the design grade")
    dredge_disposal = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=bool,
        derived_when_absent="the spoil is not placed (dredge-only)",
        desc="Also place the dug spoil in a downstream disposal zone")
    dredge_bank_offset_m = Param(
        door=doors.SCENARIO, default=5.0,
        bounds=(0.0, 200.0), units="m", user_lever=True, consequence="scenario",
        desc="Bank setback the dig field is held back from the mapped water's "
             "edge, so the cut does not undercut the bank it is dug beside. It "
             "is also what excludes a stretch too narrow to dredge: narrower "
             "than twice the setback and no field survives there")

    # -- advanced constitutive physics -------------------------------------- #
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        desc="Bed roughness under friction_law; unset keeps the author's own value")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
        desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, 4=Manning")
    velocity_diffusivity = Param(
        door=doors.USER, optional=True, bounds=(1e-3, 10.0),
        units="m^2/s", consequence="numerical",
        desc="Turbulent momentum diffusivity")
    tracer_diffusivity = Param(
        door=doors.USER, optional=True, bounds=(1e-3, 10.0),
        units="m^2/s", consequence="numerical",
        desc="Tracer diffusivity, which sets lateral plume spread")

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
    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        desc="Result-writing cadence; unset keeps the steering file's own period")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")
    river_geometry_uri = Param(
        door=doors.USER, optional=True, consequence="aoi",
        derived_when_absent="the reach flowline is fetched fresh for the AOI",
        desc="Reuse an already-fetched fetch_river_geometry flowline for this reach "
             "instead of re-fetching it")
    reach_seed_coords = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float] | list[float], wire=False,
        derived_when_absent=(
            "the reach centerline is resolved from the mid-reach point on the "
            "largest fetched flowline, else the geocoded centroid"),
        desc="The point the reach centerline is navigated from, (lon, lat); "
             "set when the release must pin which water body is meshed")
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
             "mesh must be the same one, and a class that couples with WAQTEL "
             "or GAIA refuses")


DOC = dict(
    summary="A DYE / TRACER / CONTAMINANT plume that TRAVELS DOWNSTREAM in a RIVER (surface water).",
    routing=(
        "THE tool for \"a spill in the river, how far downstream does it travel\", a "
        "dye/contaminant/pollutant plume moving down the channel, an oil slick from a "
        "barge, sewage/E.coli effluent decaying downstream, sediment settling onto the "
        "bed, where the bed SCOURS below a dam/weir/bridge and re-deposits, how a graded "
        "grain mixture SORTS/armors, channel-maintenance DREDGING against siltation, and "
        "wind setup on a wide reach. TELEMAC-2D shallow water over a REAL NHDPlus reach "
        "with real NHDArea banks: a finite pulse is advected by the carrier discharge and "
        "dilutes. Returns a PEAK concentration map + the time-stepped mesh the client "
        "animates. Supply `location` (geocoded) OR `bbox`."
    ),
    not_for=(
        "dissolved-oxygen sag below an outfall (`telemac_do_sag`); rainfall-runoff "
        "flood depth (`telemac_rain_on_grid`). Groundwater plumes and seepage, and "
        "dam-break or tsunami run-up, are not currently modeled here"
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
        "On success a `TelemacDyeLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the peak-concentration map and animates the SELAFIN sibling. It carries "
        "`dye_cmax_mgl` / `dye_peak_time_s` / `plume_reach_m` / `active_frames`, plus "
        "`max_scour_mm` / `max_deposition_mm` / `deposited_mass_kg` on a sediment run; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
