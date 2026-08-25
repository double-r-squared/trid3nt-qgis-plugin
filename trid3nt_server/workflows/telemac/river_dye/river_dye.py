"""Engine template ``telemac_river_dye`` - TELEMAC-2D river surface-tracer engine.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the reach mechanism is the TELEMAC
facade (``workflows/telemac/workflow.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how far a DYE / TRACER / CONTAMINANT / oil / sewage / sediment
spill travels DOWNSTREAM in a river reach, and what its peak concentration is;
OR where the bed SCOURS and re-deposits under a flood (GAIA erodible-bed
morphodynamics). TELEMAC-2D shallow water over a real reach, with the plume or
the bed evolution animated from the native time-stepped mesh.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import (
    AtomicToolMetadata,
    GateSpec,
    LeverSpec,
    ResolutionSpec,
)

from trid3nt_server.workflows.lib import (
    Data,
    DrawGate,
    Fetch,
    Forcing,
    FormGate,
    MeshPolicy,
    Param,
    ParamRef,
    Physics,
    Ref,
    doors,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import (
    TelemacDyeScenarioError,
    coerce_lonlat_point,
    compute_class,
    event_time,
    substance_class,
)
from trid3nt_server.workflows.telemac.workflow import CorridorPolicy, TelemacWorkflow

logger = logging.getLogger("trid3nt_server.workflows.telemac.river_dye.river_dye")

__all__ = ["ANSWER", "DATA", "PARAMS", "build_dye_chart", "plan", "telemac_river_dye"]

_STEPS = "trid3nt_server.workflows.telemac.steps"



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
          desc="Modeled channel width, used for the mesh node estimate and for the "
               "assumed ribbon when bank_source is not nhd_area"),
    Param("mesh_resolution", door=doors.CONSTANT, default="auto",
          consequence="numerical",
          desc="Mesh sizing mode: auto | fine | coarse"),
    Param("mesh_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(3.0, 5000.0), units="m", consequence="numerical",
          desc="Explicit target element edge length, overriding the sizing mode"),
    Param("bank_source", door=doors.CONSTANT, default="nhd_area",
          consequence="scenario",
          desc="Bank geometry source: nhd_area (real NHDArea polygons, else a typed "
               "refusal) | constant_ribbon (an assumed channel width)"),
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


#: The reach pipeline's REFERENCE data - fetched fresh for the domain the geocode
#: step binds, never BYO. The carrier discharge is a STEP rather than Data: it
#: reads the resolved mid-reach seed, which is a step result and not something a
#: producer declaration can name.
DATA = (
    Data("rivers", Fetch.tool(f"{_STEPS}.reach.fetch_reach_flowline",
                              prefetched=ParamRef("river_geometry_uri"))),
    Data("rain", Fetch.tool(f"{_STEPS}.forcing.resolve_rain_forcing",
                            rainfall_mm_per_day=ParamRef("rainfall_mm_per_day"),
                            evaporation_mm_per_day=ParamRef("evaporation_mm_per_day"),
                            gridmet_window=ParamRef("rainfall_gridmet_window"))
         .ladder("gridmet_domain_mean", "user_rate")
         # The cadence and units the deck receives, stated rather than assumed:
         # both rungs are daily rates, so this asks for no interpolation - and a
         # sub-daily target would refuse here instead of manufacturing a storm
         # shape gridMET never reported.
         .resample(to="1D", max_gap="native*3")
         .normalize(units="mm/day")),
)


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The river-tracer recipe. Pure: constructs the plan value, executes nothing.

    The gates come FIRST so every step and every producer downstream of them runs
    on the approved sheet - a step that had already consumed a value the form can
    revise would be exactly the contradiction the review exists to prevent.
    """
    physics = Physics(
        "tracer",
        substance=p.substance, release_coords=p.release_coords,
        reach_seed_coords=p.reach_seed_coords, sim_duration_s=p.sim_duration_s,
        spill_fraction=p.spill_fraction, spill_duration_s=p.spill_duration_s,
        dye_concentration_mgl=p.dye_concentration_mgl, source_q_m3s=p.source_q_m3s,
        output_interval_min=p.output_interval_min,
        friction_coefficient=p.friction_coefficient, friction_law=p.friction_law,
        velocity_diffusivity=p.velocity_diffusivity,
        tracer_diffusivity=p.tracer_diffusivity, erodible_bed=p.erodible_bed,
        sediment_gradation=p.sediment_gradation, dredging=p.dredging,
        decay_half_life_hours=p.decay_half_life_hours,
        decay_rate_per_day=p.decay_rate_per_day, sediment_type=p.sediment_type,
        grain_size_um=p.grain_size_um, bed_thickness_m=p.bed_thickness_m,
        bedload_formula=p.bedload_formula,
        morphological_factor=p.morphological_factor, dredge_mode=p.dredge_mode,
        dredge_volume_m3=p.dredge_volume_m3, dredge_disposal=p.dredge_disposal,
        dredge_crit_depth_m=p.dredge_crit_depth_m,
        dredge_dig_depth_m=p.dredge_dig_depth_m,
    )
    forcing = Forcing(carrier=Ref("carrier_discharge"), rain=d.rain,
                      wind_speed_mps=p.wind_speed_mps,
                      wind_direction_deg=p.wind_direction_deg)
    mesh = ops.build_mesh(
        Ref("reach"),
        MeshPolicy(resolution=p.mesh_resolution, target_edge_m=p.mesh_resolution_m),
        corridor=CorridorPolicy(extent_km=p.reach_length_km,
                                width_m=p.channel_width_m,
                                boundary_source=p.bank_source))
    return [
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, rivers=d.rivers,
                            discharge=p.discharge_m3s, event_time=p.event_time),
        ops.author(mesh=mesh, physics=physics, forcing=forcing),
        ops.solver_spec(compute_class=p.compute_class),
        ops.read_results(Ref("solve"), physics=physics, forcing=forcing)
           .chart("dye_concentration", builder=build_dye_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "max_deposition_mm", "max_scour_mm", "deposited_mass_kg",
          "deposit_fraction", "sediment_surface_d50_range_um", "mesh_size_m",
          "mesh_node_estimate")


