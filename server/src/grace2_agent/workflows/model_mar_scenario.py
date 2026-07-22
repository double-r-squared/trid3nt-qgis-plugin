"""``model_mar_scenario``  -  MODFLOW managed-aquifer-recharge (MAR) composer.

The end-to-end higher-order workflow for the sprint-18 Wave-2 MODFLOW ``MAR``
archetype: it turns a place (or AOI point) + an infiltration-basin footprint into
a rendered groundwater-mounding layer  -  how high the water table rises under a
recharge basin and how much water is banked. It mirrors the chain shape of
``model_mine_dewatering_scenario`` (sibling footprint-driven archetype): the basin
footprint is draped as RCH recharge cells over an unconfined transient water
table, and the head RISE (mounding) IS the deliverable.

Canonical real-world pipeline mirrored here (a managed-aquifer-recharge mounding
analysis  -  the standard MODFLOW MAR / infiltration-basin water-banking study):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the basin footprint polygon + recharge rate (NEVER
           fabricated  -  a missing basin is a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="MAR", basin, rate, months, ...)
        -> run_modflow_archetype_job (GWF transient RCH deck -> mf6 -> mounding)
        -> MoundingLayerURI (max_mounding_m + recharged_volume_m3)

Invariants (same set as model_mine_dewatering_scenario):
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A ``MAR`` run with no basin footprint returns
  a typed ``USER_INPUT_REQUIRED`` failed envelope rather than inventing a basin  -
  the honesty floor: a "modeled" envelope with empty layers never reads ok.
- **10. Minimal parameter surface: preserves.** Intent (place + basin + recharge
  rate + months) is exposed; the grid + demo aquifer K / Sy are derived defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from grace2_contracts.common import GraceModel
from grace2_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    MODFLOWRunArgs,
    MoundingLayerURI,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter, emit_chart_payloads
from ..tools import register_tool
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes) + the footprint normalizer from
# the mine_dewatering composer (one polygon-coercion implementation).
from .model_mine_dewatering_scenario import _normalize_pit_footprint
from .model_sustainable_yield_scenario import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("grace2_agent.workflows.model_mar_scenario")

__all__ = [
    "MARResult",
    "model_mar_scenario",
    "run_model_mar_scenario",
    "MARScenarioError",
    "MARInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class MARResult(GraceModel):
    """Return type for ``model_mar_scenario`` (sprint-18 Wave-2).

    Bundles the mounding layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``mounding_layer``
    carries ``max_mounding_m`` + ``recharged_volume_m3``.
    """

    schema_version: str = "v1"

    mounding_layer: MoundingLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class MARScenarioError(RuntimeError):
    """Base class for ``model_mar_scenario`` failures."""

    error_code: str = "MAR_SCENARIO_ERROR"
    retryable: bool = False


class MARInputError(MARScenarioError):
    """Caller supplied invalid / missing AOI or basin input (honesty gate)."""

    error_code = "MAR_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Engine-output chart (mirror the head-decline emit): MAR mounding summary.
#
# MoundingLayerURI carries the two typed mounding scalars (max_mounding_m +
# recharged_volume_m3) but NO per-step head series (the schema deliberately gives
# MAR the recharged-VOLUME scalar, not a head series), so the MAR chart is a
# two-metric summary bar built from those real typed numbers  -  never fabricated;
# emits nothing when both are absent (the honesty floor).
# --------------------------------------------------------------------------- #


async def _emit_mounding_chart(layer: MoundingLayerURI) -> None:
    """Side-emit the MAR mounding summary chart (best-effort, no-op safe)."""
    from ..tools.chart_tools import build_chart_payload

    rows: list[dict[str, Any]] = []
    peak = getattr(layer, "max_mounding_m", None)
    vol = getattr(layer, "recharged_volume_m3", None)
    if peak is not None and float(peak) > 0.0:
        rows.append({"metric": "peak mounding (m)", "value": float(peak)})
    if vol is not None and float(vol) > 0.0:
        rows.append(
            {"metric": "recharged volume (1000 m^3)", "value": float(vol) / 1000.0}
        )
    if not rows:
        return
    spec = {
        "title": "Managed aquifer recharge - mounding summary",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {"field": "metric", "type": "nominal", "title": "metric", "sort": None},
            "y": {"field": "value", "type": "quantitative", "title": "value"},
            "color": {
                "field": "metric",
                "type": "nominal",
                "scale": {"scheme": "blues"},
                "legend": None,
            },
        },
        "width": "container",
    }
    caption_parts = []
    if peak is not None and float(peak) > 0.0:
        caption_parts.append(f"peak mound {float(peak):.3g} m")
    if vol is not None and float(vol) > 0.0:
        caption_parts.append(f"recharged {float(vol):,.4g} m^3")
    chart = build_chart_payload(
        vega_lite_spec=spec,
        title="MAR mounding summary",
        caption=" · ".join(caption_parts) or "managed aquifer recharge result",
        source_layer_uri=getattr(layer, "uri", None),
    )
    await emit_chart_payloads(chart)


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_mar_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    basin_footprint_lonlat: Any | None = None,
    infiltration_rate_m_day: float | None = None,
    recharge_months: int | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    aquifer_sy: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> MARResult:
    """Compose place/AOI + a recharge basin -> MODFLOW mounding -> MoundingLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        basin_footprint_lonlat: the infiltration-basin outline as
            ``[(lon, lat), ...]`` (or a GeoJSON polygon). REQUIRED  -  a missing
            basin is a typed USER_INPUT_REQUIRED failure (never invented).
        infiltration_rate_m_day: applied recharge rate over the basin (m/day).
            Demo default applied by the adapter when None.
        recharge_months: number of months the basin floods (>= 1). Demo default
            applied by the adapter when None.
        n_periods: explicit transient period override (alternative to months).
        aquifer_k_ms / porosity / aquifer_sy: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``MARResult`` with the ``MoundingLayerURI`` + derived args + a narration
        summary dict.

    Raises:
        MARInputError: missing/invalid AOI or basin (the honesty gate).
        MARScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the basin -----------------
    if basin_footprint_lonlat is None:
        raise MARInputError(
            "MAR requires an infiltration-basin footprint (basin_footprint_lonlat). "
            "The basin outline is a user input and is never invented; ask the user "
            "to draw / supply the basin polygon."
        )
    try:
        basin_verts = _normalize_pit_footprint(basin_footprint_lonlat)
    except Exception as exc:  # noqa: BLE001  -  re-raise as our typed input error
        raise MARInputError(
            f"invalid basin_footprint_lonlat (expected [(lon, lat), ...]): {exc}"
        ) from exc

    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",
            release_rate_kg_s=1.0,
            duration_days=1.0,
            archetype="MAR",
            basin_footprint_lonlat=basin_verts,
            infiltration_rate_m_day=infiltration_rate_m_day,
            recharge_months=recharge_months,
            n_periods=n_periods,
            **_aquifer_overrides(aquifer_k_ms, porosity, aquifer_sy, None),
        )
    except Exception as exc:  # noqa: BLE001
        raise MARInputError(f"invalid MAR run arguments: {exc}") from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model managed aquifer recharge [{len(basin_verts)} basin vertices]",
        expected_type=MoundingLayerURI,
        error_code="MAR_RUN_FAILED",
        scenario_error=MARScenarioError,
    )

    # Mirror the head-decline emit: side-emit the mounding summary chart from the
    # typed MoundingLayerURI scalars (real solver output, never fabricated).
    await _emit_mounding_chart(layer)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "basin_vertex_count": len(basin_verts),
        "infiltration_rate_m_day": infiltration_rate_m_day,
        "recharge_months": recharge_months,
        "n_periods": n_periods,
    }
    summary = {
        "location_name": location_name,
        "max_mounding_m": layer.max_mounding_m,
        "recharged_volume_m3": layer.recharged_volume_m3,
        "basin_vertex_count": len(basin_verts),
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, the specific yield, and the "
            "recharge rate / duration are demo defaults, not site-specific "
            "hydrogeology."
        ),
    }
    logger.info(
        "MAR scenario complete location=%r max_mounding_m=%.6g recharged_volume_m3=%s",
        location_name,
        layer.max_mounding_m,
        layer.recharged_volume_m3,
    )
    return MARResult(
        mounding_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_mar_scenario",
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
async def run_model_mar_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    basin_footprint_lonlat: Any | None = None,
    infiltration_rate_m_day: float | None = None,
    recharge_months: int | None = None,
    n_periods: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    aquifer_sy: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a managed-aquifer-recharge (MAR) groundwater mound under a basin.

    Builds a transient MODFLOW 6 groundwater-flow model with an unconfined water
    table and an RCH recharge package over the user-supplied infiltration-basin
    footprint, runs it, and produces a MOUNDING layer: how high the water table
    rises under the basin (the mound) and the total volume of water recharged into
    the aquifer. Use this to assess managed aquifer recharge / water banking /
    infiltration-basin mounding.

    Use this when:
        - The user asks how much an infiltration / recharge basin raises the water
          table (the mound), managed aquifer recharge, or aquifer water banking.

    Do NOT use this for:
        - A pumping-well drawdown cone (use ``run_model_sustainable_yield_scenario``).
        - Aquifer storage & recovery cycling (use ``run_model_asr_scenario``).
        - A contaminant spill plume (use ``run_modflow_job``).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        basin_footprint_lonlat: the basin outline as ``[(lon, lat), ...]`` or a
            GeoJSON polygon. REQUIRED  -  never invented; ask the user if absent.
        infiltration_rate_m_day: applied recharge rate (m/day). Demo default if None.
        recharge_months: months the basin floods (>= 1). Demo default if None.
        n_periods: explicit transient period override.
        aquifer_k_ms / porosity / aquifer_sy: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``MARResult`` JSON dict with the ``mounding_layer`` (a
        ``MoundingLayerURI`` carrying ``max_mounding_m`` + ``recharged_volume_m3``  -
        the agent narrates these typed numbers), the ``derived_params``, and the
        ``summary``. On a recoverable failure (incl. a missing basin) the tool
        returns a typed error the agent narrates honestly  -  it never fabricates a
        basin.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await model_mar_scenario(
            location=location,
            aoi_latlon=aoi,
            basin_footprint_lonlat=basin_footprint_lonlat,
            infiltration_rate_m_day=(
                float(infiltration_rate_m_day)
                if infiltration_rate_m_day is not None
                else None
            ),
            recharge_months=(
                int(recharge_months) if recharge_months is not None else None
            ),
            n_periods=int(n_periods) if n_periods is not None else None,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            aquifer_sy=aquifer_sy,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except MARInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except MARScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "MAR_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
