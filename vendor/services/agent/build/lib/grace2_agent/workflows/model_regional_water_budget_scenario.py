"""``model_regional_water_budget_scenario``  -  MODFLOW zonal-budget composer.

The end-to-end higher-order workflow for the sprint-18 Wave-1 MODFLOW
``regional_water_budget`` archetype: it turns a place (or AOI point) into a
narrated regional water-budget partition  -  where the regional groundwater goes
(CHD inflow / outflow across the gradient, storage, any wells). It mirrors the
chain shape of ``model_sustainable_yield_scenario`` (sibling GWF-only archetype):
a steady GWF run with no new stress package; the deliverable is the cell-by-cell
budget partition read agent-side, rendered over the water-table head.

Unlike the well / pit archetypes, this one needs NO user-supplied geometry  -  the
regional gradient + grid are the demo substrate  -  so there is no fabricated-input
risk beyond the AOI resolution. The optional ``zone_partition`` splits the domain
(e.g. upgradient vs downgradient) for a finer partition.

Canonical real-world pipeline mirrored here (a regional groundwater water-budget
/ flow-accounting analysis):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> assemble MODFLOWRunArgs(archetype="regional_water_budget", zone?)
        -> run_modflow_archetype_job (GWF steady deck -> mf6 -> CBC partition)
        -> BudgetPartitionLayerURI (budget_partition_m3_day dict, real CBC terms)

Invariants (same set as model_sustainable_yield_scenario):
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated outputs.** The partition is built ONLY from real CBC budget
  terms the postprocess measured; a run with no non-trivial budget term returns
  a typed empty-result error (the honesty floor) rather than a fabricated budget.
- **10. Minimal parameter surface: preserves.** Intent (the place + an optional
  zone split) is exposed; the grid + regional gradient are the demo substrate.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from grace2_contracts.common import GraceModel
from grace2_contracts.modflow_contracts import (
    BudgetPartitionLayerURI,
    MODFLOWRunArgs,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
)
from ..tools import register_tool
from .model_sustainable_yield_scenario import (
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger(
    "grace2_agent.workflows.model_regional_water_budget_scenario"
)

__all__ = [
    "RegionalWaterBudgetResult",
    "model_regional_water_budget_scenario",
    "run_model_regional_water_budget_scenario",
    "RegionalWaterBudgetScenarioError",
    "RegionalWaterBudgetInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class RegionalWaterBudgetResult(GraceModel):
    """Return type for ``model_regional_water_budget_scenario`` (sprint-18 Wave-1).

    Bundles the budget-partition layer + the derived args + a narration summary.
    Invariant 1: the narrated budget is the typed ``budget_layer
    .budget_partition_m3_day`` dict  -  never free-generated.
    """

    schema_version: str = "v1"

    budget_layer: BudgetPartitionLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class RegionalWaterBudgetScenarioError(RuntimeError):
    """Base class for ``model_regional_water_budget_scenario`` failures."""

    error_code: str = "REGIONAL_WATER_BUDGET_SCENARIO_ERROR"
    retryable: bool = False


class RegionalWaterBudgetInputError(RegionalWaterBudgetScenarioError):
    """Caller supplied invalid / missing AOI input."""

    error_code = "REGIONAL_WATER_BUDGET_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_regional_water_budget_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    zone_partition: str | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> RegionalWaterBudgetResult:
    """Compose place/AOI -> MODFLOW steady GWF -> regional CBC budget partition.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        zone_partition: optional zone-split scheme (e.g.
            ``"upgradient_downgradient"``). None = whole-domain budget only.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``RegionalWaterBudgetResult`` with the ``BudgetPartitionLayerURI`` +
        derived args + a narration summary dict.

    Raises:
        RegionalWaterBudgetInputError: missing/invalid AOI.
        RegionalWaterBudgetScenarioError: a required step (geocode / solver)
            failed, or the run produced an empty budget (the honesty floor).
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    try:
        lat, lon, location_name = await _resolve_aoi_point(
            location, aoi_latlon, pipeline_emitter=pipeline_emitter
        )
    except Exception as exc:  # noqa: BLE001  -  map the shared input error to ours
        # _resolve_aoi_point raises SustainableYieldInputError on a bad AOI; we
        # re-raise as our own typed input error for honest narration.
        from .model_sustainable_yield_scenario import SustainableYieldInputError

        if isinstance(exc, SustainableYieldInputError):
            raise RegionalWaterBudgetInputError(str(exc)) from exc
        raise

    try:
        overrides: dict[str, Any] = {}
        if aquifer_k_ms is not None:
            overrides["aquifer_k_ms"] = float(aquifer_k_ms)
        if porosity is not None:
            overrides["porosity"] = float(porosity)
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",
            release_rate_kg_s=1.0,
            duration_days=1.0,
            archetype="regional_water_budget",
            zone_partition=zone_partition,
            **overrides,
        )
    except Exception as exc:  # noqa: BLE001
        raise RegionalWaterBudgetInputError(
            f"invalid regional_water_budget run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label="Model regional water budget",
        expected_type=BudgetPartitionLayerURI,
        error_code="REGIONAL_WATER_BUDGET_RUN_FAILED",
        scenario_error=RegionalWaterBudgetScenarioError,
    )

    # task-198: wire the CBC budget partition to a signed inflow/outflow bar
    # chart. Real solver terms (BudgetPartitionLayerURI.budget_partition_m3_day,
    # FLOW-JA-FACE already excluded upstream) - the builder emits nothing when
    # the partition is empty (the honesty floor).
    from ..tools.chart_tools import build_budget_partition_chart

    _budget_chart = build_budget_partition_chart(
        budget_partition_m3_day=dict(layer.budget_partition_m3_day),
        source_layer_uri=getattr(layer, "uri", None),
    )
    await emit_chart_payloads(_budget_chart)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "zone_partition": zone_partition,
    }
    summary = {
        "location_name": location_name,
        "budget_partition_m3_day": dict(layer.budget_partition_m3_day),
        "zone_partition": zone_partition,
    }
    logger.info(
        "regional_water_budget scenario complete location=%r terms=%s",
        location_name,
        sorted(layer.budget_partition_m3_day),
    )
    return RegionalWaterBudgetResult(
        budget_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_regional_water_budget_scenario",
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
async def run_model_regional_water_budget_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    zone_partition: str | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a regional groundwater water-budget partition (where the water goes).

    Builds a steady MODFLOW 6 regional groundwater-flow model (a west->east
    regional gradient over the demo grid), runs it, reads the cell-by-cell flow
    budget, and partitions it by term (CHD inflow / outflow across the gradient,
    storage, any wells)  -  narrating where the regional groundwater enters and
    leaves the domain. Use this for a regional flow-accounting / water-budget
    summary. The budget is built ONLY from real solver budget terms (never
    fabricated).

    Use this when:
        - The user asks for a regional groundwater water budget, where the water
          goes / comes from across an area, or a flow-accounting summary.

    Do NOT use this for:
        - A pumping-well drawdown cone (use ``run_model_sustainable_yield_scenario``).
        - Mine-pit dewatering (use ``run_model_mine_dewatering_scenario``).
        - A contaminant spill plume (use ``run_modflow_job``).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        zone_partition: optional zone-split scheme (e.g.
            ``"upgradient_downgradient"``). None = whole-domain budget.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``RegionalWaterBudgetResult`` JSON dict with the
        ``budget_layer`` (a ``BudgetPartitionLayerURI`` carrying the
        ``budget_partition_m3_day`` dict  -  the agent narrates these typed numbers),
        the ``derived_params``, and the ``summary``. On a recoverable failure the
        tool returns a typed error the agent narrates honestly.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await model_regional_water_budget_scenario(
            location=location,
            aoi_latlon=aoi,
            zone_partition=zone_partition,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except RegionalWaterBudgetInputError as exc:
        return {
            "status": "error",
            "error_code": "REGIONAL_WATER_BUDGET_INPUT_INVALID",
            "error_message": str(exc),
        }
    except RegionalWaterBudgetScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(
                exc, "error_code", "REGIONAL_WATER_BUDGET_SCENARIO_ERROR"
            ),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
