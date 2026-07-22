"""``model_wetland_hydroperiod_scenario``  -  MODFLOW wetland-hydroperiod composer.

The end-to-end higher-order workflow for the sprint-18 Wave-2 MODFLOW
``wetland_hydroperiod`` archetype: it turns a place (or AOI point) + a wetland
footprint into a rendered seasonal-water-table-range layer  -  how much the wetland
water table swings across the recharge / evapotranspiration seasons (the
hydroperiod). It mirrors the chain shape of ``model_mine_dewatering_scenario``
(sibling footprint-driven archetype): the wetland footprint is draped as RCH
recharge (a per-period wet/dry schedule) + an EVT head-dependent ET sink over an
unconfined transient water table, and the seasonal head RANGE IS the deliverable.

Canonical real-world pipeline mirrored here (a wetland-hydroperiod / seasonal
water-table-fluctuation analysis):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the wetland footprint polygon (NEVER fabricated  -  a
           missing footprint is a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="wetland_hydroperiod", footprint, ...)
        -> run_modflow_archetype_job (GWF transient RCH+EVT deck -> mf6 -> range)
        -> HydroperiodLayerURI (seasonal_head_range_m + head_timeseries)

Invariants (same set as model_mine_dewatering_scenario):
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A ``wetland_hydroperiod`` run with no wetland
  footprint returns a typed ``USER_INPUT_REQUIRED`` failed envelope rather than
  inventing a wetland  -  the honesty floor: a "modeled" envelope with empty layers
  never reads ok.
- **10. Minimal parameter surface: preserves.** Intent (place + footprint +
  recharge / ET schedule) is exposed; the grid + demo aquifer K / Sy are derived.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    HydroperiodLayerURI,
    MODFLOWRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter, emit_chart_payloads
from ..tools import register_tool
from .model_mine_dewatering_scenario import _normalize_pit_footprint
from .model_sustainable_yield_scenario import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.model_wetland_hydroperiod_scenario"
)

__all__ = [
    "WetlandHydroperiodResult",
    "model_wetland_hydroperiod_scenario",
    "run_model_wetland_hydroperiod_scenario",
    "WetlandHydroperiodScenarioError",
    "WetlandHydroperiodInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class WetlandHydroperiodResult(GraceModel):
    """Return type for ``model_wetland_hydroperiod_scenario`` (sprint-18 Wave-2).

    Bundles the hydroperiod layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``hydroperiod_layer``
    carries ``seasonal_head_range_m`` + the under-wetland ``head_timeseries``.
    """

    schema_version: str = "v1"

    hydroperiod_layer: HydroperiodLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class WetlandHydroperiodScenarioError(RuntimeError):
    """Base class for ``model_wetland_hydroperiod_scenario`` failures."""

    error_code: str = "WETLAND_HYDROPERIOD_SCENARIO_ERROR"
    retryable: bool = False


class WetlandHydroperiodInputError(WetlandHydroperiodScenarioError):
    """Caller supplied invalid / missing AOI or wetland input (honesty gate)."""

    error_code = "WETLAND_HYDROPERIOD_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Engine-output chart (mirror the head-decline emit): hydroperiod head-vs-time.
# --------------------------------------------------------------------------- #


async def _emit_hydroperiod_chart(layer: HydroperiodLayerURI) -> None:
    """Side-emit the wetland seasonal water-table head-vs-time line (no-op safe).

    Builds a head-vs-time line from the typed ``HydroperiodLayerURI.head_timeseries``
    (real solver output  -  the water table under the wetland over the seasons). The
    builder emits nothing for an absent / single-point series (the honesty floor).
    """
    from ..tools.processing.charts_common import build_head_series_chart

    series = getattr(layer, "head_timeseries", None)
    if not series:
        return
    chart = build_head_series_chart(
        head_timeseries=list(series),
        title="Wetland water table over the seasons (hydroperiod)",
        y_title="water table (m)",
        caption_label="seasonal rise/fall of the wetland water table",
        source_layer_uri=getattr(layer, "uri", None),
    )
    await emit_chart_payloads(chart)


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_wetland_hydroperiod_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    wetland_footprint_lonlat: Any | None = None,
    recharge_schedule_m_day: list[float] | None = None,
    et_surface_m: float | None = None,
    et_max_rate_m_day: float | None = None,
    et_extinction_depth_m: float | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    specific_yield: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> WetlandHydroperiodResult:
    """Compose place/AOI + a wetland footprint -> MODFLOW -> HydroperiodLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        wetland_footprint_lonlat: the wetland outline as ``[(lon, lat), ...]``
            (or a GeoJSON polygon). REQUIRED  -  a missing footprint is a typed
            USER_INPUT_REQUIRED failure (never invented).
        recharge_schedule_m_day: per-transient-period recharge rate (one m/day
            value per period). Demo wet/dry alternation applied when None.
        et_surface_m / et_max_rate_m_day / et_extinction_depth_m: EVT (ET sink)
            controls. Demo defaults applied by the adapter when None.
        n_periods: explicit transient period override.
        aquifer_k_ms / porosity / specific_yield: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``WetlandHydroperiodResult`` with the ``HydroperiodLayerURI`` + derived
        args + a narration summary dict.

    Raises:
        WetlandHydroperiodInputError: missing/invalid AOI or footprint (honesty gate).
        WetlandHydroperiodScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the wetland ---------------
    if wetland_footprint_lonlat is None:
        raise WetlandHydroperiodInputError(
            "wetland_hydroperiod requires a wetland footprint "
            "(wetland_footprint_lonlat). The wetland outline is a user input and "
            "is never invented; ask the user to draw / supply the wetland polygon."
        )
    try:
        wetland_verts = _normalize_pit_footprint(wetland_footprint_lonlat)
    except Exception as exc:  # noqa: BLE001
        raise WetlandHydroperiodInputError(
            f"invalid wetland_footprint_lonlat (expected [(lon, lat), ...]): {exc}"
        ) from exc

    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    schedule = (
        [float(v) for v in recharge_schedule_m_day]
        if recharge_schedule_m_day
        else None
    )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",
            release_rate_kg_s=1.0,
            duration_days=1.0,
            archetype="wetland_hydroperiod",
            wetland_footprint_lonlat=wetland_verts,
            recharge_schedule_m_day=schedule,
            et_surface_m=et_surface_m,
            et_max_rate_m_day=et_max_rate_m_day,
            et_extinction_depth_m=et_extinction_depth_m,
            n_periods=n_periods,
            **_wetland_overrides(aquifer_k_ms, porosity, specific_yield),
        )
    except Exception as exc:  # noqa: BLE001
        raise WetlandHydroperiodInputError(
            f"invalid wetland_hydroperiod run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model wetland hydroperiod [{len(wetland_verts)} footprint vertices]",
        expected_type=HydroperiodLayerURI,
        error_code="WETLAND_HYDROPERIOD_RUN_FAILED",
        scenario_error=WetlandHydroperiodScenarioError,
    )

    # Mirror the head-decline emit: side-emit the seasonal head-vs-time line chart
    # from the typed HydroperiodLayerURI.head_timeseries (real solver output).
    await _emit_hydroperiod_chart(layer)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "wetland_vertex_count": len(wetland_verts),
        "recharge_schedule_m_day": schedule,
        "et_surface_m": et_surface_m,
        "et_max_rate_m_day": et_max_rate_m_day,
        "et_extinction_depth_m": et_extinction_depth_m,
        "n_periods": n_periods,
    }
    summary = {
        "location_name": location_name,
        "seasonal_head_range_m": layer.seasonal_head_range_m,
        "wetland_vertex_count": len(wetland_verts),
        "head_series_steps": (
            len(layer.head_timeseries) if layer.head_timeseries else 0
        ),
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, the specific yield, and the "
            "recharge / ET schedule are demo defaults, not site-specific "
            "hydrogeology."
        ),
    }
    logger.info(
        "wetland_hydroperiod scenario complete location=%r seasonal_head_range_m=%.6g",
        location_name,
        layer.seasonal_head_range_m,
    )
    return WetlandHydroperiodResult(
        hydroperiod_layer=layer, derived_params=derived, summary=summary
    )


