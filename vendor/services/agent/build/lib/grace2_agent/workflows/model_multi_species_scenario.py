"""``model_multi_species_scenario``  -  MODFLOW Wave-3 N-species plume composer.

The end-to-end higher-order workflow for the sprint-18 Wave-3 MODFLOW
``multi_species`` archetype: it turns a spill point + a user-supplied list of
solute species (each with its own release rate / sorption / decay, optionally a
parent->daughter chain link) into N rendered groundwater-plume layers from ONE
shared GWF flow field. It is the multi-solute analogue of the single-contaminant
Case 2 composer (``model_groundwater_contamination_scenario``): same spill point +
demo aquifer, but N independent ModflowGwt transport models instead of one, each
producing its own ``PlumeLayerURI``.

Canonical real-world pipeline mirrored here (a chlorinated-solvent multi-species
groundwater-transport study  -  e.g. a TCE source that degrades to cis-DCE then
VC, each a distinct plume with distinct toxicity / footprint):

    resolve the spill point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the species list (NEVER fabricated  -  a missing /
           empty list, or no positive release rate, is a typed USER_INPUT_REQUIRED
           failure: we never invent a contaminant)
        -> assemble MODFLOWRunArgs(archetype="multi_species", species=[...])
        -> run_modflow_multi_species_job (ONE shared GWF + N GWT -> mf6 -> N .ucn)
        -> postprocess_multi_species -> N PlumeLayerURI (one per species)
        -> emit a per-species concentration summary chart from the typed scalars.

Invariants:
- **1. Determinism boundary: preserves.** Every narrated number comes from a typed
  field  -  each ``PlumeLayerURI``'s ``max_concentration_mgl`` / ``plume_area_km2``
  and the derived-args dict. No LLM call anywhere; the summary + chart are built
  from those typed fields, never free-generated.
- **2. Deterministic workflows: preserves.** Straight-line Python composition over
  the registered run-tool + postprocess; typed-exception handling at the boundary.
- **8. Cancellation is first-class: preserves.** Every ``await`` is a
  cancel-propagation site; ``asyncio.CancelledError`` bubbles untouched.
- **9. No fabricated model inputs.** A multi_species run with no species, an empty
  list, a species missing a name, or NO species carrying a positive release rate
  returns a typed ``USER_INPUT_REQUIRED`` failed envelope rather than inventing a
  contaminant  -  a "modeled" envelope with empty layers never reads ok.
- **10. Minimal parameter surface: preserves.** Intent (the spill point + the
  species list) is exposed; the grid + the demo aquifer K / porosity are derived
  defaults the user does not supply.
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
    MODFLOWRunArgs,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SpeciesSpec,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from ..tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger("grace2_agent.workflows.model_multi_species_scenario")

__all__ = [
    "MultiSpeciesResult",
    "model_multi_species_scenario",
    "run_model_multi_species_scenario",
    "MultiSpeciesScenarioError",
    "MultiSpeciesInputError",
    "normalize_species_list",
]


# --------------------------------------------------------------------------- #
# Result envelope (kept LOCAL to the agent; see Case 2 composer rationale)
# --------------------------------------------------------------------------- #


class MultiSpeciesResult(GraceModel):
    """Return type for ``model_multi_species_scenario`` (sprint-18 Wave-3).

    Bundles the N per-species plume layers + the derived args + a narration
    summary dict. Invariant 1: every narrated number is a typed field  -  each
    ``plume_layers[i]`` carries ``max_concentration_mgl`` + ``plume_area_km2``.

    Fields:
        plume_layers: ordered list of one ``PlumeLayerURI`` per species (same
            order as the input species list). At least one.
        derived_params: JSON-able derived-args dict (spill point, species specs).
        summary: narration dict ``{location_name, species: [{name, ...}], ...}``.
    """

    schema_version: str = "v1"

    plume_layers: list[PlumeLayerURI] = Field(min_length=1)
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class MultiSpeciesScenarioError(RuntimeError):
    """Base class for ``model_multi_species_scenario`` failures."""

    error_code: str = "MULTI_SPECIES_SCENARIO_ERROR"
    retryable: bool = False


class MultiSpeciesInputError(MultiSpeciesScenarioError):
    """Caller supplied invalid / missing spill point or species list (honesty gate)."""

    error_code = "MULTI_SPECIES_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Registry / coercion helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise MultiSpeciesScenarioError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


def normalize_species_list(species: Any) -> list[SpeciesSpec]:
    """Coerce a heterogeneous species input into validated ``SpeciesSpec`` list.

    Accepts a list of ``SpeciesSpec`` objects OR plain dicts (the wire form the
    LLM passes). Validates each through the ``SpeciesSpec`` contract (non-empty
    name, release rate >= 0, optional sorption / decay / parent). The HONESTY
    floor lives here: an empty list, a non-list, a malformed species, OR a list
    where NO species carries a positive release rate raises
    ``MultiSpeciesInputError`` (we never invent a contaminant / source).

    Returns the validated, ordered ``SpeciesSpec`` list.

    Raises:
        MultiSpeciesInputError: the list is empty / malformed / sourceless.
    """
    if species is None:
        raise MultiSpeciesInputError(
            "multi_species requires a non-empty list of species (each with a name "
            "and a release rate). The species are a user input and are never "
            "invented; ask the user which contaminants were released."
        )
    if not isinstance(species, (list, tuple)) or len(species) == 0:
        raise MultiSpeciesInputError(
            "multi_species requires a non-empty list of species; "
            f"got {type(species).__name__} with no entries."
        )
    specs: list[SpeciesSpec] = []
    for raw in species:
        if isinstance(raw, SpeciesSpec):
            specs.append(raw)
            continue
        if not isinstance(raw, dict):
            raise MultiSpeciesInputError(
                f"each species must be a SpeciesSpec or a dict; got "
                f"{type(raw).__name__}."
            )
        try:
            specs.append(SpeciesSpec(**raw))
        except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
            raise MultiSpeciesInputError(
                f"invalid species spec {raw!r}: {exc}"
            ) from exc
    # Names must be unique (the adapter keys GWT models on the species name).
    names = [s.name for s in specs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise MultiSpeciesInputError(
            f"species names must be unique; duplicated: {sorted(dupes)}."
        )
    # At least one species must carry a real source (a pure daughter-only list
    # would model nothing  -  the honesty floor: never a sourceless 'modeled' run).
    if not any(float(s.release_rate_kg_s) > 0.0 for s in specs):
        raise MultiSpeciesInputError(
            "at least one species must have a positive release_rate_kg_s; a list "
            "of pure daughter products (all release rates 0) has no source to "
            "model. Ask the user for the released (parent) contaminant + amount."
        )
    return specs


def _coerce_optional_latlon(value: Any) -> tuple[float, float] | None:
    """Coerce an optional lat/lon arg (str / list / tuple) -> (lat, lon) or None."""
    if value is None:
        return None
    from ..tool_arg_normalizer import coerce_latlon

    return tuple(coerce_latlon(value))  # type: ignore[return-value]


async def _maybe_emit(
    emitter: Any | None,
    *,
    name: str,
    tool_name: str,
    invoke: Any,
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(
            name=name, tool_name=tool_name, invoke=invoke
        )
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def _resolve_spill_point(
    location: str | None,
    spill_location_latlon: tuple[float, float] | None,
    *,
    pipeline_emitter: Any | None,
) -> tuple[float, float, str]:
    """Resolve (lat, lon, name) from a place string OR an explicit spill point.

    Exactly one of ``location`` / ``spill_location_latlon`` must be supplied.
    """
    has_loc = bool(location and location.strip())
    has_point = spill_location_latlon is not None
    if has_loc == has_point:
        raise MultiSpeciesInputError(
            "supply exactly one of location or spill_location_latlon "
            f"(got location={has_loc}, spill_location_latlon={has_point})."
        )
    if has_point:
        lat = float(spill_location_latlon[0])  # type: ignore[index]
        lon = float(spill_location_latlon[1])  # type: ignore[index]
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
        raise MultiSpeciesScenarioError(
            f"geocode_location({location!r}) returned no centroid lat/lon."
        )
    return float(glat), float(glon), str(location)


# --------------------------------------------------------------------------- #
# Per-species concentration chart (one multi-series chart; typed scalars only)
# --------------------------------------------------------------------------- #


async def _emit_multi_species_chart(plumes: list[PlumeLayerURI]) -> None:
    """Side-emit a per-species plume summary chart (best-effort, no-op safe).

    A grouped bar over the N species, each with its real typed
    ``max_concentration_mgl`` + ``plume_area_km2`` (Invariant 1: never fabricated).
    Emits nothing when every plume is empty (the honesty floor).
    """
    from ..tools.chart_tools import build_chart_payload

    rows: list[dict[str, Any]] = []
    for p in plumes:
        species = _species_name_from_layer(p)
        conc = float(getattr(p, "max_concentration_mgl", 0.0) or 0.0)
        area = float(getattr(p, "plume_area_km2", 0.0) or 0.0)
        if conc > 0.0:
            rows.append(
                {"species": species, "metric": "peak conc (mg/L)", "value": conc}
            )
        if area > 0.0:
            rows.append(
                {"species": species, "metric": "plume area (km^2)", "value": area}
            )
    if not rows:
        return
    spec = {
        "title": "Multi-species plumes - per-species summary",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {"field": "species", "type": "nominal", "title": "species"},
            "y": {"field": "value", "type": "quantitative", "title": "value"},
            "color": {"field": "metric", "type": "nominal", "title": "metric"},
            "xOffset": {"field": "metric", "type": "nominal"},
        },
        "width": "container",
    }
    caption = " · ".join(
        f"{_species_name_from_layer(p)}: peak {float(getattr(p, 'max_concentration_mgl', 0.0)):.3g} mg/L, "
        f"{float(getattr(p, 'plume_area_km2', 0.0)):.3g} km^2"
        for p in plumes
    )
    chart = build_chart_payload(
        vega_lite_spec=spec,
        title="Multi-species plume summary",
        caption=caption or "multi-species groundwater plumes",
        source_layer_uri=getattr(plumes[0], "uri", None) if plumes else None,
    )
    await emit_chart_payloads(chart)


def _species_name_from_layer(plume: PlumeLayerURI) -> str:
    """Recover the species label from a ``PlumeLayerURI`` name for the chart axis.

    The postprocess names each layer ``Contaminant Plume - <species> (peak ...)``;
    extract ``<species>`` for a compact chart axis. Falls back to the layer id.
    """
    name = getattr(plume, "name", "") or ""
    if " - " in name:
        tail = name.split(" - ", 1)[1]
        # strip the trailing " (peak concentration)" qualifier.
        return tail.split(" (", 1)[0].strip() or name
    return name or getattr(plume, "layer_id", "species")


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_multi_species_scenario(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | None = None,
    *,
    species: Any = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    duration_days: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> MultiSpeciesResult:
    """Compose spill point + a species list -> MODFLOW N-species -> N plume layers.

    Args:
        location: a place name (geocoded to the spill point). Supply this OR
            ``spill_location_latlon``  -  exactly one.
        spill_location_latlon: an explicit ``(lat, lon)`` spill point.
        species: the list of solute species  -  each a ``SpeciesSpec`` or a dict
            ``{name, release_rate_kg_s, sorption_kd?, decay_per_day?, parent?}``.
            REQUIRED, non-empty, with at least one positive release rate (never
            invented  -  a missing list is a typed USER_INPUT_REQUIRED failure).
        aquifer_k_ms / porosity: optional demo-aquifer overrides (narrated as demo
            defaults).
        duration_days: optional transport duration override (days). Demo default
            applied by the adapter when None.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``MultiSpeciesResult`` with ``plume_layers`` (one ``PlumeLayerURI`` per
        species) + derived args + a narration summary dict.

    Raises:
        MultiSpeciesInputError: missing/invalid spill point or species list.
        MultiSpeciesScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the species ---------------
    specs = normalize_species_list(species)

    # task-168: declare the planned internal-tool count: geocode (only when a place
    # string was supplied) + run_modflow_multi_species_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_spill_point(
        location, spill_location_latlon, pipeline_emitter=pipeline_emitter
    )

    # --- assemble the forcing contract (placeholder top-level contaminant /
    # rate carry the multi_species deck's required-but-unused scalars; the real
    # per-species forcing rides in ``species``  -  mirrors MAR's n/a placeholder).
    kwargs: dict[str, Any] = dict(
        spill_location_latlon=(lat, lon),
        contaminant=specs[0].name,
        release_rate_kg_s=max(
            (s.release_rate_kg_s for s in specs if s.release_rate_kg_s > 0.0),
            default=1.0,
        ),
        duration_days=float(duration_days) if duration_days is not None else 20.0,
        archetype="multi_species",
        species=specs,
    )
    if aquifer_k_ms is not None:
        kwargs["aquifer_k_ms"] = float(aquifer_k_ms)
    if porosity is not None:
        kwargs["porosity"] = float(porosity)
    try:
        run_args = MODFLOWRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise MultiSpeciesInputError(
            f"derived multi_species parameters failed validation: {exc}"
        ) from exc

    # --- run the N-species solver inside a substep + validate the typed result -
    from ..tools.run_modflow_multi_species_tool import run_modflow_multi_species_job

    async with substep(current_emitter(), "run_modflow_multi_species_job"):
        result = await _maybe_emit(
            pipeline_emitter,
            name=f"Model multi-species plumes [{len(specs)} species]",
            tool_name="run_modflow_multi_species_job",
            invoke=lambda: run_modflow_multi_species_job(
                run_args, compute_class=compute_class
            ),
        )
        if not isinstance(result, MultiSpeciesPlumeResult):
            ecode = "MULTI_SPECIES_RUN_FAILED"
            emsg = "multi_species run did not produce per-species plume layers"
            if isinstance(result, dict):
                ecode = result.get("error_code", ecode)
                emsg = result.get("error_message", emsg)
            raise MultiSpeciesScenarioError(f"{ecode}: {emsg}")

    plumes = result.plumes

    # Emit ONE per-species concentration summary chart from the typed scalars.
    await _emit_multi_species_chart(plumes)

    derived = {
        "location_name": location_name,
        "spill_location_latlon": [lat, lon],
        "duration_days": kwargs["duration_days"],
        "species": [s.model_dump() for s in specs],
    }
    summary = {
        "location_name": location_name,
        "n_species": len(plumes),
        "species": [
            {
                "name": _species_name_from_layer(p),
                "max_concentration_mgl": p.max_concentration_mgl,
                "plume_area_km2": p.plume_area_km2,
            }
            for p in plumes
        ],
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g} "
            "are demo defaults, not site-specific hydrogeology. Each species "
            "transports on the shared flow field; parent->daughter ingrowth "
            "coupling is recorded but not yet wired (independent transport)."
        ),
    }
    logger.info(
        "multi_species scenario complete location=%r n_plumes=%d",
        location_name,
        len(plumes),
    )
    return MultiSpeciesResult(
        plume_layers=plumes, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_multi_species_scenario",
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
async def run_model_multi_species_scenario(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    species: Any = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    duration_days: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model multiple co-released groundwater contaminants as N distinct plumes.

    Builds a MODFLOW 6 model with ONE shared groundwater-flow field driving N
    solute-transport models (one per species), runs it, and produces N plume
    layers  -  one ``PlumeLayerURI`` per species, each carrying that species' peak
    concentration + plume footprint. Each species has its own release rate,
    optional sorption (retardation) and first-order decay, and may name a parent
    species in a degradation chain (e.g. TCE -> cis-DCE -> VC). Use this when a
    single spill released SEVERAL contaminants whose plumes differ.

    Use this when:
        - The user describes a spill of MULTIPLE contaminants / solutes and wants
          each plume (multiple species, a solvent mixture, a degradation chain
          like TCE -> cis-DCE -> VC).

    Do NOT use this for:
        - A single-contaminant spill (use ``run_modflow_job`` or
          ``run_model_groundwater_contamination_scenario``).
        - Pumping drawdown / dewatering / recharge mounding (the other MODFLOW
          archetype tools).
        - Surface-water flooding (use ``run_model_flood_scenario``  -  SFINCS).

    Params:
        location: place name (geocoded to the spill point). Supply this OR
            ``spill_location_latlon``.
        spill_location_latlon: explicit ``(lat, lon)`` spill point.
        species: the list of contaminants, each ``{name, release_rate_kg_s,
            sorption_kd?, decay_per_day?, parent?}``. REQUIRED, non-empty, with at
            least one positive release rate  -  never invented; ask the user which
            contaminants were released + how much if absent.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        duration_days: optional transport duration (days). Demo default if None.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``MultiSpeciesResult`` JSON dict with ``plume_layers`` (one
        ``PlumeLayerURI`` per species  -  the agent narrates each species'
        ``max_concentration_mgl`` + ``plume_area_km2`` typed numbers), the
        ``derived_params``, and the ``summary``. On a recoverable failure (incl. a
        missing / empty / sourceless species list) the tool returns a typed error
        the agent narrates honestly  -  it never fabricates a contaminant.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    point = _coerce_optional_latlon(spill_location_latlon)
    try:
        result = await model_multi_species_scenario(
            location=location,
            spill_location_latlon=point,
            species=species,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            duration_days=(
                float(duration_days) if duration_days is not None else None
            ),
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except MultiSpeciesInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except MultiSpeciesScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "MULTI_SPECIES_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
