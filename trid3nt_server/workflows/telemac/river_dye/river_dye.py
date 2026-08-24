"""Engine template ``telemac_river_dye`` - TELEMAC-2D river surface-tracer engine.

Declared as PARAMS + DATA + a pure ``plan(p, d)``: the tool body normalizes the
wire args, resolves the doors, validates the plan and hands it to the
interpreter. The reach pipeline itself is the shared TELEMAC step family
(``workflows/telemac/steps``), so every river template runs the same skeleton.
See ``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.telemac_contracts import TelemacDyeLayerURI
from trid3nt_contracts.tool_registry import (
    AtomicToolMetadata,
    GateSpec,
    LeverSpec,
    ResolutionSpec,
)

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.declarative import (
    DeclarativeError,
    DrawGate,
    Data,
    Fetch,
    FormGate,
    Param,
    ParamRef,
    Ref,
    Workflow,
    doors,
    interpret,
    merge_provenance,
    render_docstring,
    resolve_params,
)
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.shared.run_products import persist_run_products
from trid3nt_server.workflows.telemac.steps import (
    CarrierDischarge,
    Geocode,
    Products,
    ReachSeed,
    Solve,
    TelemacDyeScenarioError,
    WriteDeck,
    classify_substance,
    coerce_event_time,
    coerce_lonlat_point,
    sanitize_substance,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.river_dye.river_dye")

__all__ = ["DATA", "PARAMS", "plan", "telemac_river_dye"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The compute ladder the dispatcher knows. Anything outside it is a model
#: invention that used to crash the dispatch AFTER the geocode and river fetch.
_ALLOWED_COMPUTE = frozenset(
    {"small", "medium", "standard", "large", "xlarge", "gpu"})


TEMPLATE_CARD = TemplateCard(
    question=(
        "how far a DYE / TRACER / CONTAMINANT / oil / sewage / sediment spill "
        "travels DOWNSTREAM in a river reach (surface water) + its peak "
        "concentration; OR where the BED SCOURS / ERODES and re-deposits under a "
        "flood (GAIA erodible-bed morphodynamics) - TELEMAC-2D shallow-water over "
        "a real reach; animated time-stepped plume / bed evolution"
    ),
    required_inputs=["location OR bbox"],
    knobs=(
        "spill_fraction, spill_duration_s, dye_concentration_mgl, "
        "reach_length_km, sim_duration_s, source_q_m3s, channel_width_m, "
        "substance (dye / oil / sewage / sediment / scour / graded), mesh_resolution, "
        "release_coords, event_time, decay_half_life_hours, grain_size_um, erodible_bed, "
        "bed_thickness_m, bedload_formula, morphological_factor, sediment_gradation, "
        "dredging, dredge_mode (scheduled/criterion), dredge_volume_m3, "
        "dredge_disposal, dredge_crit_depth_m, dredge_dig_depth_m, friction_coefficient, "
        "rainfall_mm_per_day, rainfall_gridmet_window, evaporation_mm_per_day"
    ),
)


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name on the river, geocoded to the reach"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place"),
    Param("substance", door=doors.QUESTION, default="dye", consequence="scenario",
          desc="What was spilled - dye | oil/diesel/crude | sewage/E.coli | "
               "sediment/sand/silt | scour/erosion | graded/mixed-grain | dredging; "
               "the word picks the TELEMAC module family"),
    Param("release_coords", door=doors.USER, optional=True, user_lever=True,
          consequence="scenario",
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
          derived_when_absent=(
              "the bed is erodible only when the substance names scour / erosion / "
              "a mobile bed, or a graded mixture or dredging rule needs one"),
          desc="Force GAIA erodible-bed morphodynamics on (True) or off (False): a "
               "real bed with active bedload, so it scours and re-deposits"),
    Param("bed_thickness_m", door=doors.USER, optional=True, bounds=(0.05, 50.0),
          units="m", consequence="scenario",
          desc="Erodible bed only - depth of the erodible sediment stock"),
    Param("bedload_formula", door=doors.USER, optional=True, consequence="numerical",
          desc="Erodible bed only - GAIA bed-load law: 1=Meyer-Peter-Mueller "
               "(default), 2=Einstein-Brown, 7=van Rijn"),
    Param("morphological_factor", door=doors.USER, optional=True, bounds=(1.0, 100.0),
          user_lever=True, consequence="numerical",
          desc="Erodible bed only - amplifies bed change per hydraulic step so a "
               "short hydrograph yields a readable depth; a speed-up lever, not a rate"),
    Param("sediment_gradation", door=doors.USER, optional=True, consequence="scenario",
          desc="Multi-class graded sediment: a preset name (graded_sand | "
               "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list of "
               "[d50_um, fraction] pairs; forces a mobile bed so the mix can sort"),
    Param("dredging", door=doors.USER, optional=True, consequence="scenario",
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
          derived_when_absent="the spoil is not placed (dredge-only)",
          desc="Also place the dug spoil in a downstream disposal zone"),

    # -- advanced constitutive physics -------------------------------------- #
    Param("friction_coefficient", door=doors.USER, optional=True, bounds=(10.0, 90.0),
          user_lever=True, consequence="numerical",
          desc="Bed roughness under friction_law; unset keeps the deck's own value"),
    Param("friction_law", door=doors.USER, optional=True, consequence="numerical",
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
         .ladder("gridmet_domain_mean", "user_rate")),
)


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The river-tracer recipe. Pure: constructs the plan value, executes nothing.

    The gates come FIRST so every step and every producer downstream of them runs
    on the approved sheet - a step that had already consumed a value the form can
    revise would be exactly the contradiction the review exists to prevent.
    """
    return Workflow("telemac_river_dye", engine="telemac2d")[
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        Geocode.reach(p.location, p.bbox).named("reach"),
        ReachSeed(reach=Ref("reach"), rivers=Ref("rivers")).named("seed"),
        CarrierDischarge(seed=Ref("seed"), explicit=p.discharge_m3s,
                         event_time=p.event_time).named("carrier_discharge"),
        WriteDeck.telemac(
            reach=Ref("reach"), seed=Ref("seed"),
            carrier_discharge=Ref("carrier_discharge"), rain=Ref("rain"),
            release_coords=p.release_coords, reach_seed_coords=p.reach_seed_coords,
            substance=p.substance, reach_length_km=p.reach_length_km,
            channel_width_m=p.channel_width_m, sim_duration_s=p.sim_duration_s,
            spill_fraction=p.spill_fraction, spill_duration_s=p.spill_duration_s,
            dye_concentration_mgl=p.dye_concentration_mgl,
            source_q_m3s=p.source_q_m3s, mesh_resolution=p.mesh_resolution,
            mesh_resolution_m=p.mesh_resolution_m, bank_source=p.bank_source,
            output_interval_min=p.output_interval_min,
            wind_speed_mps=p.wind_speed_mps, wind_direction_deg=p.wind_direction_deg,
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
        ).named("deck"),
        Solve.telemac(deck=Ref("deck"), compute_class=p.compute_class).named("solve"),
        Products.dye(deck=Ref("deck"), solve=Ref("solve"),
                     carrier_discharge=Ref("carrier_discharge"))
                .named("plume")
                .chart("dye_concentration", builder=f"{_STEPS}.products.build_dye_chart"),
    ]


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


