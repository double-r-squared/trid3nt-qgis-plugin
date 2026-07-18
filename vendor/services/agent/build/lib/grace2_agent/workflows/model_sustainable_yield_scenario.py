"""``model_sustainable_yield_scenario``  -  MODFLOW pumping-drawdown composer.

The end-to-end higher-order workflow for the sprint-18 Wave-1 MODFLOW
``sustainable_yield`` archetype: it turns a place (or AOI point) + a pumping
well (location + extraction rate) into a rendered drawdown-cone layer  -  the cone
of depression a sustained extraction draws down around the well. It mirrors the
chain shape of ``model_river_seepage_scenario`` (the J9 template) and the Case 2
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

from grace2_contracts.common import GraceModel
from grace2_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    DrawdownLayerURI,
    MODFLOWRunArgs,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from ..tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger(
    "grace2_agent.workflows.model_sustainable_yield_scenario"
)

__all__ = [
    "SustainableYieldResult",
    "model_sustainable_yield_scenario",
    "run_model_sustainable_yield_scenario",
    "SustainableYieldScenarioError",
    "SustainableYieldInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope (agent-local; mirrors RiverSeepageResult)
# --------------------------------------------------------------------------- #


class SustainableYieldResult(GraceModel):
    """Return type for ``model_sustainable_yield_scenario`` (sprint-18 Wave-1).

    Bundles the drawdown layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``drawdown_layer``
    carries ``max_drawdown_m`` + the timeseries; ``summary`` mirrors them.
    """

    schema_version: str = "v1"

    drawdown_layer: DrawdownLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class SustainableYieldScenarioError(RuntimeError):
    """Base class for ``model_sustainable_yield_scenario`` failures."""

    error_code: str = "SUSTAINABLE_YIELD_SCENARIO_ERROR"
    retryable: bool = False


class SustainableYieldInputError(SustainableYieldScenarioError):
    """Caller supplied invalid / missing AOI or well input (honesty gate)."""

    error_code = "SUSTAINABLE_YIELD_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Helpers (shared shape with model_river_seepage_scenario)
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
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> SustainableYieldResult:
    """Compose place/AOI + a pumping well -> MODFLOW drawdown -> DrawdownLayerURI.

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
        compute_class: FR-CE-3 compute class.
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

    # task-168: declare the planned internal-tool count up front: geocode (only
    # when a place string was supplied) + run_modflow_archetype_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
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

    # task-198: wire the at-well head-decline series to a value-vs-time line
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
    return SustainableYieldResult(
        drawdown_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# Engine-output chart (task-198): head-decline value-vs-time line
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
    from ..tools.chart_tools import build_head_decline_chart

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
# Shared archetype-run helper (re-used by all three Wave-1 composers' shape)
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
    from ..tools.run_modflow_archetype_tool import run_modflow_archetype_job

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
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_sustainable_yield_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_sustainable_yield_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    pumping_rate_m3_day: float | None = None,
    sim_years: float | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a pumping well's drawdown cone (sustainable-yield / well interference).

    Builds a MODFLOW 6 transient groundwater-flow model with a sustained
    extraction well at the user-supplied location + rate, runs it, and produces a
    DRAWDOWN layer (the cone of depression  -  how far the water table is drawn
    down around the well, and the peak decline). Use this to assess sustainable
    yield, well interference, or how much a proposed pumping rate lowers the water
    table.

    Use this when:
        - The user asks how much a pumping well draws down the water table, the
          drawdown cone / cone of depression, sustainable yield, or well
          interference.

    Do NOT use this for:
        - A contaminant spill plume (use ``run_modflow_job``).
        - Mine-pit dewatering (use ``run_model_mine_dewatering_scenario``).
        - Surface-water flooding (use ``run_model_flood_scenario``  -  SFINCS).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  never
            invented; ask the user if absent.
        pumping_rate_m3_day: well extraction rate, m^3/day. REQUIRED. A positive
            value is treated as extraction magnitude; negative is extraction too.
        sim_years / n_periods: optional transient horizon controls.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``SustainableYieldResult`` JSON dict with the
        ``drawdown_layer`` (a ``DrawdownLayerURI`` carrying ``max_drawdown_m`` +
        ``head_decline_timeseries``  -  the agent narrates these typed numbers),
        the ``derived_params``, and the ``summary``. On a recoverable failure
        (incl. a missing well/rate) the tool returns a typed error the agent
        narrates honestly  -  it never fabricates a well.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
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
    from ..tool_arg_normalizer import coerce_latlon

    return tuple(coerce_latlon(value))  # type: ignore[return-value]
