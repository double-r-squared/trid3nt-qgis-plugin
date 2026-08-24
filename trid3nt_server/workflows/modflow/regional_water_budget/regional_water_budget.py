"""Engine template ``modflow_regional_water_budget`` - MODFLOW 6 regional budget.

Declared as PARAMS + ``plan(p, d)``: the tool body normalizes the wire args,
resolves the doors, validates the plan and hands it to the interpreter. The
archetype pipeline is the shared MODFLOW step family
(``workflows/modflow/steps``). See ``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.modflow_contracts import BudgetPartitionLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_latlon
from trid3nt_server.declarative import (
    DeclarativeError,
    FormGate,
    Param,
    Workflow,
    doors,
    interpret,
    merge_provenance,
    render_docstring,
    resolve_params,
)
from trid3nt_server.workflows.modflow._template_card import TemplateCard
from trid3nt_server.workflows.modflow.steps import (
    ModflowStepError,
    RunArchetype,
    run_id_of,
)
from trid3nt_server.workflows.shared.run_products import persist_run_products

logger = logging.getLogger(
    "trid3nt_server.workflows.modflow.regional_water_budget.regional_water_budget"
)

__all__ = ["DATA", "PARAMS", "modflow_regional_water_budget", "plan"]

_STEPS = "trid3nt_server.workflows.modflow.steps"

#: The zone splits the archetype deck writer understands. Anything else is a model
#: invention that used to reach mf6 and partition nothing.
_ZONE_SCHEMES = frozenset({"upgradient_downgradient"})


TEMPLATE_CARD = TemplateCard(
    question="a regional groundwater water-budget partition (where the water goes)",
    required_inputs=["location (or aoi_latlon)"],
    knobs="zone_partition, aquifer_k_ms, porosity",
)


PARAMS: tuple[Param, ...] = (
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name for the modeled region, geocoded to the AOI point"),
    Param("aoi_latlon", door=doors.DERIVED, resolve=f"{_STEPS}.aoi.aoi_latlon",
          consequence="aoi",
          desc="AOI point (lat, lon) EPSG:4326; geocoded from location unless "
               "supplied"),
    Param("location_name", door=doors.DERIVED,
          resolve=f"{_STEPS}.aoi.location_name", consequence="aoi",
          desc="What the run narrates the region as"),

    Param("zone_partition", door=doors.USER, optional=True, consequence="scenario",
          derived_when_absent="the budget is reported for the whole domain, unsplit",
          desc="Zone-split scheme for the partition: upgradient_downgradient"),

    Param("aquifer_k_ms", door=doors.DERIVED,
          resolve=f"{_STEPS}.aquifer.aquifer_k_ms", user_lever=True,
          bounds=(1.0e-9, 1.0), units="m/s", consequence="physics",
          desc="Aquifer saturated hydraulic conductivity; SoilGrids-derived at the "
               "AOI via the Saxton-Rawls pedotransfer function unless supplied - a "
               "SCREENING near-surface proxy, NOT a measured aquifer K"),
    Param("porosity", door=doors.DERIVED, resolve=f"{_STEPS}.aquifer.porosity",
          user_lever=True, bounds=(0.01, 0.7), consequence="physics",
          desc="Effective porosity, from the same SoilGrids texture fit unless "
               "supplied"),

    Param("compute_class", door=doors.CONSTANT, default="standard",
          consequence="numerical", desc="Solve sizing class"),
)

#: The archetype fetches nothing it hands back as an artifact: the AOI point and
#: the aquifer properties are point SAMPLES, which the doors resolve as declared
#: params, and the deck + grid are the solver's own.
DATA: tuple = ()


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The regional-budget recipe. Pure: constructs the plan value, executes nothing."""
    return Workflow("modflow_regional_water_budget", engine="modflow6")[
        FormGate(title="Review the regional water-budget inputs"),
        RunArchetype.regional_water_budget(
            expected_type="trid3nt_contracts.modflow_contracts.BudgetPartitionLayerURI",
            aoi_latlon=p.aoi_latlon, zone_partition=p.zone_partition,
            aquifer_k_ms=p.aquifer_k_ms, porosity=p.porosity,
            compute_class=p.compute_class,
            tool_label="Model regional water budget",
        ).named("budget")
         .chart("budget_partition", builder=f"{_STEPS}.products.build_budget_chart"),
    ]


