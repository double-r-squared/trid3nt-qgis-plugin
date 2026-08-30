"""The CONTRACT of ``telemac_river_dye``: its declared params and its prose.

One file over from the recipe. ``river_dye.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the fifty rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.lib import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a reach run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh; a
#: lattice is refused at the door rather than trusted into a deck that assumes
#: edges. The release enters the water at a POINT - the one release geometry this
#: plume pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name on the river, geocoded to the reach"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place"),
    Param("substance", door=doors.QUESTION, default="dye", consequence="scenario",
          desc="What was spilled - dye | oil/diesel/crude | sewage/E.coli | "
               "sediment/sand/silt | scour/erosion | graded/mixed-grain | dredging; "
               "the word picks the TELEMAC module family"),
    Param("release_coords", door=doors.USER, optional=True, user_lever=True,
          consequence="scenario", type=tuple[float, float] | list[float],
          derived_when_absent=(
              "the release sits at spill_fraction along the meshed reach; the "
              "downstream plume distance is measured from there"),
          desc="Where the substance enters the water, (lon, lat) EPSG:4326"),

    # -- the scenario ------------------------------------------------------- #
    Param("spill_fraction", door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
          consequence="scenario",
          desc="Along-reach release position, 0=upstream..1=downstream; the source "
               "must sit strictly INSIDE the reach, never on a boundary"),
    Param("spill_duration_s", door=doors.SCENARIO, default=300.0,
          bounds=(1.0, 86400.0), units="s", consequence="scenario",
          desc="Finite pulse injection window"),
    Param("dye_concentration_mgl", door=doors.SCENARIO, default=100.0,
          bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
          desc="Source concentration of the released substance"),
    Param("source_q_m3s", door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
          units="m^3/s", consequence="scenario",
          desc="Point-source discharge of the release itself, small against the "
               "river's carrier flow"),
    Param("reach_length_km", door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
          units="km", consequence="aoi",
          desc="Modeled reach length downstream of the release; a longer reach is "
               "coarsened under the mesh node budget"),
    Param("sim_duration_s", door=doors.SCENARIO, default=3600.0,
          bounds=(600.0, 14400.0), units="s", consequence="numerical",
          desc="Simulated physical time"),
    Param("wind_speed_mps", door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
          units="m/s", consequence="scenario",
          desc="Sustained wind driving a surface wind-stress term; 0 = no wind"),
    Param("wind_direction_deg", door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
          units="deg", consequence="scenario",
          desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
               "wind_speed_mps > 0"),

    # -- forcing levers ----------------------------------------------------- #
    Param("discharge_m3s", door=doors.USER, optional=True, units="m^3/s",
          bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
          derived_when_absent=(
              "the steady carrier discharge is resolved from the NOAA National "
              "Water Model at the reach; no NWM coverage refuses typed rather "
              "than falling back to a constant"),
          desc="Steady upstream CARRIER discharge - the river flow that dilutes "
               "and transports the release"),
    Param("event_time", door=doors.QUESTION, optional=True, consequence="scenario",
          derived_when_absent=(
              "the carrier discharge is read at the MOST RECENT published NWM "
              "cycle"),
          desc="The storm/event moment to read the carrier discharge cycle at - "
               "from phrasing like 'during last Tuesday's storm'; an ISO date "
               "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
               "Unset reads the most recent published NWM cycle. The NWM PDS "
               "bucket retains only the last ~30 days of history; a deeper "
               "request refuses typed rather than silently reading a "
               "different cycle."),
    Param("rainfall_mm_per_day", door=doors.USER, optional=True, bounds=(0.0, 2000.0),
          units="mm/day", consequence="scenario",
          desc="Distributed ON-MESH rainfall applied at every wet node, independent "
               "of the inflow hydrograph"),
    Param("evaporation_mm_per_day", door=doors.USER, optional=True, bounds=(0.0, 50.0),
          units="mm/day", consequence="scenario",
          desc="Distributed evaporation, subtracted from the net rain flux"),
    Param("rainfall_gridmet_window", door=doors.USER, optional=True,
          consequence="scenario",
          desc="Real-storm source: an ISO window 'YYYY-MM-DD:YYYY-MM-DD' whose "
               "gridMET domain-mean daily precipitation supersedes rainfall_mm_per_day"),

    # -- substance-class levers --------------------------------------------- #
    Param("decay_half_life_hours", door=doors.USER, optional=True, bounds=(0.1, 720.0),
          units="h", user_lever=True, consequence="scenario",
          desc="Decaying substances only - first-order half-life; unset uses the "
               "narrated literature default for the named substance"),
    Param("decay_rate_per_day", door=doors.USER, optional=True, bounds=(0.01, 100.0),
          units="1/day", user_lever=True, consequence="scenario",
          desc="Decaying substances only - decay rate per day, as an alternative to "
               "the half-life"),
    Param("grain_size_um", door=doors.USER, optional=True, bounds=(5.0, 2000.0),
          units="um", user_lever=True, consequence="scenario",
          desc="Sediment only - median grain diameter d50; ~200 um fine sand settles "
               "within a few km, ~20 um silt mostly stays suspended"),
    Param("sediment_type", door=doors.USER, optional=True, consequence="scenario",
          desc="Sediment alias - sand | silt | mud - picking the default grain size"),
    Param("erodible_bed", door=doors.USER, optional=True, consequence="scenario",
          type=bool,
          derived_when_absent=(
              "the bed is erodible only when the substance names scour / erosion / "
              "a mobile bed, or a graded mixture or dredging rule needs one"),
          desc="Force GAIA erodible-bed morphodynamics on (True) or off (False): a "
               "real bed with active bedload, so it scours and re-deposits"),
    Param("bed_thickness_m", door=doors.USER, optional=True, bounds=(0.05, 50.0),
          units="m", consequence="scenario",
          desc="Erodible bed only - depth of the erodible sediment stock"),
    Param("bedload_formula", door=doors.USER, optional=True, consequence="numerical",
          type=int,
          desc="Erodible bed only - GAIA bed-load law: 1=Meyer-Peter-Mueller "
               "(default), 2=Einstein-Brown, 7=van Rijn"),
    Param("morphological_factor", door=doors.USER, optional=True, bounds=(1.0, 100.0),
          user_lever=True, consequence="numerical",
          desc="Erodible bed only - amplifies bed change per hydraulic step so a "
               "short hydrograph yields a readable depth; a speed-up lever, not a rate"),
    Param("sediment_gradation", door=doors.USER, optional=True, consequence="scenario",
          type=list | str,
          desc="Multi-class graded sediment: a preset name (graded_sand | "
               "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list of "
               "[d50_um, fraction] pairs; forces a mobile bed so the mix can sort"),
    Param("dredging", door=doors.USER, optional=True, consequence="scenario",
          type=bool,
          derived_when_absent=(
              "the NESTOR dig/dump rule arms only when the ask names dredging, "
              "channel maintenance, spoil disposal or shoaling"),
          desc="Force the NESTOR channel-maintenance dig/dump rule on or off; it "
               "layers onto the erodible-bed morphodynamics"),
    Param("dredge_mode", door=doors.CONSTANT, default="scheduled",
          consequence="scenario",
          desc="Dredging rule: scheduled (remove a target volume over a window) | "
               "criterion (dig only where the bed silts within tolerance of grade)"),
    Param("dredge_volume_m3", door=doors.USER, optional=True, bounds=(1.0, 1.0e7),
          units="m^3", consequence="scenario",
          desc="Scheduled-mode target dredged volume"),
    Param("dredge_crit_depth_m", door=doors.USER, optional=True, bounds=(0.01, 20.0),
          units="m", consequence="scenario",
          desc="Criterion-mode siltation tolerance above the design grade"),
    Param("dredge_dig_depth_m", door=doors.USER, optional=True, bounds=(0.05, 30.0),
          units="m", consequence="scenario",
          desc="Criterion-mode dig target below the design grade"),
    Param("dredge_disposal", door=doors.USER, optional=True, consequence="scenario",
          type=bool,
          derived_when_absent="the spoil is not placed (dredge-only)",
          desc="Also place the dug spoil in a downstream disposal zone"),

    # -- advanced constitutive physics -------------------------------------- #
    Param("friction_coefficient", door=doors.USER, optional=True, bounds=(10.0, 90.0),
          user_lever=True, consequence="numerical",
          desc="Bed roughness under friction_law; unset keeps the deck's own value"),
    Param("friction_law", door=doors.USER, optional=True, consequence="numerical",
          type=int,
          desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, 4=Manning"),
    Param("velocity_diffusivity", door=doors.USER, optional=True, bounds=(1e-3, 10.0),
          units="m^2/s", consequence="numerical",
          desc="Turbulent momentum diffusivity"),
    Param("tracer_diffusivity", door=doors.USER, optional=True, bounds=(1e-3, 10.0),
          units="m^2/s", consequence="numerical",
          desc="Tracer diffusivity, which sets lateral plume spread"),

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    Param("channel_width_m", door=doors.CONSTANT, default=60.0, bounds=(10.0, 1500.0),
          units="m", consequence="numerical",
          desc="Modeled channel width, used for the mesh node estimate"),
    Param("mesh_resolution", door=doors.CONSTANT, default="auto",
          consequence="numerical",
          desc="Mesh sizing mode: auto | fine | coarse"),
    Param("mesh_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(3.0, 5000.0), units="m", consequence="numerical",
          desc="Explicit target element edge length, overriding the sizing mode"),
    Param("bank_source", door=doors.CONSTANT, default="nhd_area",
          consequence="scenario",
          desc="Bank geometry source: nhd_area - the real mapped NHDArea polygon. An "
               "unmapped reach refuses; there is no assumed-width rung"),
    Param("output_interval_min", door=doors.USER, optional=True, bounds=(0.1, 1440.0),
          units="min", consequence="numerical",
          desc="Result-writing cadence; unset keeps the deck's own graphic period"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
    Param("river_geometry_uri", door=doors.USER, optional=True, consequence="aoi",
          derived_when_absent="the reach flowline is fetched fresh for the AOI",
          desc="Reuse an already-fetched fetch_river_geometry flowline for this reach "
               "instead of re-fetching it"),
    Param("reach_seed_coords", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float] | list[float], wire=False,
          derived_when_absent=(
              "the reach centerline is resolved from the mid-reach point on the "
              "largest fetched flowline, else the geocoded centroid"),
          desc="The point the worker resolves the reach centerline from, (lon, lat); "
               "set when the release must pin which water body is meshed"),
)


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
        "groundwater plumes / river seepage (`modflow_*`); dissolved-oxygen sag below an "
        "outfall (`telemac_do_sag`); flood depth (`sfincs_flood` / `swmm_urban_flood`); "
        "dam-break or tsunami run-up (`geoclaw_inundation`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point on the canvas before the solve, and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step - which on this plan means a completed solve is replayed "
         "from its own artifact instead of re-solving. A run that completed is marked "
         "complete and is never replayed."),
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
