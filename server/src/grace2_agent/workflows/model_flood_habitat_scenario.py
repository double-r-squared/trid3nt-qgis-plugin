"""model_flood_habitat_scenario — Case 1 composer workflow (job-0118).

This is a **higher-order workflow** that composes existing atomic tools and
the M5 ``model_flood_scenario`` modeling workflow end-to-end into a single
Case 1 demo workflow:

    1. fetch_wdpa_protected_areas(bbox, designation_filter?)
         → wdpa LayerURI (FlatGeobuf protected-area polygons)
    2. for each species_key:
         fetch_gbif_occurrences(species_key, bbox)
         → per-species LayerURI (FlatGeobuf point occurrences)
    3. model_flood_scenario(bbox, rainfall_event)
         → AssessmentEnvelope with flood-depth LayerURI
    4. compute_zonal_statistics(flood_layer, wdpa_layer, statistics=...)
         → impact_metrics dict (per-WDPA-polygon flood-depth aggregates)
    5. if place_clip_polygon_uri is provided:
         clip_raster_to_polygon(flood_layer, place_clip_polygon_uri)
         clip_vector_to_polygon(wdpa_layer, place_clip_polygon_uri)
         clip_vector_to_polygon(each species_layer, place_clip_polygon_uri)
    6. Build human-readable ``case_summary_text`` from the metrics + counts.
    7. Return ``CaseOneResult`` carrying every layer URI + impact metrics +
       summary text.

Invariants:
- **1. Determinism boundary: preserves.** ``CaseOneResult`` fields are typed;
  ``case_summary_text`` is a deterministic format-string output (not LLM-generated).
- **2. Deterministic workflows: preserves.** No LLM in the loop; pure Python
  composition over the existing atomic tools + ``model_flood_scenario``.
- **3. Engine registration: preserves.** This composer reuses
  ``model_flood_scenario`` (already engine-registered); no agent-core changes.
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (bbox + species + rainfall event + optional protected-area designation
  filter + optional place-clip polygon); every other input (DEM, landcover,
  river geometry, precip depth, Manning's, WDPA endpoint URL) is fetched or
  defaulted inside the composer chain.

Pipeline-emitter integration (TENTATIVE per kickoff Open Question):
- The composer accepts an optional ``pipeline_emitter`` keyword argument; when
  provided, each major step is wrapped in ``emit_tool_call`` so the client
  renders one progress card per step inline in chat. When omitted, the
  composer runs silently (no emission) so direct-call unit/smoke harnesses do
  not need a mock emitter.
- LayerURI returns flow through ``add_loaded_layer`` automatically via
  ``emit_tool_call``'s built-in ``isinstance(result, LayerURI)`` gate.

LLM exposure (workflow-as-atomic-tool-wrapper pattern, matching ``model_flood_scenario``):

    @register_tool(AtomicToolMetadata(
        name="run_model_flood_habitat_scenario",
        ttl_class="live-no-cache",
        source_class="workflow_dispatch",
        cacheable=False,
    ))
    async def run_model_flood_habitat_scenario(...) -> dict: ...

The wrapper forwards verbatim to the composer body and returns the
``CaseOneResult.model_dump(mode="json")`` (a dict — the LLM tool surface does
not need the pydantic instance).
"""

from __future__ import annotations

import logging
from typing import Any

from grace2_contracts.case_results import CaseOneResult
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..tools import register_tool
from ..tools.clip_raster_to_polygon import clip_raster_to_polygon
from ..tools.clip_vector_to_polygon import clip_vector_to_polygon
from ..tools.compute_zonal_statistics import compute_zonal_statistics
from ..tools.fetch_gbif_occurrences import fetch_gbif_occurrences
from ..tools.fetch_wdpa_protected_areas import fetch_wdpa_protected_areas
from .model_flood_scenario import model_flood_scenario

__all__ = [
    "model_flood_habitat_scenario",
    "run_model_flood_habitat_scenario",
    "CaseOneResult",
]

logger = logging.getLogger("grace2_agent.workflows.model_flood_habitat_scenario")


# --------------------------------------------------------------------------- #
# Pipeline-emitter helper
# --------------------------------------------------------------------------- #