_METADATA = AtomicToolMetadata(
    name="modflow_regional_water_budget",
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
async def modflow_regional_water_budget(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | str | None = None,
    zone_partition: str | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
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
        logger.warning("modflow_regional_water_budget %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except ModflowStepError as exc:
        # A DERIVATION refuses before the plan is ever built (the AOI, the law-9
        # aquifer properties), so its typed code never passes through a step.
        logger.warning("modflow_regional_water_budget %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            raise
        logger.exception("modflow_regional_water_budget unexpected failure")
        return {"status": "error", "error_code": "MODFLOW_INTERNAL_ERROR",
                "error_message": str(exc)}

    layer: BudgetPartitionLayerURI = result.value
    layer = layer.model_copy(update={
        "synthetic_inputs": merge_provenance(layer.synthetic_inputs or [],
                                             result.entries),
    })
    # The run's OWN sheet, not the one this call resolved: a form gate may have
    # revised it, and what is narrated has to be what ran.
    ran = result.params or p
    await persist_run_products(run_id_of(layer), charts=result.charts,
                               metrics=_physical_answer(layer, ran))
    logger.info(
        "modflow_regional_water_budget complete layer_id=%s terms=%s executed=%s "
        "replayed=%s notes=%s",
        layer.layer_id, sorted(layer.budget_partition_m3_day), result.executed,
        result.replayed, result.notes,
    )
    point = ran.get("aoi_latlon")
    return {
        "schema_version": "v1",
        "budget_layer": layer.model_dump(mode="json"),
        "derived_params": {
            "location_name": ran.get("location_name"),
            "aoi_latlon": [float(point[0]), float(point[1])] if point else None,
            "zone_partition": ran.get("zone_partition"),
        },
        "summary": {
            "location_name": ran.get("location_name"),
            "budget_partition_m3_day": dict(layer.budget_partition_m3_day),
            "zone_partition": ran.get("zone_partition"),
            "aquifer_provenance": _aquifer_provenance(ran),
        },
    }


def _aquifer_provenance(p: Any) -> str:
    """What the narration says about where K and porosity came from.

    Read off the run's OWN resolved rows, so the prose cannot drift from the
    machine-readable provenance the layer carries.
    """
    parts: list[str] = []
    for name, label in (("aquifer_k_ms", "Aquifer K"),
                        ("porosity", "Effective porosity")):
        row = p.row(name)
        if row is not None and row.note:
            parts.append(f"{label} {row.note}.")
    parts.append("The budget partition is a planning-level illustration, not a "
                 "calibrated model.")
    return " ".join(parts)


def _physical_answer(layer: BudgetPartitionLayerURI, p: Any) -> dict[str, Any]:
    """The run's ANSWER, as the numbers a reader has to be able to check."""
    return {
        "budget_partition_m3_day": dict(layer.budget_partition_m3_day),
        "zone_partition": p.get("zone_partition"),
        "aquifer_k_ms": p.get("aquifer_k_ms"),
        "porosity": p.get("porosity"),
        "location_name": p.get("location_name"),
        "layer_uri": layer.uri,
    }


def _normalize(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Coerce the wire args to the door-1 sheet: at least one of location/aoi_latlon."""
    location = args.get("location")
    has_loc = bool(location and str(location).strip())
    point = args.get("aoi_latlon")
    coerced: tuple[float, float] | None = None
    if point is not None:
        try:
            coerced = tuple(coerce_latlon(point))  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 - a bad point is the caller's, typed back
            return {}, {"status": "error",
                        "error_code": "REGIONAL_WATER_BUDGET_INPUT_INVALID",
                        "error_message": f"invalid aoi_latlon: {point!r}"}
    if not has_loc and coerced is None:
        return {}, {"status": "error",
                    "error_code": "REGIONAL_WATER_BUDGET_INPUT_INVALID",
                    "error_message": (
                        "modflow_regional_water_budget needs a place `location` "
                        "(geocoded) or an explicit `aoi_latlon` point.")}

    zone = args.get("zone_partition")
    if zone is not None and str(zone).strip().lower() not in _ZONE_SCHEMES:
        logger.warning("modflow_regional_water_budget: unknown zone_partition %r - "
                       "reporting the whole-domain budget", zone)
        zone = None

    declared = {p.name for p in PARAMS}
    supplied = {k: v for k, v in args.items() if k in declared and v is not None}
    supplied["location"] = location if has_loc else None
    supplied["aoi_latlon"] = coerced
    supplied["zone_partition"] = zone
    supplied["compute_class"] = str(args.get("compute_class") or "standard")
    return {k: v for k, v in supplied.items() if v is not None}, None


_DOC = dict(
    summary="A REGIONAL GROUNDWATER WATER-BUDGET partition - where the water goes.",
    routing=(
        "THE tool for \"water budget for this aquifer\", \"where does the regional "
        "groundwater come from and go\", a zonal groundwater balance, a flow-"
        "accounting summary for an area. Builds a steady MODFLOW 6 regional "
        "groundwater-flow model (a west-to-east regional gradient over the domain "
        "grid), solves it, reads the cell-by-cell flow budget and partitions it by "
        "term (CHD inflow / outflow across the gradient, storage, any wells). "
        "Planning-grade envelope, not a calibrated regulatory model; aquifer K and "
        "porosity are SoilGrids-derived at the AOI or REFUSED (law 9). Supply a "
        "place `location` (geocoded) OR an explicit `aoi_latlon`."
    ),
    not_for=(
        "a pumping-well drawdown cone (`modflow_sustainable_yield`); mine-pit "
        "dewatering (`modflow_mine_dewatering`); a contaminant spill plume "
        "(`modflow_contaminant_plume`); surface-water flooding (`sfincs_flood`); "
        "urban storm-sewer flooding (`swmm_urban_flood`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved param sheet - including the '
         "SoilGrids-derived aquifer K and porosity - for review/edit before the "
         'solve and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same "
         "invocation left behind and re-runs every step from the top. Default "
         "False resumes at the failed step."),
    ),
    returns=(
        "On success a dict with `budget_layer` (a `BudgetPartitionLayerURI` "
        "carrying `budget_partition_m3_day` - the agent narrates those typed "
        "numbers), `derived_params` and `summary`. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

modflow_regional_water_budget.__doc__ = render_docstring(**_DOC)
modflow_regional_water_budget.routing_doc = render_docstring(**_DOC, view="routing")
