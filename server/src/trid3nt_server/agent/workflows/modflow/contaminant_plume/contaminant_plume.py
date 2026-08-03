"""``modflow_contaminant_plume`` - MODFLOW groundwater contaminant-plume template.

The ONE registered MODFLOW template that answers "model a contaminant plume in
groundwater" - single OR multi species. Its ``species`` knob (min 1) drives one
shared GWF flow field + N GWT solute-transport models (one per species). A
single contaminant is ``species`` of length 1 - the convenience fields
``contaminant`` / ``release_rate_kg_s`` are accepted and normalized into a
one-element species list (minimal parameter surface, Invariant 10).

Chain:

    resolve the spill point (geocode a place, or take an explicit lat/lon)
        -> normalize the contaminant(s) into a SpeciesSpec list (never invented -
           a missing/empty list with no single-contaminant convenience fields is
           a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="multi_species", species=[...])
        -> run_modflow_multi_species_job (ONE shared GWF + N GWT -> mf6 -> N .ucn)
        -> postprocess_multi_species -> N PlumeLayerURI (one per species)
        -> load EACH plume onto the map (length 1 for a single contaminant)
        -> return the uniform plumes[] envelope.

Envelope unification (contract section 5.3): the template ALWAYS returns a
``plumes[]`` list (length 1 for a single species), so downstream consumers accept
one uniform shape. Every narrated number is a typed ``PlumeLayerURI`` field
(``max_concentration_mgl`` / ``plume_area_km2``) - never free-generated
(Invariant 1). A run whose every species plume is at/below the detection floor
returns a typed empty-result error rather than reading as a successful layer set
(Invariant 9).

Tagged ``engine="modflow"``, ``tier="template"``: EXCLUDED from the default
retrieval pool, surfaced only by the ``run_modflow`` door's gate expansion
(select-then-call).
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
    MODFLOWRunArgs,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SpeciesSpec,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

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
    "trid3nt_server.agent.workflows.modflow.contaminant_plume.contaminant_plume"
)

__all__ = [
    "ContaminantPlumeResult",
    "model_contaminant_plume",
    "modflow_contaminant_plume",
    "ContaminantPlumeScenarioError",
    "ContaminantPlumeInputError",
    "normalize_species_list",
    "TEMPLATE_CARD",
]


#: Door listing card (engine-door refactor): the curated one-line question +
#: required inputs the ``run_modflow`` concierge surfaces for select-then-call.
TEMPLATE_CARD = TemplateCard(
    question=(
        "how far a contaminant spill spreads in an aquifer + peak concentration "
        "(single OR multi species)"
    ),
    required_inputs=["spill_location_latlon (or location)", "contaminant (or species)"],
    knobs=(
        "duration_days, aquifer_k_ms, porosity; "
        "species=[{name, release_rate_kg_s, sorption_kd, decay_per_day, parent}]"
    ),
)


# --------------------------------------------------------------------------- #
# Result envelope (agent-local; uniform plumes[] shape)
# --------------------------------------------------------------------------- #


class ContaminantPlumeResult(GraceModel):
    """Return type for ``model_contaminant_plume`` (engine-door FOLD).

    Bundles the N per-species plume layers (length 1 for a single contaminant) +
    the derived args + a narration summary dict. Invariant 1: every narrated
    number is a typed field - each ``plumes[i]`` carries ``max_concentration_mgl``
    + ``plume_area_km2``.

    Fields:
        plumes: ordered list of one ``PlumeLayerURI`` per species (same order as
            the input species list). At least one.
        derived_params: JSON-able derived-args dict (spill point, species specs).
        summary: narration dict ``{location_name, species: [{name, ...}], ...}``.
    """

    schema_version: str = "v1"

    plumes: list[PlumeLayerURI] = Field(min_length=1)
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class ContaminantPlumeScenarioError(RuntimeError):
    """Base class for ``model_contaminant_plume`` failures."""

    error_code: str = "CONTAMINANT_PLUME_SCENARIO_ERROR"
    retryable: bool = False


class ContaminantPlumeInputError(ContaminantPlumeScenarioError):
    """Caller supplied invalid / missing spill point or contaminant (honesty gate)."""

    error_code = "CONTAMINANT_PLUME_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Registry / coercion helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise ContaminantPlumeScenarioError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


def normalize_species_list(
    species: Any,
    *,
    contaminant: str | None = None,
    release_rate_kg_s: float | None = None,
) -> list[SpeciesSpec]:
    """Coerce a heterogeneous contaminant input into a validated ``SpeciesSpec`` list.

    Accepts EITHER a ``species`` list (of ``SpeciesSpec`` objects OR plain dicts,
    the wire form the LLM passes) OR the single-contaminant convenience pair
    (``contaminant`` + ``release_rate_kg_s``), which is normalized into a
    one-element species list. Validates each through the ``SpeciesSpec`` contract
    (non-empty name, release rate >= 0, optional sorption / decay / parent). The
    HONESTY floor lives here: no contaminant at all, an empty list, a malformed
    species, duplicate names, OR a list where NO species carries a positive
    release rate raises ``ContaminantPlumeInputError`` (we never invent a
    contaminant / source).

    Returns the validated, ordered ``SpeciesSpec`` list (at least one).

    Raises:
        ContaminantPlumeInputError: no source / empty / malformed / sourceless.
    """
    # Single-contaminant convenience -> a one-element species list (keeps the
    # minimal-parameter surface: a caller with one contaminant need not build a
    # list). An explicit species list wins when both are supplied.
    if not species:
        if contaminant:
            species = [
                {
                    "name": contaminant,
                    "release_rate_kg_s": (
                        float(release_rate_kg_s)
                        if release_rate_kg_s is not None
                        else 0.0
                    ),
                }
            ]
        else:
            raise ContaminantPlumeInputError(
                "a contaminant plume requires either a single contaminant "
                "(contaminant + release_rate_kg_s) OR a non-empty species list "
                "(each with a name and a release rate). The contaminant(s) are a "
                "user input and are never invented; ask the user what was released."
            )

    if not isinstance(species, (list, tuple)) or len(species) == 0:
        raise ContaminantPlumeInputError(
            "the species input must be a non-empty list; "
            f"got {type(species).__name__} with no entries."
        )
    specs: list[SpeciesSpec] = []
    for raw in species:
        if isinstance(raw, SpeciesSpec):
            specs.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ContaminantPlumeInputError(
                f"each species must be a SpeciesSpec or a dict; got "
                f"{type(raw).__name__}."
            )
        try:
            specs.append(SpeciesSpec(**raw))
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            raise ContaminantPlumeInputError(
                f"invalid species spec {raw!r}: {exc}"
            ) from exc
    # Names must be unique (the adapter keys GWT models on the species name).
    names = [s.name for s in specs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ContaminantPlumeInputError(
            f"species names must be unique; duplicated: {sorted(dupes)}."
        )
    # At least one species must carry a real source (a pure daughter-only list
    # would model nothing - the honesty floor: never a sourceless 'modeled' run).
    if not any(float(s.release_rate_kg_s) > 0.0 for s in specs):
        raise ContaminantPlumeInputError(
            "at least one species must have a positive release_rate_kg_s; a list "
            "of pure daughter products (all release rates 0) has no source to "
            "model. Ask the user for the released (parent) contaminant + amount."
        )
    return specs


def _coerce_optional_latlon(value: Any) -> tuple[float, float] | None:
    """Coerce an optional lat/lon arg (str / list / tuple) -> (lat, lon) or None."""
    if value is None:
        return None
    from trid3nt_server.agent.tool_arg_normalizer import coerce_latlon

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
        raise ContaminantPlumeInputError(
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
        raise ContaminantPlumeScenarioError(
            f"geocode_location({location!r}) returned no centroid lat/lon."
        )
    return float(glat), float(glon), str(location)


def _species_name_from_layer(plume: PlumeLayerURI) -> str:
    """Recover the species label from a ``PlumeLayerURI`` name for the chart axis.

    The postprocess names each layer ``Contaminant Plume - <species> (peak ...)``;
    extract ``<species>`` for a compact chart axis. Falls back to the layer id.
    """
    name = getattr(plume, "name", "") or ""
    if " - " in name:
        tail = name.split(" - ", 1)[1]
        return tail.split(" (", 1)[0].strip() or name
    return name or getattr(plume, "layer_id", "species")


async def _emit_plume_chart(plumes: list[PlumeLayerURI]) -> None:
    """Side-emit a per-species plume summary chart (best-effort, no-op safe).

    A grouped bar over the species, each with its real typed
    ``max_concentration_mgl`` + ``plume_area_km2`` (Invariant 1: never fabricated).
    Emits nothing when every plume is empty (the honesty floor).
    """
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

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
        "title": "Contaminant plume - per-species summary",
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
        title="Contaminant plume summary",
        caption=caption or "groundwater contaminant plume(s)",
        source_layer_uri=getattr(plumes[0], "uri", None) if plumes else None,
    )
    await emit_chart_payloads(chart)


# --------------------------------------------------------------------------- #
# The composer (single AND multi species; always plumes[])
# --------------------------------------------------------------------------- #


async def model_contaminant_plume(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | None = None,
    *,
    contaminant: str | None = None,
    release_rate_kg_s: float | None = None,
    species: Any = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    duration_days: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> ContaminantPlumeResult:
    """Compose spill point + contaminant(s) -> MODFLOW GWT -> plumes[] (>=1).

    Single OR multi species. A single contaminant is ``species`` of length 1 (the
    ``contaminant`` / ``release_rate_kg_s`` convenience fields normalize into it).

    Raises:
        ContaminantPlumeInputError: missing/invalid spill point or contaminant.
        ContaminantPlumeScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the contaminant ----------
    specs = normalize_species_list(
        species, contaminant=contaminant, release_rate_kg_s=release_rate_kg_s
    )

    # declare the planned internal-tool count: geocode (only when a
    # place string was supplied) + run_modflow_multi_species_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_spill_point(
        location, spill_location_latlon, pipeline_emitter=pipeline_emitter
    )

    # Assemble the forcing contract. The top-level contaminant / rate carry the
    # multi_species deck's required-but-unused scalars; the real per-species
    # forcing rides in ``species`` (mirrors the archetype placeholder pattern).
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
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        raise ContaminantPlumeInputError(
            f"derived contaminant-plume parameters failed validation: {exc}"
        ) from exc

    # --- run the (N>=1)-species solver inside a substep + validate the result --
    from trid3nt_server.agent.tools.simulation.modflow.run_modflow_multi_species_tool import (
        run_modflow_multi_species_job,
    )

    async with substep(current_emitter(), "run_modflow_multi_species_job"):
        result = await _maybe_emit(
            pipeline_emitter,
            name=f"Model contaminant plume(s) [{len(specs)} species]",
            tool_name="run_modflow_multi_species_job",
            invoke=lambda: run_modflow_multi_species_job(
                run_args, compute_class=compute_class
            ),
        )
        if not isinstance(result, MultiSpeciesPlumeResult):
            ecode = "CONTAMINANT_PLUME_RUN_FAILED"
            emsg = "contaminant-plume run did not produce plume layers"
            if isinstance(result, dict):
                ecode = result.get("error_code", ecode)
                emsg = result.get("error_message", emsg)
            raise ContaminantPlumeScenarioError(f"{ecode}: {emsg}")

    plumes = list(result.plumes)

    # Load EACH plume onto the map (length 1 for a single contaminant). Mirrors
    # emit_tool_call's single-LayerURI gate applied per plume - the run tool
    # returns the typed MultiSpeciesPlumeResult (a dict on the wire), which the
    # dispatch does NOT auto-load, so the template loads them itself. Best-effort:
    # a None emitter (direct-call / CI) or a dropped un-renderable layer no-ops.
    emitter = current_emitter()
    if emitter is not None:
        for p in plumes:
            emit_layer = emit_layer_uri(p)
            if emit_layer is not None:
                try:
                    await emitter.add_loaded_layer(emit_layer)
                except Exception as exc:  # noqa: BLE001 - one bad layer never sinks the run
                    logger.debug("could not add plume layer: %s", exc)

    # Emit ONE per-species concentration summary chart from the typed scalars.
    await _emit_plume_chart(plumes)

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
        "contaminant_plume scenario complete location=%r n_plumes=%d",
        location_name,
        len(plumes),
    )
    return ContaminantPlumeResult(
        plumes=plumes, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed registered TEMPLATE (engine=modflow, tier=template)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="modflow_contaminant_plume",
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
async def modflow_contaminant_plume(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    contaminant: str | None = None,
    release_rate_kg_s: float | None = None,
    species: Any = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    duration_days: float | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a groundwater contaminant plume (spill spread + peak concentration), single OR multi species.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity default to narrated demo values unless supplied), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Builds a MODFLOW 6 model with ONE shared groundwater-flow field driving one
    solute-transport model per species, runs it, and produces one plume layer per
    species - each ``PlumeLayerURI`` carrying that species' peak concentration +
    plume footprint. A single contaminant is the length-1 case (pass
    ``contaminant`` + ``release_rate_kg_s``); several co-released contaminants
    pass ``species=[...]`` (a solvent mixture, a degradation chain like
    TCE -> cis-DCE -> VC). Always returns a uniform ``plumes[]`` list.

    Use this when:
        - The user wants to model a groundwater contamination spill / contaminant
          plume / how far a chemical spill spreads in an aquifer + peak concentration.
        - A spill released ONE contaminant (contaminant + release_rate_kg_s) OR
          SEVERAL (species=[...]).

    Do NOT use this for:
        - Surface-water / inundation flooding (use ``sfincs_flood``).
        - Contaminant entering groundwater ALONG a river (modflow_river_seepage).
        - Pumping drawdown / capture zone / dewatering (the other modflow_* templates).

    Params:
        location: place name (geocoded to the spill point). Supply this OR
            ``spill_location_latlon``.
        spill_location_latlon: explicit ``(lat, lon)`` spill point.
        contaminant: single-contaminant name (e.g. "benzene", "TCE"); modeled as a
            conservative tracer unless sorption/decay is given via ``species``.
        release_rate_kg_s: single-contaminant mass-release rate, kg/s (>0).
        species: OPTIONAL list of contaminants, each ``{name, release_rate_kg_s,
            sorption_kd?, decay_per_day?, parent?}`` - for a multi-contaminant
            spill. At least one species must carry a positive release rate; never
            invented (ask the user what was released if absent).
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        duration_days: optional transport duration (days). Demo default if None.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a JSON dict with ``plumes`` (a list of one ``PlumeLayerURI``
        per species - length 1 for a single contaminant; the agent narrates each
        species' ``max_concentration_mgl`` + ``plume_area_km2`` typed numbers),
        the ``derived_params``, and the ``summary``. On a recoverable failure
        (incl. a missing / empty / sourceless contaminant) the tool returns a
        typed error the agent narrates honestly - it never fabricates a contaminant.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` - the cache shim is NOT invoked.
    """
    point = _coerce_optional_latlon(spill_location_latlon)
    try:
        result = await model_contaminant_plume(
            location=location,
            spill_location_latlon=point,
            contaminant=contaminant,
            release_rate_kg_s=(
                float(release_rate_kg_s) if release_rate_kg_s is not None else None
            ),
            species=species,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            duration_days=(
                float(duration_days) if duration_days is not None else None
            ),
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except ContaminantPlumeInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except ContaminantPlumeScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "CONTAMINANT_PLUME_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