@register_tool(
    _TELEMAC_RIVER_DYE_METADATA,
    # readOnlyHint=False (runs a solver writing output COG + mesh artifacts),
    # openWorldHint=False (worker container + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + output keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac_river_dye(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    substance: str = "dye",
    contaminant: str | None = None,
    release_coords: tuple[float, float] | list[float] | None = None,
    release_lon: float | None = None,
    release_lat: float | None = None,
    spill_location_latlon: str | None = None,
    spill_fraction: float | None = None,
    spill_duration_s: float | None = None,
    dye_concentration_mgl: float | None = None,
    source_q_m3s: float | None = None,
    reach_length_km: float | None = None,
    sim_duration_s: float | None = None,
    channel_width_m: float | None = None,
    river_geometry_uri: str | None = None,
    mesh_resolution: str | None = None,
    mesh_resolution_m: float | None = None,
    discharge_m3s: float | None = None,
    event_time: str | None = None,
    decay_half_life_hours: float | None = None,
    decay_rate_per_day: float | None = None,
    grain_size_um: float | None = None,
    sediment_type: str | None = None,
    erodible_bed: bool | None = None,
    bed_thickness_m: float | None = None,
    bedload_formula: int | None = None,
    morphological_factor: float | None = None,
    sediment_gradation: list | str | None = None,
    dredging: bool | None = None,
    dredge_mode: str | None = None,
    dredge_volume_m3: float | None = None,
    dredge_disposal: bool | None = None,
    dredge_crit_depth_m: float | None = None,
    dredge_dig_depth_m: float | None = None,
    friction_coefficient: float | None = None,
    friction_law: int | None = None,
    velocity_diffusivity: float | None = None,
    tracer_diffusivity: float | None = None,
    wind_speed_mps: float | None = None,
    wind_direction_deg: float | None = None,
    rainfall_mm_per_day: float | None = None,
    evaporation_mm_per_day: float | None = None,
    rainfall_gridmet_window: str | None = None,
    compute_class: str | None = None,
    bank_source: str | None = None,
    output_interval_min: float | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    # The approve-mesh decision tail sets these (underscore -> stripped from the
    # model's schema): whether the CALL-provided release coords also seed the
    # reach, and the original pair the preview meshed from. They fold into
    # ``reach_seed_coords`` before any door.
    _release_seeds_reach: bool | None = None,
    _seed_release_lon: float | None = None,
    _seed_release_lat: float | None = None,
    **_extra_ignored: Any,
) -> TelemacDyeLayerURI | dict[str, Any]:
    supplied, err = _normalize(locals())
    if err is not None:
        return err
    try:
        p = await resolve_params(PARAMS, supplied)
        result = await interpret(
            plan(p, None), p, PARAMS, DATA,
            input_mode=input_mode, resume=not restart_clean,
        )
    except asyncio.CancelledError:
        raise
    except DeclarativeError as exc:
        logger.warning("telemac_river_dye %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": _with_notes(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            # The banks / degenerate-reach gates carry .suggestions the adapter
            # harvests off the RAISED exception, so the model can retry with
            # corrected args. Flattening them destroys that channel.
            raise
        logger.exception("telemac_river_dye unexpected failure")
        return {"status": "error", "error_code": "TELEMAC_INTERNAL_ERROR",
                "error_message": _with_notes(exc)}

    layer = result.value
    update: dict[str, Any] = {
        "synthetic_inputs": merge_provenance(layer.synthetic_inputs or [],
                                             result.entries),
    }
    if result.notes:
        parts = [layer.fallback_note] if layer.fallback_note else []
        parts += [f"NOTE: {n}" for n in result.notes]
        update["fallback_note"] = " ".join(parts)
    layer = layer.model_copy(update=update)

    run_id = (result.results.get("solve") or {}).get("run_id")
    await persist_run_products(run_id, charts=result.charts,
                               metrics=_physical_answer(layer))
    logger.info(
        "telemac_river_dye complete layer_id=%s dye_cmax_mgl=%.4g plume_reach_m=%s "
        "active_frames=%s executed=%s replayed=%s notes=%s",
        layer.layer_id, layer.dye_cmax_mgl, layer.plume_reach_m,
        layer.active_frames, result.executed, result.replayed, result.notes,
    )
    return layer


def _physical_answer(layer: TelemacDyeLayerURI) -> dict[str, Any]:
    """The run's ANSWER, as the numbers a reader has to be able to check.

    Persisted beside the chart spec so verification cites the run's own figures
    rather than recomputing them from the raster. ``discharge_m3s``/``discharge_note``
    ride the carrier-discharge provenance row, so the RESOLVED NWM cycle (never a
    bare "latest") is pinned here too.
    """
    disc = next((r for r in (layer.synthetic_inputs or [])
                if r.param == "discharge_m3s"), None)
    return {
        "dye_cmax_mgl": layer.dye_cmax_mgl,
        "dye_peak_time_s": layer.dye_peak_time_s,
        "plume_reach_m": layer.plume_reach_m,
        "active_frames": layer.active_frames,
        "max_deposition_mm": layer.max_deposition_mm,
        "max_scour_mm": layer.max_scour_mm,
        "deposited_mass_kg": layer.deposited_mass_kg,
        "deposit_fraction": layer.deposit_fraction,
        "sediment_surface_d50_range_um": layer.sediment_surface_d50_range_um,
        "mesh_size_m": layer.mesh_size_m,
        "mesh_node_estimate": layer.mesh_node_estimate,
        "layer_uri": layer.uri,
        "discharge_m3s": disc.value if disc else None,
        "discharge_note": disc.note if disc else None,
    }


def _with_notes(exc: BaseException) -> str:
    """The failure, plus whatever auxiliary products the run also lost on the way."""
    notes = getattr(exc, "__notes__", ()) or ()
    return " ".join([str(exc), *notes])


def _release_point(args: dict[str, Any]) -> Any:
    """The release point from whichever field the caller used.

    Models split the same value across three shapes - an explicit pair, split
    lon/lat, or one "lat,lon" string - and dropping the ones the signature does
    not name is the silent-swallow class.
    """
    if args.get("release_coords") is not None:
        return args["release_coords"]
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
    return None if (lat is None and lon is None) else [lon, lat]


def _reach_seed_point(args: dict[str, Any], release: Any) -> Any:
    """Which point the WORKER resolves the reach centerline from.

    The approve-mesh decision tail is the only caller that separates the two:
    call-provided release coords seed the reach, while a gate-picked click moves
    the SOURCE only - re-seeding from the click would silently mesh a different
    reach than the one the user approved.
    """
    if args.get("_seed_release_lon") is not None \
            or args.get("_seed_release_lat") is not None:
        return [args.get("_seed_release_lon"), args.get("_seed_release_lat")]
    return None if args.get("_release_seeds_reach") is False else release


def _normalize(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Coerce the wire args to the door-1 sheet: exactly one AOI, one release point."""
    location, bbox = args.get("location"), args.get("bbox")
    coerced: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            # A non-numeric string bbox is almost always a PLACE NAME - shift it
            # into location rather than dead-ending the call.
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                logger.warning("telemac_river_dye: bbox %r is a place name - using "
                               "as location", bbox)
                location, bbox = bbox, None
            else:
                return {}, {"status": "error", "error_code": "TELEMAC_PARAMS_INVALID",
                            "error_message": (
                                "invalid bbox (expected 4 numbers min_lon,min_lat,"
                                f"max_lon,max_lat): {bbox!r}")}
        else:
            coerced = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced is None:
        return {}, {"status": "error", "error_code": "TELEMAC_PARAMS_INCOMPLETE",
                    "error_message": (
                        "telemac_river_dye needs a place `location` (geocoded) or an "
                        "explicit `bbox` AOI. For a natural prompt like 'dye spill in "
                        "the river near <place>', pass location='<place>'.")}
    if has_loc and coerced is not None:
        # LOCATION wins: a model that fabricates a bbox alongside a real place name
        # has been observed to put it on open water at a river MOUTH, and the
        # geocoded place is ground truth. A user-drawn AOI arrives via case state.
        logger.warning("telemac_river_dye: both location and bbox supplied - dropping "
                       "the bbox %s in favour of geocoding %r", coerced, location)
        coerced = None

    try:
        release = coerce_lonlat_point(_release_point(args))
        seed_point = coerce_lonlat_point(_reach_seed_point(args, release))
        event_time = coerce_event_time(args.get("event_time"))
    except TelemacDyeScenarioError as exc:
        return {}, {"status": "error", "error_code": exc.error_code,
                    "error_message": str(exc)}

    substance = sanitize_substance(args.get("substance"))
    contaminant = args.get("contaminant")
    if contaminant:
        # Models split intent across the two fields - substance="dye" AND
        # contaminant="crude oil" - so an oil spill silently ran the tracer class.
        # Any NON-tracer contaminant class wins over a tracer-class substance.
        cont = sanitize_substance(contaminant, default="")
        if cont and classify_substance(substance)[0] == "tracer" \
                and classify_substance(cont)[0] != "tracer":
            logger.info("telemac_river_dye: substance %r is tracer-class but "
                        "contaminant %r is %s-family - classifying by contaminant",
                        substance, cont, classify_substance(cont)[0])
            substance = cont

    compute = str(args.get("compute_class") or "medium").strip().lower()
    if compute not in _ALLOWED_COMPUTE:
        logger.warning("telemac_river_dye: unknown compute_class %r coerced to "
                       "'medium'", args.get("compute_class"))
        compute = "medium"

    declared = {p.name for p in PARAMS}
    supplied = {k: v for k, v in args.items() if k in declared and v is not None}
    supplied.update({
        "location": location if has_loc else None,
        "bbox": coerced,
        "release_coords": release,
        "reach_seed_coords": seed_point,
        "event_time": event_time,
        "substance": substance,
        "compute_class": compute,
    })
    # A bearing WRAPS; it does not clamp, so the modulo happens before the door.
    if args.get("wind_direction_deg") is not None:
        try:
            supplied["wind_direction_deg"] = float(args["wind_direction_deg"]) % 360.0
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in supplied.items() if v is not None}, None


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

#: The full sheet is what the MODEL needs (it fills the params); the routing view
#: is what a surface that only helps someone CHOOSE the tool needs, and it fits
#: the truncation budget by construction.
telemac_river_dye.__doc__ = render_docstring(**_DOC)
telemac_river_dye.routing_doc = render_docstring(**_DOC, view="routing")