def _wetland_overrides(
    aquifer_k_ms: float | None,
    porosity: float | None,
    specific_yield: float | None,
) -> dict[str, Any]:
    """Pass only the supplied wetland overrides (let the contract default the rest).

    ``specific_yield`` is the wetland-specific Sy field (distinct from the generic
    ``aquifer_sy``); ``_aquifer_overrides`` does not cover it, so it is threaded
    here on top of the shared K / porosity overrides.
    """
    out = _aquifer_overrides(aquifer_k_ms, porosity, None, None)
    if specific_yield is not None:
        out["specific_yield"] = float(specific_yield)
    return out


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_wetland_hydroperiod_scenario",
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
async def run_model_wetland_hydroperiod_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    wetland_footprint_lonlat: Any | None = None,
    recharge_schedule_m_day: list[float] | None = None,
    et_surface_m: float | None = None,
    et_max_rate_m_day: float | None = None,
    et_extinction_depth_m: float | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    specific_yield: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a wetland's seasonal water-table range (hydroperiod).

    Builds a transient MODFLOW 6 groundwater-flow model with an unconfined water
    table, an RCH recharge schedule (seasonal wet/dry), and an EVT
    evapotranspiration sink over the user-supplied wetland footprint, runs it, and
    produces a HYDROPERIOD layer: how much the wetland water table swings across
    the seasons (the seasonal head range) and the under-wetland head series. Use
    this to assess wetland hydroperiod / seasonal water-table fluctuation.

    Use this when:
        - The user asks about a wetland's hydroperiod, seasonal water-table swing /
          fluctuation, or how wet/dry seasons move the wetland water table.

    Do NOT use this for:
        - A recharge-basin mound (use ``run_model_mar_scenario``).
        - A pumping-well drawdown cone (use ``run_model_sustainable_yield_scenario``).
        - Surface-water flooding (use ``run_model_flood_scenario``  -  SFINCS).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        wetland_footprint_lonlat: the wetland outline as ``[(lon, lat), ...]`` or a
            GeoJSON polygon. REQUIRED  -  never invented; ask the user if absent.
        recharge_schedule_m_day: per-period recharge rate list (m/day). Demo
            wet/dry alternation if None.
        et_surface_m / et_max_rate_m_day / et_extinction_depth_m: EVT controls.
            Demo defaults if None.
        n_periods: explicit transient period override.
        aquifer_k_ms / porosity / specific_yield: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``WetlandHydroperiodResult`` JSON dict with the
        ``hydroperiod_layer`` (a ``HydroperiodLayerURI`` carrying
        ``seasonal_head_range_m`` + ``head_timeseries``  -  the agent narrates these
        typed numbers), the ``derived_params``, and the ``summary``. On a
        recoverable failure (incl. a missing footprint) the tool returns a typed
        error the agent narrates honestly  -  it never fabricates a wetland.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await model_wetland_hydroperiod_scenario(
            location=location,
            aoi_latlon=aoi,
            wetland_footprint_lonlat=wetland_footprint_lonlat,
            recharge_schedule_m_day=recharge_schedule_m_day,
            et_surface_m=(float(et_surface_m) if et_surface_m is not None else None),
            et_max_rate_m_day=(
                float(et_max_rate_m_day) if et_max_rate_m_day is not None else None
            ),
            et_extinction_depth_m=(
                float(et_extinction_depth_m)
                if et_extinction_depth_m is not None
                else None
            ),
            n_periods=int(n_periods) if n_periods is not None else None,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            specific_yield=specific_yield,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except WetlandHydroperiodInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except WetlandHydroperiodScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(
                exc, "error_code", "WETLAND_HYDROPERIOD_SCENARIO_ERROR"
            ),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
