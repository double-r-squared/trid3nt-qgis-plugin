"""MODFLOW GWE heat-transport composers - the geothermal leg.

Two LLM-facing question-class tools ride ONE ``gwe_thermal`` archetype (the heat
twin of ``modflow_contaminant_plume``), a GWF+GWE dual-model deck coupled through
a ``GWF6-GWE6`` exchange with a warm-water injection WEL carrying an AUXILIARY
TEMPERATURE mapped by the GWE SSM onto the energy-transport source:

  * ``modflow_thermal_plume`` (mode = injection_plume) - a continuous warm-water
    injection well drives a downgradient THERMAL PLUME. Primary deliverable: the
    peak-temperature-excess COG (radial conductive-advective heat transport,
    thermal-pollution / reinjection / generic thermal-source questions).
  * ``modflow_thermal_storage`` (mode = ates) - seasonal charge/recover cycling
    for AQUIFER THERMAL ENERGY STORAGE. Primary deliverable: the per-cycle
    recovery-efficiency CHART (how much stored heat the well recovers each cycle).

DISTINCTNESS CALL (completion): two thin tools over ONE shared archetype +
postprocess, NOT one tool with a mode knob. Justification mirrors the
``capture_zone`` / ``wellhead_protection`` precedent (two registered tools over one
shared PRT archetype + ``postprocess_capture_zone``, differing only in framing +
default tiers): (1) the naming law = question class, and "aquifer thermal energy
storage recovery" is a distinct question from "thermal plume spread"; (2) the
PRIMARY deliverable differs (a raster plume footprint vs a recovery-efficiency
chart); (3) distinct retrieval corpora route cleanly to distinct tools. Both share
the ``compose_thermal_scenario`` core (DRY) and both dispatch ``archetype="gwe_thermal"``
with the appropriate ``gwe_mode``.

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** The injection-well SITE is a user input -- a
  run with neither ``location`` nor ``aoi_latlon`` returns a typed
  ``USER_INPUT_REQUIRED`` failure. The thermal properties are LABELED DEMO DEFAULTS
  routed through the input-review gate (no thermal-property fetcher in v1).
- **10. Minimal parameter surface.** Intent (the site + inject temperature + rate,
  and for ATES the cycle count) is exposed; the grid + thermal properties are
  derived/demo-defaulted.
- **0231. Input-layer parity.** The injection-well site surfaces as a role="input"
  Case POINT (visible-by-default) so the user sees WHERE the heat enters.

PRECISION CAVEAT (Invariant 1): planning-grade heat-transport envelope on a 50 m
demo grid with DEMO thermal properties (heat capacities, densities, thermal
conductivities, ambient temperature), NOT a calibrated geothermal forecast.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel, SyntheticInput
from trid3nt_contracts.modflow_contracts import (
    MODFLOWRunArgs,
    ThermalPlumeLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.modflow._input_review import (
    AquiferRefusal,
    gate_and_stamp_modflow_inputs,
    resolve_and_gate_aquifer,
    thermal_demo_review_entries,
)
from trid3nt_server.workflows.modflow._template_card import TemplateCard
# Reuse the shared AOI-resolve + archetype-run seams (one implementation, all
# archetypes) from the sustainable_yield composer.
from trid3nt_server.workflows.modflow.sustainable_yield.sustainable_yield import (
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.modflow.thermal_plume.thermal_plume"
)

__all__ = [
    "ThermalScenarioResult",
    "compose_thermal_scenario",
    "modflow_thermal_plume",
    "modflow_thermal_storage",
    "ThermalScenarioError",
    "ThermalInputError",
]

# The undisturbed-aquifer demo ambient temperature (degC) mirrored from the
# adapter (GWE_AMBIENT_TEMPERATURE_C) for the narration default; the adapter
# remains the authority (it stamps the real value on the layer).
_DEMO_AMBIENT_C: float = 10.0
_DEMO_INJECTION_DELTA_C: float = 30.0
_DEMO_SOLID_CONDUCTIVITY_WMC: float = 2.5


class ThermalScenarioResult(GraceModel):
    """Return type for the thermal composers.

    Bundles the temperature layer + derived args + a narration summary. Invariant
    1: every narrated number is a typed field -- ``thermal_layer`` carries
    ``peak_temperature_c`` / ``peak_excess_temperature_c`` / ``ambient_temperature_c``
    / ``thermal_plume_area_km2`` (+ ``recovery_efficiency`` /
    ``recovery_efficiency_series`` for ATES).
    """

    schema_version: str = "v1"

    thermal_layer: ThermalPlumeLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class ThermalScenarioError(RuntimeError):
    """Base class for the thermal composer failures."""

    error_code: str = "THERMAL_SCENARIO_ERROR"
    retryable: bool = False


class ThermalInputError(ThermalScenarioError):
    """Caller supplied invalid / missing site or thermal input (honesty gate).

    Invariant 9: the injection-well site is never fabricated. A run with neither a
    ``location`` nor an ``aoi_latlon`` raises this so the agent asks for the real
    site.
    """

    error_code = "THERMAL_INPUT_INVALID"


async def _emit_well_context_point(
    layer: ThermalPlumeLayerURI,
    lat: float,
    lon: float,
    *,
    mode: str,
    inject_c: float,
    ambient_c: float,
    pipeline_emitter: Any | None,
) -> None:
    """BEST-EFFORT: surface the injection-well SITE as a role="input" POINT (0231).

    A single FlatGeobuf POINT at the well ``(lat, lon)`` carrying the mode +
    temperatures as attributes, so the user sees WHERE the heat enters (the
    visible-by-default input-layer parity ruling). Never raises / never fails the
    solve. Rides ``publish_input_layer`` (forces role + strips the competing
    zoom-to).
    """
    import asyncio

    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]

        from trid3nt_contracts.execution import LayerURI
        from trid3nt_server.workflows.modflow.postprocess_modflow import (
            _upload_fgb,
        )

        run_id = str(layer.layer_id).replace("thermal-plume-", "") or "thermal"
        props = [{
            "feature_type": "injection_well",
            "gwe_mode": mode,
            "injection_temperature_c": round(float(inject_c), 3),
            "ambient_temperature_c": round(float(ambient_c), 3),
            "run_id": run_id,
        }]
        gdf = await asyncio.to_thread(
            gpd.GeoDataFrame,
            props,
            geometry=[Point(float(lon), float(lat))],
            crs="EPSG:4326",
        )
        tmp = Path(tempfile.mkdtemp(prefix="gwe_well_")) / "thermal_well_4326.fgb"
        await asyncio.to_thread(
            gdf.to_file, str(tmp), driver="FlatGeobuf", engine="pyogrio"
        )
        uri = await asyncio.to_thread(
            _upload_fgb, tmp, run_id, None, fgb_filename="thermal_well_4326.fgb"
        )
        well_layer = LayerURI(
            layer_id=f"thermal-well-{run_id}",
            name="Injection well (heat source)",
            layer_type="vector",
            uri=uri,
            style_preset="usgs_groundwater",
            role="input",
            bbox=None,
        )
        await publish_input_layer(
            pipeline_emitter or current_emitter(), well_layer, role="input"
        )
    except Exception as exc:  # noqa: BLE001 -- context layer is best-effort
        logger.warning(
            "thermal composer: building the well context point failed (non-fatal): %s",
            exc,
        )


async def compose_thermal_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    mode: str = "injection_plume",
    injection_temperature_c: float | None = None,
    ambient_temperature_c: float | None = None,
    injection_rate_m3_day: float | None = None,
    duration_days: float | None = None,
    n_cycles: int | None = None,
    thermal_conductivity_solid_wmc: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> ThermalScenarioResult:
    """Compose a site -> MODFLOW GWF+GWE heat transport -> ``ThermalPlumeLayerURI``.

    Shared core of ``modflow_thermal_plume`` (mode=injection_plume) and
    ``modflow_thermal_storage`` (mode=ates). Resolves the injection-well site
    (Invariant 9 honesty gate), assembles the ``gwe_thermal`` run args, runs the
    dual-model solve, loads the temperature-excess COG, surfaces the well site as a
    role="input" POINT (0231), emits the ATES recovery chart when present, and
    stamps the LOUD thermal demo-default provenance through the input-review gate.

    Raises:
        ThermalInputError: missing/invalid site or thermal input (Invariant 9 gate).
        ThermalScenarioError: a required step (geocode / solver) failed.
    """
    if mode not in ("injection_plume", "ates"):
        raise ThermalInputError(
            f"unknown thermal mode {mode!r} (expected injection_plume or ates)"
        )

    # --- Validate scalar thermal inputs (typed, before the geocode) ---------- #
    try:
        if injection_rate_m3_day is not None and float(injection_rate_m3_day) <= 0.0:
            raise ValueError("injection_rate_m3_day must be > 0")
        if (
            injection_temperature_c is not None
            and ambient_temperature_c is not None
            and float(injection_temperature_c) <= float(ambient_temperature_c)
        ):
            raise ValueError(
                "injection_temperature_c must exceed ambient_temperature_c "
                "(a warm-water injection drives the plume/ATES)"
            )
        if mode == "ates":
            if n_cycles is None:
                raise ValueError("ates mode requires n_cycles (>= 1)")
            if int(n_cycles) < 1:
                raise ValueError("n_cycles must be >= 1")
    except ThermalInputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ThermalInputError(f"invalid thermal parameter: {exc}") from exc

    # declare the planned internal-tool count: geocode (only for a place string) +
    # run_modflow_archetype_job (always).
    _planned = 1 + (1 if (location and location.strip()) else 0)
    begin_substeps(current_emitter(), _planned)

    # Honesty gate (Invariant 9): the injection-well site is never fabricated.
    try:
        lat, lon, location_name = await _resolve_aoi_point(
            location, aoi_latlon, pipeline_emitter=pipeline_emitter
        )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "SustainableYieldInputError":
            raise ThermalInputError(
                "thermal modeling requires an injection-well site: supply exactly "
                "one of location (a place name) or aoi_latlon (an explicit lat/lon). "
                "The site is a user input and is never invented; ask the user."
            ) from exc
        raise

    # --- Aquifer + thermal properties: resolve at the well or REFUSE (law 9) --- #
    # K/porosity are SoilGrids-derived; the undisturbed aquifer temperature and the
    # aquifer-grain thermal conductivity have NO fetcher (row 4) - required inputs
    # that REFUSE in auto rather than run on an invented demo value. Gate pre-solve.
    _thermal_extra: list[SyntheticInput] = []
    if ambient_temperature_c is None:
        _thermal_extra.append(SyntheticInput(
            param="ambient_temperature_c", value=None, units="degC",
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note="undisturbed aquifer temperature is required and has no fetcher in "
                 "v1; supply ambient_temperature_c (or approve in user_gated mode).",
        ))
    if thermal_conductivity_solid_wmc is None:
        _thermal_extra.append(SyntheticInput(
            param="thermal_conductivity_solid_wmc", value=None, units="W/(m*degC)",
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note="aquifer-grain thermal conductivity is required (literature-range, "
                 "not fetchable); supply it (or approve in user_gated mode).",
        ))
    try:
        resolution = await resolve_and_gate_aquifer(
            tool_name=("modflow_thermal_storage" if mode == "ates"
                       else "modflow_thermal_plume"),
            lat=lat, lon=lon, aquifer_k_ms=aquifer_k_ms, porosity=porosity,
            extra_entries=_thermal_extra,
        )
    except AquiferRefusal as exc:
        raise ThermalScenarioError(str(exc)) from exc
    eff_k = float(resolution.k_ms)
    eff_porosity = float(resolution.porosity)

    # Duration: injection_plume defaults to a 120-day plume horizon; ates derives
    # a full-year-per-cycle seasonal horizon (360 days/cycle) when unset.
    if duration_days is not None:
        _duration = float(duration_days)
    elif mode == "ates":
        _duration = 360.0 * float(int(n_cycles))
    else:
        _duration = 120.0

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="temperature",
            release_rate_kg_s=1.0,  # placeholder: ignored (GWE energy-transport, not a mass SRC)
            duration_days=_duration,
            aquifer_k_ms=eff_k,
            porosity=eff_porosity,
            archetype="gwe_thermal",
            gwe_mode=mode,
            injection_temperature_c=injection_temperature_c,
            ambient_temperature_c=ambient_temperature_c,
            injection_rate_m3_day=injection_rate_m3_day,
            n_cycles=(int(n_cycles) if (mode == "ates" and n_cycles is not None) else None),
            thermal_conductivity_solid_wmc=thermal_conductivity_solid_wmc,
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise ThermalInputError(f"invalid thermal run arguments: {exc}") from exc

    inject_label = (
        f"{float(injection_temperature_c):g} degC"
        if injection_temperature_c is not None
        else f"ambient+{_DEMO_INJECTION_DELTA_C:g} degC (demo)"
    )
    tool_label = (
        f"Model aquifer thermal energy storage [{int(n_cycles)} cycles, inject {inject_label}]"
        if mode == "ates"
        else f"Model thermal plume [inject {inject_label}]"
    )
    layer: ThermalPlumeLayerURI = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=tool_label,
        expected_type=ThermalPlumeLayerURI,
        error_code="THERMAL_RUN_FAILED",
        scenario_error=ThermalScenarioError,
    )

    peak_excess = getattr(layer, "peak_excess_temperature_c", 0.0)
    peak_abs = getattr(layer, "peak_temperature_c", 0.0)
    ambient = getattr(layer, "ambient_temperature_c", _DEMO_AMBIENT_C)
    inject_c = (
        float(injection_temperature_c)
        if injection_temperature_c is not None
        else float(ambient) + _DEMO_INJECTION_DELTA_C
    )
    recovery = getattr(layer, "recovery_efficiency", None)
    recovery_series = getattr(layer, "recovery_efficiency_series", None)

    # 0231: surface the injection-well site as a role="input" POINT (best-effort).
    await _emit_well_context_point(
        layer, lat, lon, mode=mode, inject_c=inject_c, ambient_c=float(ambient),
        pipeline_emitter=pipeline_emitter,
    )

    # ATES: emit the recovery-efficiency chart the postprocess built + stashed.
    chart = getattr(layer, "_chart_payload", None)
    if chart is not None:
        await emit_chart_payloads(chart)

    # --- Emit-on-solve: the temperature-excess animation (ADR 0284) ----------
    # postprocess_gwe_thermal wrote the peak + every saved-step temperature-excess
    # COG to outputs.json host-side + stashed the run_id. The SEAM owns the
    # TEMPORAL FRAMES ONLY (frames_only -> the typed peak layer stays
    # composer-built). Best-effort: absent manifest / no emitter -> peak-only.
    from trid3nt_server.workflows.modflow._frame_emit import (
        read_and_emit_modflow_frames,
    )

    await read_and_emit_modflow_frames(
        current_emitter(),
        run_id=getattr(layer, "_run_id", None),
        bbox=getattr(layer, "bbox", None),
    )

    caveat = (
        "The aquifer thermal properties (heat capacities, densities, thermal "
        "conductivities) and the ambient temperature are DEMO defaults (no site "
        "thermal-property fetcher in v1). This is a planning-grade heat-transport "
        "envelope on a 50 m demo grid, NOT a calibrated geothermal forecast."
    )
    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "gwe_mode": mode,
        "injection_temperature_c": inject_c,
        "ambient_temperature_c": ambient,
        "injection_rate_m3_day": run_args.injection_rate_m3_day,
        "n_cycles": (int(n_cycles) if mode == "ates" and n_cycles is not None else None),
        "duration_days": _duration,
    }
    summary: dict[str, Any] = {
        "location_name": location_name,
        "gwe_mode": mode,
        "peak_temperature_c": peak_abs,
        "peak_excess_temperature_c": peak_excess,
        "ambient_temperature_c": ambient,
        "thermal_plume_area_km2": getattr(layer, "thermal_plume_area_km2", 0.0),
        "demo_thermal_caveat": caveat,
    }
    if mode == "ates":
        summary["recovery_efficiency"] = recovery
        summary["recovery_efficiency_series"] = recovery_series
        if recovery_series:
            summary["recovery_note"] = (
                f"recovery efficiency {float(recovery_series[0]) * 100:.0f}% -> "
                f"{float(recovery_series[-1]) * 100:.0f}% across "
                f"{len(recovery_series)} cycle(s) (the aquifer thermal buffer pre-warms)"
            )
    logger.info(
        "thermal scenario complete mode=%s location=%r peak_excess_c=%.4g "
        "peak_temp_c=%.4g recovery=%s",
        mode, location_name, peak_excess, peak_abs, recovery,
    )

    # structured thermal demo-default provenance through the review gate,
    # stamped onto the layer envelope (the prose caveat stays on the summary).
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name=(
            "modflow_thermal_storage" if mode == "ates" else "modflow_thermal_plume"
        ),
        layer=layer,
        entries=list(resolution.entries) + thermal_demo_review_entries(
            ambient_temperature_c=float(ambient),
            ambient_user_supplied=(ambient_temperature_c is not None),
            injection_temperature_c=float(inject_c),
            injection_user_supplied=(injection_temperature_c is not None),
            thermal_conductivity_solid_wmc=(
                float(thermal_conductivity_solid_wmc)
                if thermal_conductivity_solid_wmc is not None
                else _DEMO_SOLID_CONDUCTIVITY_WMC
            ),
            conductivity_user_supplied=(thermal_conductivity_solid_wmc is not None),
            note=caveat,
        ),
        params={
            "injection_temperature_c": injection_temperature_c,
            "ambient_temperature_c": ambient_temperature_c,
        },
    )
    if _review.cancelled:
        raise ThermalScenarioError(
            f"thermal input review {_review.cancel_reason or 'not approved'}"
        )
    return ThermalScenarioResult(
        thermal_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic tools (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_PLUME_TEMPLATE_CARD = TemplateCard(
    question="how injected warm water spreads as a downgradient THERMAL PLUME in an aquifer (GWE heat transport)",
    required_inputs=["location (or aoi_latlon)"],
    knobs="injection_temperature_c, injection_rate_m3_day, ambient_temperature_c, thermal_conductivity_solid_wmc, duration_days",
)

_PLUME_METADATA = AtomicToolMetadata(
    name="modflow_thermal_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _PLUME_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_thermal_plume(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    injection_temperature_c: float | None = None,
    injection_rate_m3_day: float | None = None,
    ambient_temperature_c: float | None = None,
    thermal_conductivity_solid_wmc: float | None = None,
    duration_days: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a groundwater THERMAL PLUME from warm-water injection (MODFLOW 6 GWE).

    Fidelity: MODFLOW 6 local planning-grade heat-transport envelope on a 50 m demo
    grid (saturated K/porosity SoilGrids-derived at the AOI or refused; ambient
    temperature + grain thermal conductivity refuse in auto unless supplied, law 9), NOT a calibrated geothermal forecast. Off-scope:
    seasonal aquifer thermal energy STORAGE recovery -> modflow_thermal_storage; a
    saturated CONTAMINANT plume -> modflow_contaminant_plume; pumping drawdown ->
    modflow_sustainable_yield.

    Builds a MODFLOW 6 GWF+GWE (energy-transport) dual-model deck at the injection
    site -- a continuous warm-water injection well carrying an AUXILIARY TEMPERATURE,
    coupled through a GWF6-GWE6 exchange -- runs it, and produces:

      * A RASTER MAP layer: the peak temperature EXCESS above the undisturbed
        aquifer (degC), rendered as a downgradient warm plume over the basemap.
      * HEADLINE SCALARS: ``peak_excess_temperature_c`` (peak heating above ambient)
        + ``thermal_plume_area_km2``. Narrate these as the key physical result.
      * The injection-well site as a role="input" context POINT (visible-by-default).

    Use this when the user asks how injected warm water / thermal-pollution / reinjected
    cooling water / a heat source spreads through an aquifer, the extent of a thermal
    plume, or radial conductive-advective heat transport in groundwater.

    Do NOT use this for:
        - Seasonal aquifer thermal energy storage recovery efficiency (use
          ``modflow_thermal_storage``).
        - A saturated CONTAMINANT plume (use ``modflow_contaminant_plume``).
        - A pumping-well drawdown (use ``modflow_sustainable_yield``).

    PRECISION CAVEAT: DEMO thermal properties (heat capacities, densities, thermal
    conductivities) + a demo ambient temperature on a 50 m grid. Narrate the peak
    temperature excess as a qualitative screening estimate, NOT a calibrated forecast.

    Params:
        location: place name (geocoded to the injection site). Supply this OR aoi_latlon.
        aoi_latlon: explicit ``(lat, lon)`` injection site.
        injection_temperature_c: injected-water temperature, degC. None -> ambient
            + 30 degC demo default (narrated).
        injection_rate_m3_day: warm-water injection rate, m^3/day. None -> demo default.
        ambient_temperature_c: undisturbed aquifer temperature, degC. None -> 10 degC demo.
        thermal_conductivity_solid_wmc: aquifer-grain thermal conductivity, W/(m*degC).
            None -> 2.5 W/(m*degC) demo default.
        duration_days: plume-transport horizon, days. None -> 120 d.
        aquifer_k_ms / porosity: aquifer hydraulics (demo defaults when None).
        compute_class: compute class. This archetype runs LOCAL-ONLY.

    Returns:
        On success: a ``ThermalScenarioResult`` JSON dict with ``thermal_layer`` (a
        ``ThermalPlumeLayerURI`` carrying the temperature scalars), ``derived_params``,
        and ``summary``. On a recoverable failure (incl. a missing site) a typed error.

    ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await compose_thermal_scenario(
            location=location,
            aoi_latlon=aoi,
            mode="injection_plume",
            injection_temperature_c=(
                float(injection_temperature_c)
                if injection_temperature_c is not None
                else None
            ),
            ambient_temperature_c=(
                float(ambient_temperature_c)
                if ambient_temperature_c is not None
                else None
            ),
            injection_rate_m3_day=(
                float(injection_rate_m3_day)
                if injection_rate_m3_day is not None
                else None
            ),
            thermal_conductivity_solid_wmc=(
                float(thermal_conductivity_solid_wmc)
                if thermal_conductivity_solid_wmc is not None
                else None
            ),
            duration_days=(float(duration_days) if duration_days is not None else None),
            aquifer_k_ms=(float(aquifer_k_ms) if aquifer_k_ms is not None else None),
            porosity=(float(porosity) if porosity is not None else None),
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except ThermalInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except ThermalScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "THERMAL_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")


_STORAGE_TEMPLATE_CARD = TemplateCard(
    question="the seasonal recovery efficiency of AQUIFER THERMAL ENERGY STORAGE (ATES charge/recover cycling, GWE heat transport)",
    required_inputs=["location (or aoi_latlon)", "n_cycles"],
    knobs="n_cycles, injection_temperature_c, injection_rate_m3_day, ambient_temperature_c, thermal_conductivity_solid_wmc",
)

_STORAGE_METADATA = AtomicToolMetadata(
    name="modflow_thermal_storage",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _STORAGE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_thermal_storage(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    n_cycles: int | None = None,
    injection_temperature_c: float | None = None,
    injection_rate_m3_day: float | None = None,
    ambient_temperature_c: float | None = None,
    thermal_conductivity_solid_wmc: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model AQUIFER THERMAL ENERGY STORAGE recovery efficiency (MODFLOW 6 GWE, ATES).

    Fidelity: MODFLOW 6 local planning-grade heat-transport envelope on a 50 m demo
    grid (saturated K/porosity SoilGrids-derived at the AOI or refused; ambient
    temperature + grain thermal conductivity refuse in auto unless supplied, law 9), NOT a calibrated ATES design tool. Off-scope: a one-way
    thermal PLUME from continuous injection -> modflow_thermal_plume; a saturated
    contaminant plume -> modflow_contaminant_plume.

    Builds a MODFLOW 6 GWF+GWE dual-model deck at the site and runs ``n_cycles`` of
    (inject warm season) then (extract season) at the SAME well -- seasonal aquifer
    thermal energy storage charge/recover -- and produces:

      * A CHART (primary deliverable): per-cycle RECOVERY EFFICIENCY (the fraction of
        the injected thermal lift the well recovers each cycle), which RISES as the
        aquifer thermal buffer pre-warms across cycles.
      * A RASTER MAP layer: the peak temperature excess (the charged ATES footprint).
      * HEADLINE SCALAR: ``recovery_efficiency`` (last cycle) + the per-cycle
        ``recovery_efficiency_series``. Narrate the recovery trend as the key result.
      * The injection/recovery well as a role="input" context POINT (visible-by-default).

    Use this when the user asks about aquifer thermal energy storage (ATES), seasonal
    storage of heat/cooling in groundwater for district heating/cooling, borehole or
    well thermal energy storage recovery efficiency, or how much stored heat a
    seasonal geothermal storage well recovers.

    Do NOT use this for:
        - A one-way thermal plume from continuous injection (use ``modflow_thermal_plume``).
        - A saturated contaminant plume (use ``modflow_contaminant_plume``).

    PRECISION CAVEAT: DEMO thermal properties on a 50 m grid; the recovery efficiency
    is a qualitative screening estimate, NOT a calibrated ATES design number.

    Params:
        location: place name (geocoded to the storage-well site). Supply this OR aoi_latlon.
        aoi_latlon: explicit ``(lat, lon)`` storage-well site.
        n_cycles: number of seasonal inject/recover cycles (>= 1) -- REQUIRED.
        injection_temperature_c: injected-water temperature, degC. None -> ambient
            + 30 degC demo default.
        injection_rate_m3_day: seasonal injection/recovery rate, m^3/day. None -> demo.
        ambient_temperature_c: undisturbed aquifer temperature, degC. None -> 10 degC demo.
        thermal_conductivity_solid_wmc: aquifer-grain thermal conductivity, W/(m*degC).
            None -> 2.5 W/(m*degC) demo default (higher -> more thermal loss -> lower recovery).
        aquifer_k_ms / porosity: aquifer hydraulics (demo defaults when None).
        compute_class: compute class. This archetype runs LOCAL-ONLY.

    Returns:
        On success: a ``ThermalScenarioResult`` JSON dict with ``thermal_layer`` (a
        ``ThermalPlumeLayerURI`` carrying ``recovery_efficiency`` +
        ``recovery_efficiency_series``), ``derived_params``, and ``summary``. On a
        recoverable failure (incl. a missing site or n_cycles) a typed error.

    ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await compose_thermal_scenario(
            location=location,
            aoi_latlon=aoi,
            mode="ates",
            n_cycles=(int(n_cycles) if n_cycles is not None else None),
            injection_temperature_c=(
                float(injection_temperature_c)
                if injection_temperature_c is not None
                else None
            ),
            ambient_temperature_c=(
                float(ambient_temperature_c)
                if ambient_temperature_c is not None
                else None
            ),
            injection_rate_m3_day=(
                float(injection_rate_m3_day)
                if injection_rate_m3_day is not None
                else None
            ),
            thermal_conductivity_solid_wmc=(
                float(thermal_conductivity_solid_wmc)
                if thermal_conductivity_solid_wmc is not None
                else None
            ),
            aquifer_k_ms=(float(aquifer_k_ms) if aquifer_k_ms is not None else None),
            porosity=(float(porosity) if porosity is not None else None),
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except ThermalInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except ThermalScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "THERMAL_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
