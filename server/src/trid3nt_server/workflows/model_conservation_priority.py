"""``model_conservation_priority``  --  conservation micro-North-Star composer.

A thin, deterministic (Invariant 2: LLM-free) higher-order workflow that builds
the SC-DNR-style conservation-priority stack for a region: a high-resolution
aerial BASE, the live species signal, the vegetation index, and the
imperiled-species biodiversity-priority raster, all emitted as layers the user
sees together.

Chain (each step is best-effort and INDEPENDENT  --  one source failing never
aborts the others; the honesty floor below makes a zero-layer run NOT ok):

    0. AOI resolution  --  accept an explicit ``bbox`` OR geocode a
       ``location_query`` via ``geocode_location`` (Nominatim).
    1. NAIP aerial BASE      -> ``fetch_naip(bbox)``               (context base)
    2. compute_ndvi          -> ``compute_ndvi(bbox, window)``     (vegetation)
    3. fetch_mobi            -> ``fetch_mobi(bbox, layer)``        (biodiversity)
    4. species occurrences   -> ``fetch_gbif_occurrences(bbox, sp)`` per species
    5. threatened ranges     -> ``fetch_iucn_red_list_range(bbox, sp)`` per name

The composer returns a ``ConservationPriorityResult`` carrying every produced
``LayerURI`` plus a deterministic ``status`` + ``summary`` built from typed
fields (no LLM). It does NOT publish  --  the agent surface / wrapper publishes
each returned ``LayerURI`` exactly as the flood-habitat composer does (so the
emitter's ``isinstance(result, LayerURI)`` gate auto-loads each layer when an
emitter is supplied).

Honesty floor (NATE render/honesty norm): a run that produced NO layers reports
``status="error"`` (never ``"ok"``) with the per-step failure reasons; a run
that produced some-but-not-all layers reports ``status="partial"`` and names
which sources failed. We never claim a conservation stack we did not build.

Invariants:
- **1. Determinism boundary: preserves.** Every narrated field is typed
  (layer counts, per-step error codes); ``summary`` is a format-string, no LLM.
- **2. Deterministic workflows: preserves.** Straight-line Python over
  registered atomic tools; typed-exception handling per step.
- **8. Cancellation is first-class: preserves.** Every ``await`` (emitter calls
  + each ``_maybe_emit``) propagates ``asyncio.CancelledError`` untouched.
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (where + which species); the data endpoints / colormaps are internal.

LLM exposure: the thin ``run_model_conservation_priority`` wrapper
(``workflow_dispatch`` metadata) lands in the registry so the LLM sees one
invocable tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger(
    "trid3nt_server.workflows.model_conservation_priority"
)

__all__ = [
    "ConservationPriorityResult",
    "model_conservation_priority",
    "run_model_conservation_priority",
    "ConservationPriorityError",
    "ConservationPriorityInputError",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ConservationPriorityError(RuntimeError):
    """Base class for conservation-priority composer failures."""

    error_code = "CONSERVATION_PRIORITY_ERROR"
    retryable = True


class ConservationPriorityInputError(ConservationPriorityError):
    """Neither a usable bbox nor a geocodable location_query was supplied."""

    error_code = "CONSERVATION_PRIORITY_INPUT_ERROR"
    retryable = False


# --------------------------------------------------------------------------- #
# Result envelope (agent-local; typed fields only  --  Invariant 1)
# --------------------------------------------------------------------------- #


class ConservationPriorityResult(GraceModel):
    """Return type for ``model_conservation_priority``.

    Fields:
        bbox: the resolved AOI bbox (``[min_lon, min_lat, max_lon, max_lat]``).
        location_name: canonical place name when geocoded, else None.
        aerial_layer: NAIP RGB ``LayerURI`` (base), or None on failure.
        ndvi_layer: Sentinel-2 NDVI ``LayerURI``, or None on failure.
        biodiversity_layer: MoBI ``LayerURI``, or None on failure.
        species_layers: list of GBIF occurrence ``LayerURI`` (one per species).
        range_layers: list of IUCN range ``LayerURI`` (one per species name).
        status: ``"ok"`` (all attempted sources produced a layer),
            ``"partial"`` (some produced layers, some failed), or ``"error"``
            (ZERO layers produced  --  the honesty floor; never ``"ok"``).
        failures: ``{step_name: error_text}`` for every step that failed.
        summary: deterministic narration-ready one-liner (no LLM).
        schema_version: ``"v1"``.
    """

    bbox: list[float]
    location_name: str | None = None
    aerial_layer: LayerURI | None = None
    ndvi_layer: LayerURI | None = None
    biodiversity_layer: LayerURI | None = None
    species_layers: list[LayerURI] = Field(default_factory=list)
    range_layers: list[LayerURI] = Field(default_factory=list)
    status: str = "error"
    failures: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    schema_version: str = "v1"

    def all_layers(self) -> list[LayerURI]:
        """Every produced layer, in stack order (aerial base first)."""
        layers: list[LayerURI] = []
        if self.aerial_layer is not None:
            layers.append(self.aerial_layer)
        if self.ndvi_layer is not None:
            layers.append(self.ndvi_layer)
        if self.biodiversity_layer is not None:
            layers.append(self.biodiversity_layer)
        layers.extend(self.species_layers)
        layers.extend(self.range_layers)
        return layers


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable via TOOL_REGISTRY."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise ConservationPriorityError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:10]}...)"
        )
    return entry.fn


async def _maybe_emit(
    emitter: Any | None,
    *,
    name: str,
    tool_name: str,
    invoke: Any,
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if an emitter is given,
    else call directly (awaiting if the callable returned a coroutine). Mirrors
    the flood-habitat composer's helper so the emitter's
    ``isinstance(result, LayerURI)`` auto-load gate fires for each layer.
    """
    if emitter is not None:
        return await emitter.emit_tool_call(
            name=name, tool_name=tool_name, invoke=invoke
        )
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def _resolve_bbox(
    bbox: tuple[float, float, float, float] | None,
    location_query: str | None,
    emitter: Any | None,
) -> tuple[tuple[float, float, float, float], str | None]:
    """Resolve an AOI bbox from an explicit bbox or a geocoded location_query.

    Returns ``(bbox, location_name)``. Raises ``ConservationPriorityInputError``
    when neither is usable.
    """
    if bbox is not None:
        if len(bbox) != 4:
            raise ConservationPriorityInputError(
                f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
            )
        return (tuple(float(v) for v in bbox), None)  # type: ignore[return-value]

    if not location_query:
        raise ConservationPriorityInputError(
            "model_conservation_priority requires either a bbox or a "
            "location_query (got neither)."
        )

    geocode_fn = _registry_fn("geocode_location")
    geo = await _maybe_emit(
        emitter,
        name=f"Geocode: {location_query}",
        tool_name="geocode_location",
        invoke=lambda: geocode_fn(location_query),
    )
    bb = geo.get("bbox") if isinstance(geo, dict) else None
    if not bb or len(bb) != 4:
        raise ConservationPriorityInputError(
            f"geocode_location({location_query!r}) returned no usable bbox"
        )
    return (
        (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])),
        geo.get("name") if isinstance(geo, dict) else None,
    )