async def _maybe_emit(
    emitter: Any | None,
    *,
    name: str,
    tool_name: str,
    invoke: Any,
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if emitter is given,
    otherwise call directly. Synchronous callables are awaited transparently
    by ``emit_tool_call``; we mirror that here for the no-emitter path.
    """
    if emitter is not None:
        return await emitter.emit_tool_call(
            name=name,
            tool_name=tool_name,
            invoke=invoke,
        )
    result = invoke()
    # Honor sync-vs-async — sibling tools mix both.
    import asyncio as _asyncio

    if _asyncio.iscoroutine(result):
        result = await result
    return result


# --------------------------------------------------------------------------- #
# Summary text builder (deterministic; never LLM-generated)
# --------------------------------------------------------------------------- #


def _format_case_summary(
    *,
    bbox: tuple[float, float, float, float],
    species_counts: dict[str, int],
    impact_metrics: dict[str, Any],
    wdpa_polygon_count: int | None,
    flood_failed: bool,
    flood_error_code: str | None,
    rainfall_event: str,
    place_label: str | None,
) -> str:
    """Build the narration-ready summary string from the composed result.

    Output is a single human-readable sentence the agent surface can cite
    verbatim. Format-string only — no LLM in the chain (invariant 1).
    """
    parts: list[str] = []
    location_phrase = f"Within {place_label}" if place_label else "In the case bbox"
    parts.append(f"{location_phrase}")

    if species_counts:
        total = sum(species_counts.values())
        per = ", ".join(
            f"{n} {key}" for key, n in species_counts.items()
        )
        parts.append(f": {total} species occurrence(s) ({per})")
    else:
        parts.append(": no species occurrences requested")

    if wdpa_polygon_count is not None:
        parts.append(f"; {wdpa_polygon_count} protected-area polygon(s)")

    if flood_failed:
        parts.append(
            f"; flood modeling for the {rainfall_event} event did not complete"
            f" (error: {flood_error_code or 'UNKNOWN'})"
        )
    else:
        aggregate = impact_metrics.get("aggregate") if isinstance(impact_metrics, dict) else None
        if isinstance(aggregate, dict) and aggregate:
            max_d = aggregate.get("max")
            mean_d = aggregate.get("mean")
            count_d = aggregate.get("count")
            piece: list[str] = []
            if max_d is not None:
                piece.append(f"max flood depth {float(max_d):.2f} m")
            if mean_d is not None:
                piece.append(f"mean {float(mean_d):.2f} m")
            if count_d is not None:
                piece.append(f"{int(count_d)} flooded cells in protected areas")
            if piece:
                parts.append(f"; {', '.join(piece)} ({rainfall_event} event)")
        else:
            parts.append(
                f"; flood layer produced but no protected-area overlap"
                f" ({rainfall_event} event)"
            )

    parts.append(
        f"; bbox=[{bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}]."
    )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Helpers — species-key normalization for cache + summary keying
# --------------------------------------------------------------------------- #


def _species_key_label(key: int | str) -> str:
    """Stable string label for a species key (used as dict key in summary)."""
    return str(key)


def _layer_from_envelope(envelope: Any) -> LayerURI | None:
    """Extract the primary LayerURI from a ``model_flood_scenario`` envelope.

    The envelope's ``layers`` is a list of ``ResultLayer`` (envelope-side
    shape, not ``LayerURI``); we re-wrap the primary as a ``LayerURI`` for
    downstream use. Returns ``None`` if the envelope is a failed envelope
    (empty layers).
    """
    layers = getattr(envelope, "layers", None) or []
    if not layers:
        return None
    primary = layers[0]
    return LayerURI(
        layer_id=getattr(primary, "layer_id", ""),
        name=getattr(primary, "name", ""),
        layer_type=getattr(primary, "layer_type", "raster"),
        uri=getattr(primary, "uri", ""),
        style_preset=getattr(primary, "style_preset", "continuous_flood_depth"),
        temporal=getattr(primary, "temporal", None),
        role=getattr(primary, "role", "primary"),
        units=getattr(primary, "units", None),
    )


def _detect_flood_failure(envelope: Any) -> tuple[bool, str | None]:
    """Inspect a ``model_flood_scenario`` envelope for partial-failure markers.

    Returns ``(failed, error_code)``. The model_flood_scenario partial-failure
    envelope encodes the error code into ``flood.metrics.solver_version`` as
    ``"failed:<CODE>"`` (per job-0042). We parse it back out so the composer
    can thread it into the summary.
    """
    flood = getattr(envelope, "flood", None)
    if flood is None:
        # No flood payload at all → treat as failed without code.
        return (True, None)
    metrics = getattr(flood, "metrics", None)
    if metrics is None:
        return (True, None)
    sv = getattr(metrics, "solver_version", "") or ""
    if isinstance(sv, str) and sv.startswith("failed:"):
        return (True, sv.split(":", 1)[1] or None)
    # If layers list is empty even with a non-failed solver_version, still mark
    # as failed for safety.
    if not getattr(envelope, "layers", None):
        return (True, None)
    return (False, None)


# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_flood_habitat_scenario(
    bbox: tuple[float, float, float, float],
    species_keys: list[int | str] | None = None,
    rainfall_event: str = "atlas14_100yr",
    protected_area_designation: list[str] | None = None,
    place_clip_polygon_uri: str | None = None,
    place_label: str | None = None,
    *,
    pipeline_emitter: Any | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> CaseOneResult:
    """Compose Case 1 (flood + habitat exposure) end-to-end.

    Higher-order Case 1 composer. Sequences:

        1. ``fetch_wdpa_protected_areas(bbox, protected_area_designation)``
        2. For each ``species_key`` in ``species_keys``:
               ``fetch_gbif_occurrences(species_key, bbox)``
        3. ``model_flood_scenario(bbox, rainfall_event)``
        4. ``compute_zonal_statistics(flood_layer, wdpa_layer, ["max", "mean", "count"])``
        5. If ``place_clip_polygon_uri`` is supplied, clip the flood raster,
           the WDPA layer, and each species layer to the polygon.
        6. Build ``case_summary_text`` from the metrics + counts.

    Each underlying call is wrapped in ``pipeline_emitter.emit_tool_call`` when
    an emitter is provided, so the client renders one progress card per
    step. LayerURI returns flow into ``loaded_layers`` automatically via the
    emitter's built-in gate.

    Args:
        bbox: case-area bbox in EPSG:4326 ``(min_lon, min_lat, max_lon, max_lat)``.
        species_keys: zero-or-more GBIF taxonKeys (int) or scientific names
            (str). Each gets a separate per-species ``LayerURI`` (per-species
            discipline). Empty list / ``None`` → no species layers; the
            workflow still produces flood + WDPA + impact summary.
        rainfall_event: design-storm identifier. Currently only the
            ``"atlas14_<N>yr"`` family is supported by the underlying
            ``model_flood_scenario``; we parse the ``N`` into the
            ``return_period_yr`` parameter. Default ``"atlas14_100yr"``.
        protected_area_designation: optional WDPA ``DESIG_ENG`` filter (e.g.
            ``["National Park", "National Preserve"]``). Passed through to
            ``fetch_wdpa_protected_areas``.
        place_clip_polygon_uri: optional polygon URI (``gs://`` or local
            path). When the user named a region in their prompt (e.g. "in Big
            Cypress National Preserve"), the agent passes the polygon URI
            here to clip the flood raster + WDPA + species layers to that
            exact polygon. ``None`` → no clipping pass.
        place_label: optional human-readable place name used in the summary
            string (e.g. ``"Big Cypress National Preserve"``). When ``None``,
            the summary references "the case bbox" instead.
        pipeline_emitter: optional ``PipelineEmitter``-compatible object. When
            provided, each step is wrapped in ``emit_tool_call`` for web-side
            progress emission. When ``None``, the workflow runs silently.
        project_id / session_id: ULID identifiers threaded into the
            underlying ``model_flood_scenario`` invocation.

    Returns:
        ``CaseOneResult`` carrying:
            - ``flood_layer_uri``: the published flood-depth ``LayerURI``
              (None when modeling failed).
            - ``species_layers``: one ``LayerURI`` per ``species_keys[i]``.
            - ``wdpa_layer_uri``: the WDPA polygon ``LayerURI``.
            - ``impact_metrics``: ``compute_zonal_statistics`` output dict
              (``by_zone`` + ``aggregate``).
            - ``case_summary_text``: deterministic narration string.
            - ``species_counts``: per-species occurrence count (label → int).
    """
    species_list: list[int | str] = list(species_keys or [])
    rainfall_event = rainfall_event or "atlas14_100yr"
    return_period_yr = _parse_return_period(rainfall_event)

    logger.info(
        "model_flood_habitat_scenario start bbox=%s species_keys=%s "
        "rainfall_event=%s designation=%s place_clip=%s",
        bbox,
        species_list,
        rainfall_event,
        protected_area_designation,
        place_clip_polygon_uri,
    )

    # --- Step 1: WDPA fetch -------------------------------------------------
    wdpa_layer: LayerURI | None = None
    wdpa_polygon_count: int | None = None
    try:
        wdpa_layer = await _maybe_emit(
            pipeline_emitter,
            name="Fetch WDPA protected areas",
            tool_name="fetch_wdpa_protected_areas",
            invoke=lambda: fetch_wdpa_protected_areas(
                bbox=bbox,
                designation_filter=protected_area_designation,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — composer continues without WDPA
        logger.warning(
            "model_flood_habitat_scenario: fetch_wdpa_protected_areas failed: %s",
            exc,
        )

    # --- Step 2: per-species GBIF fetches -----------------------------------
    species_layers: list[LayerURI] = []
    species_counts: dict[str, int] = {}
    for sk in species_list:
        label = _species_key_label(sk)
        try:
            layer = await _maybe_emit(
                pipeline_emitter,
                name=f"Fetch GBIF occurrences ({label})",
                tool_name="fetch_gbif_occurrences",
                invoke=lambda sk=sk: fetch_gbif_occurrences(
                    species_key=sk,
                    bbox=bbox,
                ),
            )
            species_layers.append(layer)
            species_counts[label] = _count_features_safely(layer)
        except Exception as exc:  # noqa: BLE001 — continue with the others
            logger.warning(
                "model_flood_habitat_scenario: fetch_gbif_occurrences(%r) failed: %s",
                sk,
                exc,
            )
            species_counts[label] = 0

    # --- Step 3: model_flood_scenario ---------------------------------------
    flood_envelope = None
    flood_layer: LayerURI | None = None
    flood_failed = True
    flood_error_code: str | None = None
    try:
        flood_envelope = await _maybe_emit(
            pipeline_emitter,
            name=f"Model flood scenario ({rainfall_event})",
            tool_name="model_flood_scenario",
            invoke=lambda: model_flood_scenario(
                bbox=bbox,
                return_period_yr=return_period_yr,
                project_id=project_id,
                session_id=session_id,
            ),
        )
        flood_failed, flood_error_code = _detect_flood_failure(flood_envelope)
        flood_layer = _layer_from_envelope(flood_envelope)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "model_flood_habitat_scenario: model_flood_scenario raised: %s",
            exc,
        )
        flood_failed = True
        flood_error_code = exc.__class__.__name__

    # --- Step 4: compute_zonal_statistics over flood × WDPA -----------------
    impact_metrics: dict[str, Any] = {}
    if flood_layer is not None and wdpa_layer is not None:
        try:
            impact_metrics = await _maybe_emit(
                pipeline_emitter,
                name="Compute zonal statistics (flood × WDPA)",
                tool_name="compute_zonal_statistics",
                invoke=lambda: compute_zonal_statistics(
                    value_raster_uri=flood_layer.uri,
                    zone_input_uri=wdpa_layer.uri,
                    statistics=["max", "mean", "count"],
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_flood_habitat_scenario: compute_zonal_statistics failed: %s",
                exc,
            )

    # --- Step 5: optional place-clip polygon pass ---------------------------
    if place_clip_polygon_uri is not None:
        if flood_layer is not None:
            try:
                clipped_flood = await _maybe_emit(
                    pipeline_emitter,
                    name="Clip flood raster to place polygon",
                    tool_name="clip_raster_to_polygon",
                    invoke=lambda: clip_raster_to_polygon(
                        raster_uri=flood_layer.uri,
                        polygon_uri=place_clip_polygon_uri,
                    ),
                )
                flood_layer = clipped_flood
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_flood_habitat_scenario: clip_raster_to_polygon (flood) failed: %s",
                    exc,
                )

        if wdpa_layer is not None:
            try:
                clipped_wdpa = await _maybe_emit(
                    pipeline_emitter,
                    name="Clip WDPA layer to place polygon",
                    tool_name="clip_vector_to_polygon",
                    invoke=lambda: clip_vector_to_polygon(
                        vector_uri=wdpa_layer.uri,
                        polygon_uri=place_clip_polygon_uri,
                    ),
                )
                wdpa_layer = clipped_wdpa
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_flood_habitat_scenario: clip_vector_to_polygon (wdpa) failed: %s",
                    exc,
                )

        clipped_species: list[LayerURI] = []
        for layer in species_layers:
            try:
                clipped = await _maybe_emit(
                    pipeline_emitter,
                    name=f"Clip species layer ({layer.name}) to place polygon",
                    tool_name="clip_vector_to_polygon",
                    invoke=lambda layer=layer: clip_vector_to_polygon(
                        vector_uri=layer.uri,
                        polygon_uri=place_clip_polygon_uri,
                    ),
                )
                clipped_species.append(clipped)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_flood_habitat_scenario: clip_vector_to_polygon (%s) failed: %s",
                    layer.name,
                    exc,
                )
                clipped_species.append(layer)
        species_layers = clipped_species

    # --- Step 6: build summary text + return CaseOneResult ------------------
    case_summary_text = _format_case_summary(
        bbox=bbox,
        species_counts=species_counts,
        impact_metrics=impact_metrics,
        wdpa_polygon_count=wdpa_polygon_count,
        flood_failed=flood_failed,
        flood_error_code=flood_error_code,
        rainfall_event=rainfall_event,
        place_label=place_label,
    )
    result = CaseOneResult(
        bbox=bbox,
        flood_layer_uri=flood_layer if not flood_failed else None,
        species_layers=species_layers,
        wdpa_layer_uri=wdpa_layer,
        impact_metrics=impact_metrics,
        case_summary_text=case_summary_text,
        species_counts=species_counts,
    )
    logger.info(
        "model_flood_habitat_scenario complete species_count=%d flood_failed=%s "
        "impact_keys=%s",
        len(species_layers),
        flood_failed,
        list(impact_metrics.keys()) if isinstance(impact_metrics, dict) else None,
    )
    return result


def _parse_return_period(rainfall_event: str) -> int:
    """Parse ``"atlas14_<N>yr"`` into ``N``. Default 100 on parse failure."""
    if not isinstance(rainfall_event, str):
        return 100
    prefix = "atlas14_"
    suffix = "yr"
    s = rainfall_event.strip().lower()
    if not s.startswith(prefix) or not s.endswith(suffix):
        return 100
    middle = s[len(prefix) : -len(suffix)]
    try:
        return int(middle)
    except ValueError:
        return 100


def _count_features_safely(layer: LayerURI) -> int:
    """Best-effort feature count for a FlatGeobuf LayerURI.

    Used to populate ``species_counts`` for the summary. Failures (no
    pyogrio, missing file, non-FGB URI) return 0 silently — the summary
    surfaces the count as 0 rather than failing the whole composer.
    """
    uri = getattr(layer, "uri", "") or ""
    if not uri:
        return 0
    try:
        import pyogrio  # type: ignore[import-not-found]
    except ImportError:
        return 0
    try:
        info = pyogrio.read_info(uri)
        return int(info.get("features", 0) or 0)
    except Exception:  # noqa: BLE001 — count is best-effort
        return 0


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_CASE_ONE_METADATA = AtomicToolMetadata(
    name="run_model_flood_habitat_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_CASE_ONE_METADATA)
async def run_model_flood_habitat_scenario(
    bbox: tuple[float, float, float, float],
    species_keys: list[int | str] | None = None,
    rainfall_event: str = "atlas14_100yr",
    protected_area_designation: list[str] | None = None,
    place_clip_polygon_uri: str | None = None,
    place_label: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Run the Case 1 (flood + habitat) composer end-to-end.

    Six-step composition chain over a single bounding box (all deterministic
    Python, zero LLM calls inside):
    1. ``fetch_wdpa_protected_areas(bbox, designation_filter)`` — WDPA polygon
       layer for the bbox.
    2. Per-species: ``fetch_gbif_occurrences(bbox, taxon_key)`` — one
       FlatGeobuf ``LayerURI`` per species in ``species_keys``.
    3. ``run_model_flood_scenario(bbox, return_period_yr)`` — SFINCS flood
       depth COG (9-step sub-chain; see that tool's docstring).
    4. ``compute_zonal_statistics(flood_depth_cog, wdpa_polygons)`` — flood
       impact metrics within each WDPA polygon.
    5. Optional: ``clip_raster_to_polygon(flood_cog, place_clip_polygon_uri)``
       — clips the flood layer to a named place polygon when provided.
    6. Optional: per-layer ``clip_vector_to_polygon(species_layer_uri,
       place_clip_polygon_uri)`` — clips each species layer to the same polygon.

    When to use:
        - Case 1 intent: combine flood modeling with species occurrence data
          and protected-area overlays over a single bbox (e.g. "Show me Florida
          panther occurrences in Big Cypress, plus a 100-year flood").
        - User wants a flood-depth layer, per-species occurrences, WDPA
          boundaries, impact summary, and narration text in one call.

    When NOT to use:
        - Flood-only scenario (use ``run_model_flood_scenario`` directly).
        - Species-only query (use ``fetch_gbif_occurrences`` directly).
        - Non-flood hazard composers (other milestones).
        - Custom multi-tool plans not covered by Case 1 (compose atomics
          manually).

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. The
            case-area bounding box; all fetches and the flood model run over
            this extent.
        species_keys: GBIF ``taxonKey`` int OR scientific name str list, one
            entry per species the agent wants overlaid. Each gets a separate
            ``LayerURI`` (per-species discipline). Empty / ``None`` → no
            species layers.
        rainfall_event: design-storm identifier. Currently the
            ``"atlas14_<N>yr"`` family (e.g. ``"atlas14_100yr"``,
            ``"atlas14_500yr"``); the ``<N>`` becomes ``return_period_yr`` in
            the underlying flood model.
        protected_area_designation: optional WDPA ``DESIG_ENG`` filter — e.g.
            ``["National Park", "National Preserve"]`` to restrict the WDPA
            overlay.
        place_clip_polygon_uri: optional polygon URI (``gs://`` or local) used
            to clip the flood + WDPA + species layers to a named region
            (e.g. an Apalachicola NF polygon when the user said
            "in Apalachicola National Forest").
        place_label: optional human-readable place name for the summary string.

    Returns:
        The ``CaseOneResult`` serialized as a dict (model_dump(mode="json")):
            - ``flood_layer_uri``: LayerURI dict (or None if flood modeling failed)
            - ``species_layers``: list of LayerURI dicts (one per species_key)
            - ``wdpa_layer_uri``: LayerURI dict (or None if no WDPA features)
            - ``impact_metrics``: zonal-stats dict (``by_zone`` + ``aggregate``)
            - ``case_summary_text``: narration-ready string
            - ``species_counts``: per-species count (label → int)
            - ``bbox``: tuple
            - ``schema_version``: ``"v1"``

    FR-DC-6: This wrapper declares ``cacheable=False`` +
    ``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` —
    same shape as ``run_model_flood_scenario`` (job-0042 / job-0060). The
    composer itself runs through cacheable atomic tools, so identical inputs
    still benefit from per-tool cache hits even though the composer itself
    is uncached.

    Cross-tool dependencies:
        Upstream (step chain):
        - ``fetch_wdpa_protected_areas`` → step 1
        - ``fetch_gbif_occurrences`` (per species) → step 2
        - ``run_model_flood_scenario`` → step 3 (itself a 9-step chain)
        - ``compute_zonal_statistics`` (flood × WDPA) → step 4
        - ``clip_raster_to_polygon`` (optional, flood + place polygon) → step 5
        - ``clip_vector_to_polygon`` (optional, per species + place polygon)
          → step 6
        Downstream (feeds):
        - Agent ``CaseOneResult`` narration — the returned ``impact_metrics``
          ``aggregate`` dict supplies headline numbers (Invariant 7).
        - ``publish_layer`` — each returned ``LayerURI`` in ``flood_layer_uri``
          / ``species_layers`` / ``wdpa_layer_uri`` can be published separately.
    """
    result = await model_flood_habitat_scenario(
        bbox=bbox,
        species_keys=species_keys,
        rainfall_event=rainfall_event,
        protected_area_designation=protected_area_designation,
        place_clip_polygon_uri=place_clip_polygon_uri,
        place_label=place_label,
    )
    return result.model_dump(mode="json")