def build_dye_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The plume's rise-to-peak chart SPEC: honest tracer scalars, never a fitted curve.

    Two points, both measured off the postprocessed field - zero concentration at
    release, then the peak at its arrival time. ``None`` when the run measured no
    peak, which is the honest "there was no curve to draw".
    """
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if cmax is None or peak_t is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    substance = params.get("substance") or "dye"
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": 0.0, "dye_mgl": 0.0},
                                {"t_s": float(peak_t), "dye_mgl": float(cmax)}]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"{str(substance).capitalize()} concentration (mg/L)"},
            },
        },
        title=f"Peak {substance} concentration - {where}",
        caption=(f"Reach peak {substance} concentration {float(cmax):.3g} mg/L, "
                 f"arriving {float(peak_t):.0f} s after release (idealized-bed demo)."),
    )


def release_points(args: dict[str, Any]) -> dict[str, Any]:
    """The release point, and the point the WORKER seeds the reach from.

    Models split the same value across three shapes - an explicit pair, split
    lon/lat, or one "lat,lon" string - and dropping the ones the signature does
    not name is the silent-swallow class. The approve-mesh decision tail is the
    only caller that separates release from seed: call-provided release coords
    seed the reach, while a gate-picked click moves the SOURCE only - re-seeding
    from the click would silently mesh a different reach than the one the user
    approved.
    """
    release = args.get("release_coords")
    if release is None:
        lat, lon = args.get("release_lat"), args.get("release_lon")
        if lat is None and lon is None and args.get("spill_location_latlon"):
            try:
                lat_s, lon_s = str(args["spill_location_latlon"]).split(",", 1)
                lat, lon = float(lat_s), float(lon_s)
            except (ValueError, TypeError):
                raise TelemacDyeScenarioError(
                    "TELEMAC_PARAMS_INVALID",
                    f"spill_location_latlon={args['spill_location_latlon']!r} is not "
                    "'lat,lon'. Supply release_coords as (lon, lat) instead.") from None
        release = None if (lat is None and lon is None) else [lon, lat]

    if args.get("_seed_release_lon") is not None \
            or args.get("_seed_release_lat") is not None:
        seed = [args.get("_seed_release_lon"), args.get("_seed_release_lat")]
    else:
        seed = None if args.get("_release_seeds_reach") is False else release
    return {"release_coords": coerce_lonlat_point(release),
            "reach_seed_coords": coerce_lonlat_point(seed)}


def wind_bearing(args: dict[str, Any]) -> dict[str, Any]:
    """A bearing WRAPS; it does not clamp, so the modulo happens before the door."""
    value = args.get("wind_direction_deg")
    if value is None:
        return {}
    try:
        return {"wind_direction_deg": float(value) % 360.0}
    except (TypeError, ValueError):
        return {}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening plume gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long reach WITHIN
#: this declaration, and the effective edge stays >= 2 cells across the channel.
_TELEMAC_RIVER_DYE_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long reach is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_TELEMAC_RIVER_DYE_METADATA = AtomicToolMetadata(
    name="telemac_river_dye",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    gate_spec=GateSpec(
        kind="solver",
        estimate_provider="trid3nt_server.gates.cards.solver_confirm:estimate_telemac_mesh",
        pin_provider="trid3nt_server.gates.cards.solver_confirm:pin_telemac_mesh",
        levers=(LeverSpec(name="mesh resolution", param="mesh_resolution_m", unit="m"),),
        title="TELEMAC approve-mesh",
        rationale="A consequential TELEMAC solve: preview + approve the river mesh before the run.",
    ),
    resolution_specs=(_TELEMAC_RIVER_DYE_RES_SPEC,),
)


_DOC = dict(
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


#: Wire ALIASES the model uses for values PARAMS already declares, plus the
#: approve-mesh decision tail (underscore -> stripped from the model's schema).
#: ``release_points`` folds them into the declared params before any door.
_EXTRA_ARGS: tuple[tuple[str, Any], ...] = (
    ("contaminant", str | None),
    ("release_lon", float | None),
    ("release_lat", float | None),
    ("spill_location_latlon", str | None),
    ("_release_seeds_reach", bool | None),
    ("_seed_release_lon", float | None),
    ("_seed_release_lat", float | None),
)


telemac_river_dye = register_workflow(
    TelemacWorkflow, _TELEMAC_RIVER_DYE_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    # The mesh row is present only when a sizing rule MOVED the user's explicit
    # edge length; on an honoured (or absent) override both fields read null.
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    coerce=(
        location_or_bbox("telemac_river_dye", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'dye spill in the river "
                              "near <place>', pass location='<place>'."),
        release_points,
        event_time(),
        substance_class(),
        compute_class(),
        wind_bearing,
    ),
    doc=_DOC,
    extra_args=_EXTRA_ARGS,
)