def _format_summary(result: ConservationPriorityResult) -> str:
    """Deterministic narration-ready one-liner from typed fields (no LLM)."""
    where = result.location_name or (
        f"bbox [{result.bbox[0]:.3f}, {result.bbox[1]:.3f}, "
        f"{result.bbox[2]:.3f}, {result.bbox[3]:.3f}]"
    )
    layers = result.all_layers()
    parts = [f"Conservation-priority stack for {where}: {len(layers)} layer(s)"]
    chips: list[str] = []
    if result.aerial_layer is not None:
        chips.append("NAIP aerial base")
    if result.ndvi_layer is not None:
        chips.append("NDVI vegetation")
    if result.biodiversity_layer is not None:
        chips.append("MoBI biodiversity importance")
    if result.species_layers:
        chips.append(f"{len(result.species_layers)} species-occurrence layer(s)")
    if result.range_layers:
        chips.append(f"{len(result.range_layers)} threatened-range layer(s)")
    if chips:
        parts.append(" (" + ", ".join(chips) + ")")
    if result.failures:
        parts.append(
            "; unavailable: "
            + ", ".join(f"{k} [{v[:60]}]" for k, v in result.failures.items())
        )
    parts.append(f". status={result.status}.")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_conservation_priority(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    species_keys: list[int | str] | None = None,
    species_names: list[str] | None = None,
    mobi_layer: str = "species_richness",
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    pipeline_emitter: Any | None = None,
) -> ConservationPriorityResult:
    """Compose the SC-DNR-style conservation-priority stack for a region.

    Deterministic (Invariant 2) fan-out over registered atomic tools. Each
    source is best-effort and independent; the honesty floor makes a zero-layer
    run report ``status="error"`` (never ``"ok"``).

    Args:
        bbox: explicit AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326,
            OR omit and pass ``location_query``.
        location_query: free-text place name, geocoded to a bbox when ``bbox``
            is not given.
        species_keys: GBIF ``taxonKey`` ints or scientific-name strs for the
            occurrence-point layers (one layer per entry).
        species_names: scientific names for the IUCN threatened-range layers
            (one layer per entry).
        mobi_layer: which MoBI product (default ``"species_richness"``).
        start_date / end_date: NDVI window (``"YYYY-MM-DD"``); default trailing
            window.
        pipeline_emitter: optional ``PipelineEmitter``; when provided each step
            is wrapped in ``emit_tool_call`` (one progress card per source) and
            each ``LayerURI`` auto-loads via the emitter's built-in gate.

    Returns:
        ``ConservationPriorityResult``.
    """
    resolved_bbox, location_name = await _resolve_bbox(
        bbox, location_query, pipeline_emitter
    )

    failures: dict[str, str] = {}

    # --- 1. NAIP aerial base (context) ---
    aerial_layer: LayerURI | None = None
    try:
        naip_fn = _registry_fn("fetch_naip")
        aerial_layer = await _maybe_emit(
            pipeline_emitter,
            name="NAIP aerial imagery",
            tool_name="fetch_naip",
            invoke=lambda: naip_fn(bbox=resolved_bbox),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001  --  best-effort per source
        failures["fetch_naip"] = f"{type(exc).__name__}: {exc}"
        logger.info("conservation: NAIP failed: %s", exc)

    # --- 2. NDVI vegetation ---
    ndvi_layer: LayerURI | None = None
    try:
        ndvi_fn = _registry_fn("compute_ndvi")
        ndvi_layer = await _maybe_emit(
            pipeline_emitter,
            name="Sentinel-2 NDVI vegetation",
            tool_name="compute_ndvi",
            invoke=lambda: ndvi_fn(
                bbox=resolved_bbox, start_date=start_date, end_date=end_date
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        failures["compute_ndvi"] = f"{type(exc).__name__}: {exc}"
        logger.info("conservation: NDVI failed: %s", exc)

    # --- 3. MoBI biodiversity importance ---
    biodiversity_layer: LayerURI | None = None
    try:
        mobi_fn = _registry_fn("fetch_mobi")
        biodiversity_layer = await _maybe_emit(
            pipeline_emitter,
            name="MoBI biodiversity importance",
            tool_name="fetch_mobi",
            invoke=lambda: mobi_fn(bbox=resolved_bbox, layer=mobi_layer),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        failures["fetch_mobi"] = f"{type(exc).__name__}: {exc}"
        logger.info("conservation: MoBI failed: %s", exc)

    # --- 4. Species occurrences (one layer per species_key) ---
    species_layers: list[LayerURI] = []
    if species_keys:
        gbif_fn = _registry_fn("fetch_gbif_occurrences")
        for sp in species_keys:
            try:
                layer = await _maybe_emit(
                    pipeline_emitter,
                    name=f"Species occurrences: {sp}",
                    tool_name="fetch_gbif_occurrences",
                    invoke=lambda sp=sp: gbif_fn(
                        bbox=resolved_bbox, species_key=sp
                    ),
                )
                if layer is not None:
                    species_layers.append(layer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures[f"fetch_gbif_occurrences[{sp}]"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                logger.info("conservation: GBIF %s failed: %s", sp, exc)

    # --- 5. Threatened ranges (one layer per species name) ---
    range_layers: list[LayerURI] = []
    if species_names:
        iucn_fn = _registry_fn("fetch_iucn_red_list_range")
        for nm in species_names:
            try:
                # NOTE: fetch_iucn_red_list_range is a per-species API lookup
                # keyed on species_name (it returns a single-feature range
                # payload, not a bbox query)  --  no bbox parameter.
                layer = await _maybe_emit(
                    pipeline_emitter,
                    name=f"Threatened range: {nm}",
                    tool_name="fetch_iucn_red_list_range",
                    invoke=lambda nm=nm: iucn_fn(species_name=nm),
                )
                if layer is not None:
                    range_layers.append(layer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures[f"fetch_iucn_red_list_range[{nm}]"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                logger.info("conservation: IUCN %s failed: %s", nm, exc)

    result = ConservationPriorityResult(
        bbox=list(resolved_bbox),
        location_name=location_name,
        aerial_layer=aerial_layer,
        ndvi_layer=ndvi_layer,
        biodiversity_layer=biodiversity_layer,
        species_layers=species_layers,
        range_layers=range_layers,
        failures=failures,
    )

    # Honesty floor: zero layers => error (never ok). Some-but-not-all => partial.
    n_layers = len(result.all_layers())
    if n_layers == 0:
        result.status = "error"
    elif failures:
        result.status = "partial"
    else:
        result.status = "ok"
    result.summary = _format_summary(result)
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_METADATA = AtomicToolMetadata(
    name="run_model_conservation_priority",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_METADATA)
async def run_model_conservation_priority(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    species_keys: list[int | str] | None = None,
    species_names: list[str] | None = None,
    mobi_layer: str = "species_richness",
    start_date: str | None = None,
    end_date: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Build the SC-DNR-style conservation-priority stack for a region.

    Deterministic composer (zero LLM calls inside) that fans out over a single
    AOI and emits the conservation stack as layers:

    1. ``fetch_naip(bbox)``  --  high-res aerial imagery BASE (US-only).
    2. ``compute_ndvi(bbox, window)``  --  Sentinel-2 NDVI vegetation index.
    3. ``fetch_mobi(bbox, layer)``  --  NatureServe MoBI imperiled-species
       biodiversity-importance raster (CONUS-only).
    4. ``fetch_gbif_occurrences(bbox, species)``  --  one occurrence-point layer
       per ``species_keys`` entry.
    5. ``fetch_iucn_red_list_range(bbox, name)``  --  one threatened-range layer
       per ``species_names`` entry.

    Each source is best-effort and INDEPENDENT  --  one failing never aborts the
    others. The honesty floor means a run that produced NO layers reports
    ``status="error"`` (never ``"ok"``); a some-but-not-all run reports
    ``"partial"`` with the failing sources named.

    When to use:
        - A conservation / habitat / biodiversity-priority request over a US
          region: "show me conservation priorities around the ACE Basin",
          "map biodiversity, vegetation, and panther sightings near Big Cypress".

    When NOT to use:
        - A single data layer (call the atomic tool directly: ``fetch_naip`` /
          ``compute_ndvi`` / ``fetch_mobi`` / ``fetch_gbif_occurrences``).
        - Flood + habitat exposure (use ``run_model_flood_habitat_scenario``).
        - Non-US AOIs for the NAIP / MoBI layers (those degrade with an honest
          typed error; NDVI + GBIF + IUCN still work globally).

    Params:
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326, OR omit
            and pass ``location_query``.
        location_query: free-text place name (geocoded when no bbox).
        species_keys: GBIF taxonKey ints / scientific-name strs for occurrence
            layers (one layer each).
        species_names: scientific names for IUCN threatened-range layers (one
            layer each).
        mobi_layer: MoBI product (default ``"species_richness"``).
        start_date / end_date: NDVI window (``"YYYY-MM-DD"``).

    Returns:
        ``ConservationPriorityResult`` as a dict (``model_dump(mode="json")``):
        ``bbox``, ``location_name``, ``aerial_layer``, ``ndvi_layer``,
        ``biodiversity_layer``, ``species_layers``, ``range_layers``,
        ``status``, ``failures``, ``summary``, ``schema_version``.

    FR-DC-6: declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  --  same shape as the other composers;
    the underlying atomic tools still benefit from per-tool caching.
    """
    result = await model_conservation_priority(
        bbox=bbox,
        location_query=location_query,
        species_keys=species_keys,
        species_names=species_names,
        mobi_layer=mobi_layer,
        start_date=start_date,
        end_date=end_date,
    )
    return result.model_dump(mode="json")
