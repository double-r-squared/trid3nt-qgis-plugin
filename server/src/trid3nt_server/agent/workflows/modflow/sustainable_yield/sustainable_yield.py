"""the composer  -  MODFLOW pumping-drawdown composer.

The end-to-end higher-order workflow for the MODFLOW
``sustainable_yield`` archetype: it turns a place (or AOI point) + a pumping
well (location + extraction rate) into a rendered drawdown-cone layer  -  the cone
of depression a sustained extraction draws down around the well. It mirrors the
chain shape of the composer and the point-spill
groundwater-contamination composer, minus the contaminant: this is a GWF-only
transient flow run, no solute transport.

Canonical real-world pipeline mirrored here (a sustainable-yield / well-
interference analysis, the MODFLOW analogue of a Theis aquifer-test drawdown):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the well location + extraction rate (NEVER
           fabricated  -  a missing well is a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="sustainable_yield", well, rate, ...)
        -> run_modflow_archetype_job (GWF transient deck -> mf6 -> drawdown)
        -> DrawdownLayerURI (max_drawdown_m + the at-well head-decline series)

Invariants:
- **1. Determinism boundary: preserves.** Every narrated number comes from a
  typed field  -  the derived-args dict + the ``DrawdownLayerURI`` scalars. No LLM
  call anywhere in this module.
- **2. Deterministic workflows: preserves.** Straight-line Python composition
  over registered atomic tools + the archetype run-tool; typed-exception
  handling at the boundary.
- **8. Cancellation is first-class: preserves.** Every ``await`` is a
  cancel-propagation site; ``asyncio.CancelledError`` bubbles untouched.
- **9. No fabricated model inputs.** A ``sustainable_yield`` run with no well
  location OR no pumping rate returns a typed ``USER_INPUT_REQUIRED`` failed
  envelope rather than inventing a well  -  a "modeled" envelope with empty layers
  never reads ok (the honesty floor).
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (the place + the well + the rate); the grid, the demo aquifer K / porosity /
  storage are derived defaults, not user-supplied.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    DrawdownLayerURI,
    MODFLOWRunArgs,
    StreamReachLayerURI,
    SubsidenceLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.workflows.modflow._input_review import (
    aquifer_k_review_entry,
    gate_and_stamp_modflow_inputs,
)
from trid3nt_server.emission.layer_uri_emit import emit_layer_uri
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from trid3nt_server.agent.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.agent.workflows.modflow._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.modflow.sustainable_yield.sustainable_yield"
)

__all__ = [
    "SustainableYieldResult",
    "StreamDepletionResult",
    "SubsidenceResult",
    "model_sustainable_yield_scenario",
    "modflow_sustainable_yield",
    "SustainableYieldScenarioError",
    "SustainableYieldInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope (agent-local; mirrors RiverSeepageResult)
# --------------------------------------------------------------------------- #


class SustainableYieldResult(GraceModel):
    """Return type for the composer.

    Bundles the drawdown layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``drawdown_layer``
    carries ``max_drawdown_m`` + the timeseries; ``summary`` mirrors them.
    """

    schema_version: str = "v1"

    drawdown_layer: DrawdownLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class StreamDepletionResult(GraceModel):
    """Return type for the SFR-coupled ``stream_depletion`` composer branch.

    "How does pumping this well affect the river?" - the routed SFR reach vector
    (PRIMARY layer) + the derived args + a narration summary. Invariant 1: every
    narrated number is a typed field on ``reach_layer`` (total_depletion_m3_day,
    depletion_fraction, gaining/losing reach counts) or mirrored in ``summary``.
    """

    schema_version: str = "v1"

    reach_layer: StreamReachLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class SubsidenceResult(GraceModel):
    """Return type for the CSUB-coupled ``land_subsidence`` composer branch.

    "How much will the ground sink if we keep pumping this well?" - the ground
    subsidence bowl COG (PRIMARY layer, cm) + the derived args + a narration
    summary. Invariant 1: every narrated number is a typed field on
    ``subsidence_layer`` (max_subsidence_cm, subsidence_area_km2,
    max_head_decline_m, inelastic_fraction) or mirrored in ``summary``.
    """

    schema_version: str = "v1"

    subsidence_layer: SubsidenceLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class SustainableYieldScenarioError(RuntimeError):
    """Base class for the composer failures."""

    error_code: str = "SUSTAINABLE_YIELD_SCENARIO_ERROR"
    retryable: bool = False


class SustainableYieldInputError(SustainableYieldScenarioError):
    """Caller supplied invalid / missing AOI or well input (honesty gate)."""

    error_code = "SUSTAINABLE_YIELD_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Helpers (shared shape with the composer)
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise SustainableYieldScenarioError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


async def _maybe_emit(
    emitter: Any | None, *, name: str, tool_name: str, invoke: Any
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(name=name, tool_name=tool_name, invoke=invoke)
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def _resolve_aoi_point(
    location: str | None,
    aoi_latlon: tuple[float, float] | None,
    *,
    pipeline_emitter: Any | None,
) -> tuple[float, float, str]:
    """Resolve (lat, lon, name) from a place string OR an explicit point.

    Exactly one of ``location`` / ``aoi_latlon`` must be supplied.
    """
    has_loc = bool(location and location.strip())
    has_point = aoi_latlon is not None
    if has_loc == has_point:
        raise SustainableYieldInputError(
            "supply exactly one of location or aoi_latlon "
            f"(got location={has_loc}, aoi_latlon={has_point})."
        )
    if has_point:
        lat, lon = float(aoi_latlon[0]), float(aoi_latlon[1])  # type: ignore[index]
        return lat, lon, (location or f"({lat:.4f}, {lon:.4f})")

    geocode_fn = _registry_fn("geocode_location")
    async with substep(current_emitter(), "geocode_location"):
        geo = await _maybe_emit(
            pipeline_emitter,
            name=f"Geocode: {location}",
            tool_name="geocode_location",
            invoke=lambda: geocode_fn(location),
        )
    glat = geo.get("latitude") if isinstance(geo, dict) else None
    glon = geo.get("longitude") if isinstance(geo, dict) else None
    if glat is None or glon is None:
        raise SustainableYieldScenarioError(
            f"geocode_location({location!r}) returned no centroid lat/lon."
        )
    return float(glat), float(glon), str(location)


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_sustainable_yield_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    well_location_latlon: tuple[float, float] | None = None,
    pumping_rate_m3_day: float | None = None,
    sim_years: float | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    aquifer_sy: float | None = None,
    aquifer_ss: float | None = None,
    couple_river_sfr: bool = False,
    river_name: str | None = None,
    river_inflow_m3_s: float | None = None,
    couple_subsidence: bool = False,
    inelastic_storage_override: float | None = None,
    csub_delay_interbeds: bool = False,
    csub_effective_stress: bool = False,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> SustainableYieldResult | StreamDepletionResult | SubsidenceResult:
    """Compose place/AOI + a pumping well -> MODFLOW drawdown -> DrawdownLayerURI.

    When ``couple_river_sfr`` is True this becomes the SFR-coupled
    ``stream_depletion`` story instead: a MODFLOW-6 SFR6 routed stream network is
    draped from the fetched NHDPlus flowline, and the deliverable is the per-reach
    depletion vector (how the pumping captures streamflow), returned as a
    ``StreamDepletionResult``. The pumping WEL is IDENTICAL to the drawdown story;
    the difference is the routed river coupling + the reach-depletion deliverable.

    When ``couple_subsidence`` is True this becomes the CSUB LAND-SUBSIDENCE story
    instead: a MODFLOW-6 CSUB package computes the aquifer-system compaction the
    pumping drawdown drives, and the deliverable is the ground-subsidence bowl COG
    (cm) + the drawdown context + a subsidence-vs-time chart, returned as a
    ``SubsidenceResult``. ``couple_river_sfr`` and ``couple_subsidence`` are
    MUTUALLY EXCLUSIVE (a typed input error, never silent precedence).

    Args:
        location: a place name (geocoded to the AOI point). Supply this OR
            ``aoi_latlon``  -  exactly one.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  a
            missing well is a typed USER_INPUT_REQUIRED failure (never invented).
        pumping_rate_m3_day: the well extraction rate, m^3/day. NEGATIVE =
            extraction (MF6 WEL sign). A positive value is treated as extraction
            magnitude and negated (the common-sense user intent). REQUIRED.
        sim_years / n_periods: transient horizon controls (demo default applied
            when both None).
        aquifer_k_ms / porosity / aquifer_sy / aquifer_ss: optional demo-aquifer
            overrides (narrated as demo defaults).
        compute_class: compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``SustainableYieldResult`` with the ``DrawdownLayerURI`` + derived args +
        a narration summary dict.

    Raises:
        SustainableYieldInputError: missing/invalid AOI or well (the honesty gate).
        SustainableYieldScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the well -----------------
    if well_location_latlon is None or pumping_rate_m3_day is None:
        raise SustainableYieldInputError(
            "sustainable_yield requires BOTH a well location (well_location_latlon) "
            "and a pumping rate (pumping_rate_m3_day). These are user inputs and "
            "are never invented; ask the user for the well + extraction rate."
        )

    # --- Mutual-exclusion guard: the two coupling flavours are exclusive ------ #
    # A single run answers ONE pumping-impact question (river depletion OR land
    # subsidence), never both; refuse rather than silently pick one.
    if couple_river_sfr and couple_subsidence:
        raise SustainableYieldInputError(
            "couple_river_sfr and couple_subsidence are mutually exclusive: a run "
            "answers EITHER the streamflow-depletion question OR the land-subsidence "
            "question, not both. Pick one."
        )

    # declare the planned internal-tool count up front: geocode (only
    # when a place string was supplied) + run_modflow_archetype_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    if couple_river_sfr:
        _planned += 1  # + fetch_river_geometry
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        wlat = float(well_location_latlon[0])
        wlon = float(well_location_latlon[1])
    except Exception as exc:  # noqa: BLE001
        raise SustainableYieldInputError(
            f"invalid well_location_latlon (expected (lat, lon)): {exc}"
        ) from exc

    # A positive rate is interpreted as an extraction magnitude (negate to the
    # MF6 WEL sign). A negative rate is passed through (already extraction).
    rate = float(pumping_rate_m3_day)
    wel_q = rate if rate < 0 else -rate

    # --- SFR-coupled stream_depletion branch (module wave) ------------------- #
    # "How does pumping this well affect the river?" - chain fetch_river_geometry
    # for the reach network, run the stream_depletion archetype, emit the reach
    # vector as PRIMARY + the two charts. Returns a StreamDepletionResult.
    if couple_river_sfr:
        return await _run_stream_depletion(
            lat=lat,
            lon=lon,
            location_name=location_name,
            wlat=wlat,
            wlon=wlon,
            wel_q=wel_q,
            rate=rate,
            river_name=river_name,
            river_inflow_m3_s=river_inflow_m3_s,
            sim_years=sim_years,
            n_periods=n_periods,
            aquifer_overrides=_aquifer_overrides(
                aquifer_k_ms, porosity, aquifer_sy, aquifer_ss
            ),
            compute_class=compute_class,
            pipeline_emitter=pipeline_emitter,
        )

    # --- CSUB-coupled land_subsidence branch (module wave) ------------------- #
    # "How much will the ground sink if we keep pumping this well?" - run the
    # land_subsidence archetype (CSUB on the same pumping WEL deck), emit the
    # subsidence bowl COG as PRIMARY + the drawdown COG as context + the chart.
    # Returns a SubsidenceResult.
    if couple_subsidence:
        return await _run_subsidence(
            lat=lat,
            lon=lon,
            location_name=location_name,
            wlat=wlat,
            wlon=wlon,
            wel_q=wel_q,
            rate=rate,
            sim_years=sim_years,
            n_periods=n_periods,
            inelastic_storage_override=inelastic_storage_override,
            csub_delay_interbeds=bool(csub_delay_interbeds),
            csub_effective_stress=bool(csub_effective_stress),
            aquifer_overrides=_aquifer_overrides(
                aquifer_k_ms, porosity, aquifer_sy, aquifer_ss
            ),
            compute_class=compute_class,
            pipeline_emitter=pipeline_emitter,
        )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",  # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,  # ignored when archetype is set
            duration_days=1.0,  # ignored when archetype is set
            archetype="sustainable_yield",
            well_location_latlon=(wlat, wlon),
            pumping_rate_m3_day=wel_q,
            sim_years=sim_years,
            n_periods=n_periods,
            **_aquifer_overrides(aquifer_k_ms, porosity, aquifer_sy, aquifer_ss),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise SustainableYieldInputError(
            f"invalid sustainable_yield run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model sustainable yield [{rate:g} m3/day]",
        expected_type=DrawdownLayerURI,
        error_code="SUSTAINABLE_YIELD_RUN_FAILED",
        scenario_error=SustainableYieldScenarioError,
    )

    # wire the at-well head-decline series to a value-vs-time line
    # chart. Real parsed engine output (DrawdownLayerURI.head_decline_timeseries)
    # - the builder returns None (emits nothing) when the series is absent.
    await _emit_head_decline_chart(layer, sim_years=sim_years, n_periods=n_periods)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "pumping_rate_m3_day": wel_q,
        "sim_years": sim_years,
        "n_periods": n_periods,
    }
    summary = {
        "location_name": location_name,
        "max_drawdown_m": layer.max_drawdown_m,
        "well_location_latlon": [wlat, wlon],
        "pumping_rate_m3_day": wel_q,
        "head_decline_steps": (
            len(layer.head_decline_timeseries)
            if layer.head_decline_timeseries
            else 0
        ),
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g}, "
            "and the storage terms are demo defaults, not site-specific "
            "hydrogeology."
        ),
    }
    logger.info(
        "sustainable_yield scenario complete location=%r max_drawdown_m=%.6g",
        location_name,
        layer.max_drawdown_m,
    )
    # structured aquifer-K provenance through gate_input_review.
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_sustainable_yield", layer=layer,
        entries=[aquifer_k_review_entry(
            k_source=("user_supplied" if aquifer_k_ms is not None else "demo_default"),
            k_ms=(aquifer_k_ms if aquifer_k_ms is not None else DEFAULT_AQUIFER_K_MS),
            porosity=porosity, note=summary["demo_aquifer_caveat"],
        )],
        params={"aquifer_k_ms": aquifer_k_ms, "porosity": porosity},
    )
    if _review.cancelled:
        raise SustainableYieldScenarioError(
            f"sustainable-yield input review {_review.cancel_reason or 'not approved'}"
        )
    return SustainableYieldResult(
        drawdown_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# Engine-output chart: head-decline value-vs-time line
# --------------------------------------------------------------------------- #


async def _emit_head_decline_chart(
    layer: DrawdownLayerURI,
    *,
    sim_years: float | None,
    n_periods: int | None,
) -> None:
    """Side-emit the at-well head-decline line chart (best-effort, no-op safe).

    Builds a value-vs-time line from the typed
    ``DrawdownLayerURI.head_decline_timeseries`` (real solver output) and emits
    it through the live pipeline emitter. The honesty floor holds in the
    builder: an absent / single-point series yields no chart. The x axis is in
    elapsed days when ``sim_years`` + ``n_periods`` give a per-step day count,
    else the bare timestep index."""
    from trid3nt_server.agent.tools.processing.charts_common import build_head_decline_chart

    series = getattr(layer, "head_decline_timeseries", None)
    if not series:
        return
    days_per_step: float | None = None
    if sim_years and n_periods and n_periods > 0:
        try:
            days_per_step = (float(sim_years) * 365.25) / float(n_periods)
        except (TypeError, ValueError, ZeroDivisionError):
            days_per_step = None
    chart = build_head_decline_chart(
        head_decline_timeseries=list(series),
        days_per_step=days_per_step,
        source_layer_uri=getattr(layer, "uri", None),
    )
    await emit_chart_payloads(chart)


# --------------------------------------------------------------------------- #
# SFR-coupled stream_depletion branch (module wave)
# --------------------------------------------------------------------------- #


def _layer_uri(obj: Any) -> str | None:
    """Extract a ``uri`` field from a fetch-tool result (dict or model)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get("uri")
    return getattr(obj, "uri", None)


async def _run_stream_depletion(
    *,
    lat: float,
    lon: float,
    location_name: str,
    wlat: float,
    wlon: float,
    wel_q: float,
    rate: float,
    river_name: str | None,
    river_inflow_m3_s: float | None,
    sim_years: float | None,
    n_periods: int | None,
    aquifer_overrides: dict[str, Any],
    compute_class: str,
    pipeline_emitter: Any | None,
) -> StreamDepletionResult:
    """Chain fetch_river_geometry -> stream_depletion run -> reach vector + charts.

    The pumping WEL is IDENTICAL to the drawdown story (``wel_q``); the difference
    is the routed SFR river coupling and the per-reach depletion deliverable. The
    river geometry is fetched over a ~2 km bbox around the AOI point (the same
    demo domain the adapter builds). A missing flowline is a typed failure - the
    river is NEVER fabricated (Invariant 9)."""
    # ~2 km half-window in degrees around the AOI point (matches the demo domain).
    d_lat = 1000.0 / 111_000.0
    import math as _math

    d_lon = 1000.0 / (111_000.0 * max(0.1, _math.cos(_math.radians(lat))))
    bbox = (lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)

    fetch_river_fn = _registry_fn("fetch_river_geometry")
    async with substep(current_emitter(), "fetch_river_geometry"):
        river_layer = await _maybe_emit(
            pipeline_emitter,
            name=f"Fetch river geometry{f' [{river_name}]' if river_name else ''}",
            tool_name="fetch_river_geometry",
            invoke=lambda: fetch_river_fn(bbox=list(bbox)),
        )
    river_uri = _layer_uri(river_layer)
    if not river_uri:
        raise SustainableYieldScenarioError(
            "fetch_river_geometry returned no river flowline near the AOI; cannot "
            "drape an SFR reach network (no mapped river within ~1 km of the well)."
        )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",  # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,  # ignored when archetype is set
            duration_days=1.0,  # ignored when archetype is set
            archetype="stream_depletion",
            well_location_latlon=(wlat, wlon),
            pumping_rate_m3_day=wel_q,
            sim_years=sim_years,
            n_periods=n_periods,
            river_geometry_uri=river_uri,
            river_inflow_m3_s=river_inflow_m3_s,
            **aquifer_overrides,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError
        raise SustainableYieldInputError(
            f"invalid stream_depletion run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model stream depletion [{rate:g} m3/day]",
        expected_type=StreamReachLayerURI,
        error_code="STREAM_DEPLETION_RUN_FAILED",
        scenario_error=SustainableYieldScenarioError,
    )

    # Emit the two stashed engine-output charts (best-effort; the postprocess
    # stashed them as private attrs so the Pydantic layer stayed clean).
    for attr in ("_depletion_chart", "_reach_profile_chart"):
        chart = getattr(layer, attr, None)
        if chart:
            await emit_chart_payloads(chart)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "pumping_rate_m3_day": wel_q,
        "river_name": river_name,
        "river_geometry_uri": river_uri,
        "river_inflow_m3_s": river_inflow_m3_s,
        "sim_years": sim_years,
        "n_periods": n_periods,
    }
    summary = {
        "location_name": location_name,
        "river_name": river_name,
        "total_depletion_m3_day": layer.total_depletion_m3_day,
        "depletion_fraction": layer.depletion_fraction,
        "n_reaches": layer.n_reaches,
        "max_stage_decline_m": layer.max_stage_decline_m,
        "gaining_reach_count": layer.gaining_reach_count,
        "losing_reach_count": layer.losing_reach_count,
        "pumping_rate_m3_day": wel_q,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g}, "
            "the channel width, Manning roughness, and streambed K are demo "
            "defaults, not site-specific hydrogeology. The depletion fraction is a "
            "qualitative planning estimate (streambed resistance keeps it below the "
            "Glover-Balmer analytic curve)."
        ),
    }
    logger.info(
        "stream_depletion scenario complete location=%r total_depletion_m3_day=%.6g "
        "fraction=%.3f n_reaches=%d",
        location_name,
        layer.total_depletion_m3_day,
        layer.depletion_fraction,
        layer.n_reaches,
    )
    # structured aquifer-K provenance through gate_input_review.
    _sd_k = aquifer_overrides.get("aquifer_k_ms")
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_stream_depletion", layer=layer,
        entries=[aquifer_k_review_entry(
            k_source=("user_supplied" if _sd_k is not None else "demo_default"),
            k_ms=(_sd_k if _sd_k is not None else DEFAULT_AQUIFER_K_MS),
            porosity=aquifer_overrides.get("porosity"),
            note=summary["demo_aquifer_caveat"],
        )],
        params={"aquifer_k_ms": _sd_k},
    )
    if _review.cancelled:
        raise SustainableYieldScenarioError(
            f"stream-depletion input review {_review.cancel_reason or 'not approved'}"
        )
    return StreamDepletionResult(
        reach_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# CSUB-coupled land_subsidence branch (module wave)
# --------------------------------------------------------------------------- #


async def _run_subsidence(
    *,
    lat: float,
    lon: float,
    location_name: str,
    wlat: float,
    wlon: float,
    wel_q: float,
    rate: float,
    sim_years: float | None,
    n_periods: int | None,
    inelastic_storage_override: float | None,
    csub_delay_interbeds: bool = False,
    csub_effective_stress: bool = False,
    aquifer_overrides: dict[str, Any],
    compute_class: str,
    pipeline_emitter: Any | None,
) -> SubsidenceResult:
    """Run the land_subsidence archetype -> subsidence bowl COG + drawdown context.

    The pumping WEL is IDENTICAL to the drawdown story (``wel_q``); the difference
    is the CSUB package (aquifer-system compaction) and the subsidence-bowl
    deliverable. No new fetcher: the CSUB interbed storage/thickness are
    demo-defaulted in the adapter (narrated honestly, never site precision). The
    subsidence COG is the PRIMARY layer; the postprocess stashes a CONTEXT
    drawdown COG (the cone that drove the compaction) + a subsidence-vs-time chart,
    both emitted here."""
    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",  # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,  # ignored when archetype is set
            duration_days=1.0,  # ignored when archetype is set
            archetype="land_subsidence",
            well_location_latlon=(wlat, wlon),
            pumping_rate_m3_day=wel_q,
            sim_years=sim_years,
            n_periods=n_periods,
            csub_ssv_inelastic_m=(
                float(inelastic_storage_override)
                if inelastic_storage_override is not None
                else None
            ),
            csub_delay_interbeds=bool(csub_delay_interbeds),
            csub_effective_stress=bool(csub_effective_stress),
            **aquifer_overrides,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError
        raise SustainableYieldInputError(
            f"invalid land_subsidence run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model land subsidence [{rate:g} m3/day]",
        expected_type=SubsidenceLayerURI,
        error_code="LAND_SUBSIDENCE_RUN_FAILED",
        scenario_error=SustainableYieldScenarioError,
    )

    # Emit the CONTEXT drawdown COG (the cone that drove the compaction) beside the
    # primary subsidence bowl (the primary layer is loaded by _run_archetype; the
    # context layer is stashed on the subsidence layer as a private attr by the
    # postprocess). Best-effort.
    drawdown_context = getattr(layer, "_drawdown_context", None)
    if drawdown_context is not None:
        try:
            emitter = current_emitter()
            emit_layer = emit_layer_uri(drawdown_context)
            if emitter is not None and emit_layer is not None:
                await emitter.add_loaded_layer(emit_layer)
        except Exception as exc:  # noqa: BLE001 -- context layer is best-effort
            logger.warning("subsidence drawdown-context emit failed: %s", exc)

    # Emit the stashed subsidence-vs-time chart (best-effort).
    chart = getattr(layer, "_subsidence_chart", None)
    if chart:
        await emit_chart_payloads(chart)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "pumping_rate_m3_day": wel_q,
        "sim_years": sim_years,
        "n_periods": n_periods,
        "inelastic_storage_override": inelastic_storage_override,
        "csub_delay_interbeds": bool(csub_delay_interbeds),
        "csub_effective_stress": bool(csub_effective_stress),
    }
    summary = {
        "location_name": location_name,
        "max_subsidence_cm": layer.max_subsidence_cm,
        "subsidence_area_km2": layer.subsidence_area_km2,
        "max_head_decline_m": layer.max_head_decline_m,
        "inelastic_fraction": layer.inelastic_fraction,
        "interbed_count": layer.interbed_count,
        "pumping_rate_m3_day": wel_q,
        "csub_formulation": (
            "EFFECTIVE_STRESS (geostatic sgm/sgs unit weights + specified "
            "preconsolidation stress; demo-parameterized, ~0.4-0.5 of head-based)"
            if csub_effective_stress
            else "HEAD_BASED (initial head = preconsolidation; all drawdown -> "
                 "inelastic compaction)"
        ),
        "csub_interbed_type": (
            "DELAY interbed (finite consolidation diffusivity: compaction is "
            "TIME-LAGGED and, at end-of-pumping, LESS than the equivalent no-delay "
            "bed; ndelaycells + a low interbed vertical K are demo assumptions)"
            if csub_delay_interbeds
            else "no-delay interbed (consolidates instantly with head)"
        ),
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g}, "
            "and the CSUB interbed thickness + inelastic/elastic compaction "
            "storage are demo defaults (no site clay-fraction fetcher in v1), so "
            "the subsidence magnitude is a qualitative planning estimate, NOT a "
            "calibrated Central Valley forecast. The HEAD_BASED formulation with "
            "preconsolidation = the initial head means all drawdown drives "
            "PERMANENT (inelastic) compaction; a previously-overdrafted aquifer "
            "with a lower preconsolidation would compact differently."
        ),
    }
    logger.info(
        "land_subsidence scenario complete location=%r max_subsidence_cm=%.6g "
        "inelastic_fraction=%.3f max_head_decline_m=%.4g interbeds=%d",
        location_name,
        layer.max_subsidence_cm,
        layer.inelastic_fraction,
        layer.max_head_decline_m,
        layer.interbed_count,
    )
    # structured aquifer-K provenance through gate_input_review.
    _sub_k = aquifer_overrides.get("aquifer_k_ms")
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_land_subsidence", layer=layer,
        entries=[aquifer_k_review_entry(
            k_source=("user_supplied" if _sub_k is not None else "demo_default"),
            k_ms=(_sub_k if _sub_k is not None else DEFAULT_AQUIFER_K_MS),
            porosity=aquifer_overrides.get("porosity"),
            note=summary["demo_aquifer_caveat"],
        )],
        params={"aquifer_k_ms": _sub_k},
    )
    if _review.cancelled:
        raise SustainableYieldScenarioError(
            f"land-subsidence input review {_review.cancel_reason or 'not approved'}"
        )
    return SubsidenceResult(
        subsidence_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# Shared archetype-run helper (re-used by all three composers' shape)
# --------------------------------------------------------------------------- #


def _aquifer_overrides(
    aquifer_k_ms: float | None,
    porosity: float | None,
    aquifer_sy: float | None,
    aquifer_ss: float | None,
) -> dict[str, Any]:
    """Pass only the supplied aquifer overrides (let the contract default the rest)."""
    out: dict[str, Any] = {}
    if aquifer_k_ms is not None:
        out["aquifer_k_ms"] = float(aquifer_k_ms)
    if porosity is not None:
        out["porosity"] = float(porosity)
    if aquifer_sy is not None:
        out["aquifer_sy"] = float(aquifer_sy)
    if aquifer_ss is not None:
        out["aquifer_ss"] = float(aquifer_ss)
    return out


async def _run_archetype(
    run_args: MODFLOWRunArgs,
    *,
    compute_class: str,
    pipeline_emitter: Any | None,
    tool_label: str,
    expected_type: type,
    error_code: str,
    scenario_error: type[Exception],
) -> Any:
    """Run the archetype solver inside a substep + validate the typed layer.

    Imported lazily so the composer module has no import-time tool dependency on
    the run-tool (which imports the heavy solver seam). The failed-but-RETURNED
    validation lives INSIDE the substep so a non-typed result raises here,
    marking the child red (honesty floor) before the error re-raises.
    """
    from trid3nt_server.agent.tools.simulation.modflow.run_modflow_archetype_tool import run_modflow_archetype_job

    async with substep(current_emitter(), "run_modflow_archetype_job"):
        result = await _maybe_emit(
            pipeline_emitter,
            name=tool_label,
            tool_name="run_modflow_archetype_job",
            invoke=lambda: run_modflow_archetype_job(
                run_args, compute_class=compute_class
            ),
        )
        if not isinstance(result, expected_type):
            ecode = error_code
            emsg = "archetype run did not produce the expected layer"
            if isinstance(result, dict):
                ecode = result.get("error_code", ecode)
                emsg = result.get("error_message", emsg)
            raise scenario_error(f"{ecode}: {emsg}")

    # Load the primary archetype layer onto the map + Case. The thin workflow
    # tool serializes the composer's typed ``*Result`` to a dict, and the server
    # dispatch's ``add_loaded_layer`` gate fires ONLY on a bare-``LayerURI``
    # return -- so a dict-returning composer must load its own headline layer.
    # ``_maybe_emit`` above only routes through ``emit_tool_call`` (whose gate
    # would load it) when a live ``pipeline_emitter`` is passed; the thin tools
    # pass ``None``, so this is the seam every archetype (capture zone, wellhead,
    # drawdown, dewatering, subsidence, ...) relies on to reach the map. Dedups
    # by layer identity, so a caller that also loads it (or a re-run) is a no-op.
    # Best-effort: a ``None`` emitter (direct-call / CI) or a dropped
    # un-renderable layer no-ops.
    emitter = current_emitter()
    if emitter is not None:
        emit_layer = emit_layer_uri(result)
        if emit_layer is not None:
            await emitter.add_loaded_layer(emit_layer)
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question="a pumping well's drawdown cone (sustainable yield), its river depletion, OR the land subsidence it drives",
    required_inputs=['location (or aoi_latlon)', 'well_location_latlon', 'pumping_rate_m3_day'],
    knobs='sim_years, n_periods, couple_river_sfr, couple_subsidence, aquifer_k_ms, porosity',
)


_METADATA = AtomicToolMetadata(
    name="modflow_sustainable_yield",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_sustainable_yield(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    pumping_rate_m3_day: float | None = None,
    sim_years: float | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    couple_river_sfr: bool = False,
    river_name: str | None = None,
    river_inflow_m3_s: float | None = None,
    couple_subsidence: bool = False,
    inelastic_storage_override: float | None = None,
    csub_delay_interbeds: bool = False,
    csub_effective_stress: bool = False,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a pumping well's drawdown cone, OR its impact on a river, OR the land subsidence it causes.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity default to narrated demo values unless supplied), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Builds a MODFLOW 6 transient groundwater-flow model with a sustained
    extraction well at the user-supplied location + rate, runs it, and produces a
    DRAWDOWN layer (the cone of depression  -  how far the water table is drawn
    down around the well, and the peak decline). Use this to assess sustainable
    yield, well interference, or how much a proposed pumping rate lowers the water
    table.

    This tool answers THREE flavours of the pumping question - pick with the flags:
      * DEFAULT (both flags False) = the DRAWDOWN CONE: how far the water table is
        drawn down around the well (sustainable yield / well interference).
      * ``couple_river_sfr=True`` = STREAMFLOW DEPLETION: "how does pumping this
        well affect the river / stream / creek?" - a routed MODFLOW-6 SFR network
        + a per-reach depletion vector (captured streamflow) + depletion charts.
      * ``couple_subsidence=True`` = LAND SUBSIDENCE: "how much will the GROUND
        SINK / subside if we keep pumping this well?" - a MODFLOW-6 CSUB
        aquifer-system-compaction run + a ground-subsidence bowl COG (cm) + the
        drawdown context + a subsidence-vs-time chart. The two flags are mutually
        exclusive.

    Use this when:
        - The user asks how much a pumping well draws down the water table, the
          drawdown cone / cone of depression, sustainable yield, or well
          interference (leave both flags False).
        - The user asks how pumping a well affects a nearby RIVER / STREAM / creek,
          streamflow depletion, or captured baseflow (set ``couple_river_sfr=True``
          and optionally name the river).
        - The user asks whether/how much pumping will make the GROUND SINK, SUBSIDE,
          or COMPACT - land subsidence, ground-surface settlement, aquifer-system
          compaction (set ``couple_subsidence=True``).

    Do NOT use this for:
        - A contaminant spill plume (use ``modflow_contaminant_plume``).
        - Mine-pit dewatering (use ``modflow_mine_dewatering``).
        - Surface-water flooding (use ``sfincs_flood``  -  SFINCS).
        - Dye / tracer released INTO a surface river and carried DOWNSTREAM (that
          is surface-water transport  -  use ``run_telemac``).
        - A static fixed-stage river<->aquifer seepage / gaining-losing budget with
          NO pumping question (use ``modflow_river_seepage``  -  it holds the river
          stage fixed; this tool's ``couple_river_sfr`` instead ROUTES the stream
          and answers the pumping-vs-river DEPLETION question).
        - Land SUBSIDING from anything OTHER than groundwater pumping (tectonics,
          sinkholes, thawing permafrost) - CSUB models only pumping-induced
          aquifer-system compaction.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  never
            invented; ask the user if absent.
        pumping_rate_m3_day: well extraction rate, m^3/day. REQUIRED. A positive
            value is treated as extraction magnitude; negative is extraction too.
        sim_years / n_periods: optional transient horizon controls.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        couple_river_sfr: when True, run the SFR-coupled STREAM DEPLETION story
            (reach vector + depletion charts) instead of the drawdown cone.
        river_name: optional river name for narration (the flowline is fetched by
            AOI bbox regardless).
        river_inflow_m3_s: optional headwater streamflow inflow (m^3/s); a demo
            default is applied when absent (narrated as a demo assumption).
        couple_subsidence: when True, run the CSUB LAND SUBSIDENCE story (ground
            subsidence bowl COG + drawdown context + subsidence chart) instead of
            the drawdown cone. Mutually exclusive with ``couple_river_sfr``.
        inelastic_storage_override: optional inelastic (virgin) specific storage
            Ssv (m^-1) for the CSUB interbed - the knob that sets the subsidence
            MAGNITUDE. A demo default (~2e-3) is applied when absent (narrated as a
            demo assumption). Only used when ``couple_subsidence=True``.
        csub_delay_interbeds: CSUB formulation knob (only when
            ``couple_subsidence=True``). When True the compressible interbed is a
            DELAY interbed (finite consolidation diffusivity via ndelaycells + a low
            interbed vertical K) so compaction is TIME-LAGGED: the bed keeps
            consolidating after the head decline and, at end-of-pumping, has
            compacted LESS than an equivalent no-delay bed. When False (default) the
            bed consolidates instantly with head. ndelaycells + the interbed K are
            demo assumptions (no site clay-profile fetcher), narrated as such.
        csub_effective_stress: CSUB formulation knob (only when
            ``couple_subsidence=True``). When True the run uses the EFFECTIVE_STRESS
            formulation (geostatic sgm/sgs unit weights + a specified initial
            preconsolidation stress) instead of the default HEAD_BASED formulation;
            the two bracket the same subsidence scale (effective-stress typically
            ~0.4-0.5 of head-based for the same stress path) as an
            order-of-magnitude crosscheck. sgm/sgs are demo assumptions, narrated
            as such. When False (default) HEAD_BASED with the initial head as the
            preconsolidation head is used.
        compute_class: compute class. Default ``"standard"``.

    Returns:
        On success: a ``SustainableYieldResult`` JSON dict with the
        ``drawdown_layer`` (a ``DrawdownLayerURI`` carrying ``max_drawdown_m`` +
        ``head_decline_timeseries``  -  the agent narrates these typed numbers),
        the ``derived_params``, and the ``summary``. On a recoverable failure
        (incl. a missing well/rate) the tool returns a typed error the agent
        narrates honestly  -  it never fabricates a well.

    ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_sustainable_yield_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            pumping_rate_m3_day=(
                float(pumping_rate_m3_day) if pumping_rate_m3_day is not None else None
            ),
            sim_years=sim_years,
            n_periods=n_periods,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            couple_river_sfr=bool(couple_river_sfr),
            river_name=river_name,
            river_inflow_m3_s=(
                float(river_inflow_m3_s) if river_inflow_m3_s is not None else None
            ),
            couple_subsidence=bool(couple_subsidence),
            inelastic_storage_override=(
                float(inelastic_storage_override)
                if inelastic_storage_override is not None
                else None
            ),
            csub_delay_interbeds=bool(csub_delay_interbeds),
            csub_effective_stress=bool(csub_effective_stress),
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except SustainableYieldInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except SustainableYieldScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "SUSTAINABLE_YIELD_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")


def _coerce_optional_latlon(value: Any) -> tuple[float, float] | None:
    """Coerce an optional lat/lon arg (str / list / tuple) -> (lat, lon) or None."""
    if value is None:
        return None
    from trid3nt_server.agent.tool_arg_normalizer import coerce_latlon

    return tuple(coerce_latlon(value))  # type: ignore[return-value]
