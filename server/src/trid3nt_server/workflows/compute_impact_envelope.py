"""``compute_impact_envelope`` workflow composer — Wave 4.11 P3.

This module chains the Wave 4.11 P2 ``postprocess_pelicun`` atomic tool with
the existing structure-inventory fetch + Pelicun damage assessment chain into
a single LLM-visible composer. The result is a single tool call that the
agent can invoke to go from "flood layer URI" → portfolio-level
``ImpactEnvelope`` (SRS Appendix B.6c, Decision N) — every numeric the agent
might cite ("X structures impacted, $Y damages, Z population displaced")
read off a typed envelope, never invented (Invariant 1).

**Pattern (mirrors ``model_flood_scenario``):**

    geocode_location (if location_query, no bbox)
      → fetch_usace_nsi(bbox)  OR  compute_building_density(bbox)
      → run_pelicun_damage_assessment(hazard_raster_uri, assets_uri)
         (or run_pelicun_with_buildings for MS_BUILDINGS path)
      → postprocess_pelicun(damage_layer_uri, flood_layer_uri)
      → ImpactEnvelope dict + narrative string + provenance

**Why a composer (and not just let the LLM chain the atomic tools)?**

The four-step chain is deterministic Python — there is no LLM judgment in
selecting the inventory source, picking thresholds, or aggregating values.
Exposing it as one composer (Invariant 2: deterministic workflows) gives the
agent one well-named verb ("compute the impact envelope") rather than four
sequenced calls. The chat narration string ("X structures impacted, $Y in
damages") gives the chat surface a single load-bearing sentence so the LLM
doesn't have to assemble it from raw envelope fields.

**Invariants this module enforces:**

- **1. Determinism boundary.** No LLM in the chain. Every numeric in the
  returned envelope is computed by ``postprocess_pelicun`` aggregating the
  Pelicun damage layer; the narrative string is a deterministic format of
  three envelope fields.
- **2. Deterministic workflows.** Pure-Python composition of registered
  atomic tools; failures in any step surface as typed
  ``ComputeImpactEnvelopeError`` subclasses with distinct error codes.
- **3. Engine registration.** Composer is registered via the same
  ``workflow_dispatch`` source class as ``run_model_flood_scenario``.
- **10. Minimal parameter surface.** Signature exposes the four
  irreducible inputs: ``flood_layer_uri`` (required), area (``bbox`` OR
  ``location_query``), ``structure_inventory_source`` (NSI vs. MS), and
  ``fragility_set``. Everything else is resolved internally.

Cross-tool dependencies:

- Upstream (consumes): ``geocode_location``, ``fetch_usace_nsi``,
  ``compute_building_density``, ``run_pelicun_damage_assessment``,
  ``run_pelicun_with_buildings``, ``postprocess_pelicun``.
- Downstream (feeds): chat narration; Case summary panel UI; MongoDB
  ``runs`` collection for the parent ``AssessmentEnvelope`` linkage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool
from ..tools.fetchers.socioeconomic.geocode_location import geocode_location
from ..tools.simulation.postprocess_pelicun import (
    PelicunPostprocessError,
    postprocess_pelicun,
)

__all__ = [
    "compute_impact_envelope",
    "ComputeImpactEnvelopeError",
    "ComputeImpactEnvelopeInputError",
    "ComputeImpactEnvelopeGeocodeError",
    "ComputeImpactEnvelopeNSIFetchError",
    "ComputeImpactEnvelopePelicunError",
    "ComputeImpactEnvelopePostprocessError",
]

logger = logging.getLogger("trid3nt_server.workflows.compute_impact_envelope")


# --------------------------------------------------------------------------- #
# Error hierarchy — distinct error_code per upstream step.
# --------------------------------------------------------------------------- #


class ComputeImpactEnvelopeError(RuntimeError):
    """Base class for ``compute_impact_envelope`` failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable``
    guides FR-AS-11 retry logic. Subclasses set both to per-step values.
    """

    error_code: str = "COMPUTE_IMPACT_ENVELOPE_ERROR"
    retryable: bool = False


class ComputeImpactEnvelopeInputError(ComputeImpactEnvelopeError):
    """Bad / missing input args (no flood_layer_uri, no bbox+no location)."""

    error_code = "COMPUTE_IMPACT_ENVELOPE_INPUT"
    retryable = False


class ComputeImpactEnvelopeGeocodeError(ComputeImpactEnvelopeError):
    """``geocode_location`` returned no usable bbox.

    Retryable in the sense that the LLM can re-issue with a different
    location string; not retryable for the same arguments.
    """

    error_code = "GEOCODE_FAILED"
    retryable = False


class ComputeImpactEnvelopeNSIFetchError(ComputeImpactEnvelopeError):
    """``fetch_usace_nsi`` (or ``compute_building_density``) failed.

    Retryable — NSI cluster occasionally returns 5xx; the agent's
    FR-AS-11 surface decides whether to retry.
    """

    error_code = "NSI_FETCH_FAILED"
    retryable = True


class ComputeImpactEnvelopePelicunError(ComputeImpactEnvelopeError):
    """The Pelicun damage assessment step failed.

    Inspect ``__cause__`` for the wrapped ``PelicunDamageError`` /
    ``PelicunWithBuildingsError`` subclass.
    """

    error_code = "PELICUN_UPSTREAM_FAILED"
    retryable = True


class ComputeImpactEnvelopePostprocessError(ComputeImpactEnvelopeError):
    """``postprocess_pelicun`` failed to aggregate the damage layer.

    Inspect ``__cause__`` for the wrapped ``PelicunPostprocessError``
    subclass (input / IO / empty / schema).
    """

    error_code = "POSTPROCESS_FAILED"
    retryable = False


# --------------------------------------------------------------------------- #
# Narration helper.
# --------------------------------------------------------------------------- #


def _format_narrative(envelope: dict[str, Any]) -> str:
    """Produce the headline-metric chat narration string.

    Pattern: ``"<N> structures impacted, $<L> in expected damages,
    <P> population at high risk"``. Population segment is omitted when the
    inventory source has no population data (e.g. MS_BUILDINGS).

    Numbers are formatted with thousands separators for readability; the
    USD figure is rounded to the nearest dollar (the LLM may further
    re-format for context — the load-bearing thing is that the dollar
    sign + number appears verbatim).
    """
    n_damaged = int(envelope.get("n_structures_damaged", 0))
    expected_loss = float(envelope.get("expected_loss_usd", 0.0))
    pop_high_risk = envelope.get("population_at_high_risk")

    parts: list[str] = [
        f"{n_damaged:,} structures impacted",
        f"${expected_loss:,.0f} in expected damages",
    ]
    if pop_high_risk is not None:
        parts.append(f"{int(pop_high_risk):,} population at high risk")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Registered atomic-tool composer (LLM-facing surface).
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="compute_impact_envelope",
    # The composer itself is uncacheable — the underlying atomic steps
    # (NSI fetch, Pelicun damage, postprocess) are each individually
    # cacheable so a re-call of the same inputs hits the per-step caches.
    # The composer's own surface is workflow_dispatch (FR-DC-6).
    #
    # Kickoff calls for ``ttl_class="static-30d"`` on the *result* (composer
    # output is cacheable in spirit because the sub-tools' caches dedupe);
    # we encode the surface-level FR-DC-6 contract via ``cacheable=False`` +
    # ``ttl_class="live-no-cache"`` (the workflow-dispatch shape every other
    # registered workflow uses), and rely on the sub-tools' static-30d
    # caches to deliver the cache-stable behavior end-to-end.
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    supports_global_query=False,
)


@register_tool(
    _METADATA,
    # MCP annotations: read-only at the composer level (the underlying
    # Pelicun step writes a FlatGeobuf; the composer itself does not mutate
    # any new state — it just chains tools). open_world=False because the
    # underlying Pelicun step is intra-GCP; NSI fetch IS external but that
    # tool declares its own open_world hint. destructive=False — additive
    # writes only. idempotent=True — deterministic seeding gives the same
    # ImpactEnvelope for the same flood_layer_uri + bbox + source.
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def compute_impact_envelope(
    flood_layer_uri: str,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    structure_inventory_source: Literal["USACE_NSI", "MS_BUILDINGS"] = "USACE_NSI",
    fragility_set: str | None = None,
    # job-0164: absorb LLM-invented kwargs (Tool argument normalizer ratchet).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Compose flood-layer → structure inventory → Pelicun → ImpactEnvelope.

    Use this (not run_model_flood_scenario, which SIMULATES the flood) when a flood layer already exists and you want the composed damage impact envelope.

    Four-step deterministic chain (no LLM in the loop):

    1. ``geocode_location(location_query)`` (only when ``bbox`` not given).
    2. ``fetch_usace_nsi(bbox)`` — preferred for CONUS — OR
       ``run_pelicun_with_buildings`` (which uses
       ``compute_building_density``) for international bboxes.
    3. ``run_pelicun_damage_assessment(flood_layer_uri, <assets_uri>)``
       (skipped for the MS_BUILDINGS path, where
       ``run_pelicun_with_buildings`` already runs Pelicun internally).
    4. ``postprocess_pelicun(damage_uri, flood_layer_uri)`` →
       ``ImpactEnvelope`` aggregate (SRS Appendix B.6c).

    Use this when:
        - The user has a flood layer URI in hand (typically from
          ``run_model_flood_scenario`` / a Case's primary flood layer) and
          asks for impact / damage / loss / population at risk.
        - The user asks "how much damage", "how many structures impacted",
          "expected losses", "displaced population", or "summarize the
          flood impact" on a generated flood scenario.
        - The Case summary panel needs a single envelope it can cite.

    Do NOT use this for:
        - Cases where no flood layer exists yet — run
          ``run_model_flood_scenario`` first.
        - Per-feature damage layers for spatial exploration on the map —
          call ``run_pelicun_damage_assessment`` or
          ``run_pelicun_with_buildings`` directly. This composer collapses
          per-feature properties into aggregate totals.
        - Non-flood hazards (the v0.1 fragility set is flood-only).

    Examples:
        - "How much damage will the 100-yr flood cause in Fort Myers, FL?"
          → ``flood_layer_uri = <result of run_model_flood_scenario>``;
            ``location_query = "Fort Myers, FL"``.
        - "Estimate displaced population for the Hurricane Ian inundation."
          → ``flood_layer_uri = <Hurricane Ian flood COG URI>``;
            ``bbox = (-81.92, 26.55, -81.80, 26.68)``.

    params:
        flood_layer_uri: the flood depth layer to assess. This MUST be the EXACT
            LayerURI value (copied verbatim) that a ``run_model_flood_scenario`` /
            ``run_model_nws_flood_event_scenario`` call returned EARLIER IN THIS
            CONVERSATION. NEVER invent, construct, or guess this value (e.g. a
            ``flood-depth-peak-<id>`` string you did not receive) — a fabricated id
            does not exist and the call will fail. If no flood scenario has been run
            yet, call ``run_model_flood_scenario`` FIRST, wait for its result, then
            pass that result's layer URI here. Required; non-empty string.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
            ``None``, ``location_query`` is geocoded.
        location_query: free-text place name (geocoded via Nominatim).
            Ignored when ``bbox`` is supplied.
        structure_inventory_source: ``"USACE_NSI"`` (default, CONUS-only,
            best fidelity — real HAZUS occupancy + per-structure value) or
            ``"MS_BUILDINGS"`` (international, Microsoft Global ML
            Buildings density grid → RES1 + class-default values).
        fragility_set: Pelicun fragility set. Defaults to
            ``"hazus_flood_v6"`` when None.

    returns:
        A dict with the following keys:

        - ``envelope_summary``: top-level fields ``n_structures_total``,
          ``n_structures_damaged``, ``n_structures_destroyed``,
          ``expected_loss_usd``, ``loss_percentile_95_usd``,
          ``population_total``, ``population_displaced``,
          ``population_at_high_risk``, ``impact_area_km2``.
        - ``raw_envelope``: full ``ImpactEnvelope.model_dump(mode='json')``.
        - ``narrative``: headline chat string (e.g. ``"1,234 structures
          impacted, $5,678,900 in expected damages, 567 population at high
          risk"``).
        - ``provenance``: dict carrying ``flood_layer_uri``, ``assets_uri``,
          ``damage_layer_uri``, ``structure_inventory_source``,
          ``fragility_set``, ``bbox``, ``location_query``, ``generated_at``.

        The agent surface cites
        ``envelope_summary.n_structures_damaged`` /
        ``envelope_summary.expected_loss_usd`` /
        ``envelope_summary.population_at_high_risk`` — never invented
        numbers (Invariant 1).

    Cache:
        Composer itself: ``cacheable=False`` (workflow dispatch).
        Underlying atomic steps each carry their own cache:
        ``fetch_usace_nsi`` ``static-30d``,
        ``run_pelicun_damage_assessment`` ``static-30d`` (deterministic
        Monte-Carlo seeding), ``postprocess_pelicun`` ``static-30d``.

    Raises (typed; ``error_code`` + ``retryable`` on each):
        ComputeImpactEnvelopeInputError: ``flood_layer_uri`` missing OR
            (``bbox`` AND ``location_query``) both missing.
        ComputeImpactEnvelopeGeocodeError: geocoder returned no bbox.
        ComputeImpactEnvelopeNSIFetchError: ``fetch_usace_nsi`` failed
            (``NSI_FETCH_FAILED``, retryable=True).
        ComputeImpactEnvelopePelicunError: Pelicun step failed
            (``PELICUN_UPSTREAM_FAILED``, retryable=True). Inspect
            ``__cause__`` for the wrapped error.
        ComputeImpactEnvelopePostprocessError: ``postprocess_pelicun``
            failed (``POSTPROCESS_FAILED``, retryable=False).
    """
    # --- Step 0: input validation --------------------------------------- #
    if flood_layer_uri is None or (
        isinstance(flood_layer_uri, str) and not flood_layer_uri.strip()
    ):
        raise ComputeImpactEnvelopeInputError(
            "flood_layer_uri is required (must be a non-empty gs:// URI or local path)"
        )
    if not isinstance(flood_layer_uri, str):
        raise ComputeImpactEnvelopeInputError(
            f"flood_layer_uri must be a string; got {type(flood_layer_uri).__name__}"
        )

    fragility = fragility_set or "hazus_flood_v6"

    # --- Step 1: bbox resolution ---------------------------------------- #
    resolved_bbox: tuple[float, float, float, float]
    if bbox is not None:
        if len(bbox) != 4:
            raise ComputeImpactEnvelopeInputError(
                f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
            )
        resolved_bbox = (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )
    elif location_query is not None:
        logger.info(
            "compute_impact_envelope: geocoding location_query=%r", location_query
        )
        try:
            geo = geocode_location(location_query)
        except Exception as exc:  # noqa: BLE001
            raise ComputeImpactEnvelopeGeocodeError(
                f"geocode_location({location_query!r}) failed: {exc}"
            ) from exc
        geo_bbox = geo.get("bbox") if isinstance(geo, dict) else None
        if not geo_bbox or len(geo_bbox) != 4:
            raise ComputeImpactEnvelopeGeocodeError(
                f"geocode_location({location_query!r}) returned no usable bbox: {geo!r}"
            )
        resolved_bbox = (
            float(geo_bbox[0]),
            float(geo_bbox[1]),
            float(geo_bbox[2]),
            float(geo_bbox[3]),
        )
    else:
        raise ComputeImpactEnvelopeInputError(
            "compute_impact_envelope requires either bbox or location_query"
        )

    # --- Step 2: structure inventory + Pelicun damage assessment -------- #
    assets_uri: str | None = None
    damage_uri: str
    if structure_inventory_source == "USACE_NSI":
        logger.info(
            "compute_impact_envelope: NSI path — fetch_usace_nsi bbox=%s",
            resolved_bbox,
        )
        try:
            nsi_fn = TOOL_REGISTRY["fetch_usace_nsi"].fn
            nsi_layer = nsi_fn(bbox=resolved_bbox)
        except Exception as exc:  # noqa: BLE001
            raise ComputeImpactEnvelopeNSIFetchError(
                f"fetch_usace_nsi failed: {exc}"
            ) from exc
        # NSI returns a LayerURI; pull its .uri for assets_uri.
        assets_uri = getattr(nsi_layer, "uri", None) or str(nsi_layer)

        logger.info(
            "compute_impact_envelope: run_pelicun_damage_assessment hazard=%s assets=%s fragility=%s",
            flood_layer_uri,
            assets_uri,
            fragility,
        )
        try:
            pelicun_fn = TOOL_REGISTRY["run_pelicun_damage_assessment"].fn
            damage_layer = pelicun_fn(
                hazard_raster_uri=flood_layer_uri,
                assets_uri=assets_uri,
                fragility_set=fragility,
            )
        except Exception as exc:  # noqa: BLE001
            raise ComputeImpactEnvelopePelicunError(
                f"run_pelicun_damage_assessment failed: {exc}"
            ) from exc
        damage_uri = getattr(damage_layer, "uri", None) or str(damage_layer)

    elif structure_inventory_source == "MS_BUILDINGS":
        # MS_BUILDINGS path: the composer ``run_pelicun_with_buildings``
        # owns the building-density → point-FGB → Pelicun chain. The
        # inferred assets_uri is the intermediate point FlatGeobuf, which
        # the composer hides — surface ``"<ms_buildings>"`` for provenance.
        logger.info(
            "compute_impact_envelope: MS_BUILDINGS path — run_pelicun_with_buildings "
            "hazard=%s bbox=%s fragility=%s",
            flood_layer_uri,
            resolved_bbox,
            fragility,
        )
        try:
            ms_fn = TOOL_REGISTRY["run_pelicun_with_buildings"].fn
            damage_layer = await ms_fn(
                hazard_raster_uri=flood_layer_uri,
                bbox=resolved_bbox,
                fragility_set=fragility,
            )
        except Exception as exc:  # noqa: BLE001
            # Distinguish building-density / pelicun failures with the
            # PELICUN_UPSTREAM_FAILED code; both surface through the
            # composer's wrapper exception ``PelicunWithBuildingsError``.
            raise ComputeImpactEnvelopePelicunError(
                f"run_pelicun_with_buildings failed: {exc}"
            ) from exc
        damage_uri = getattr(damage_layer, "uri", None) or str(damage_layer)
        assets_uri = "<ms_buildings:intermediate>"

    else:
        raise ComputeImpactEnvelopeInputError(
            f"structure_inventory_source must be 'USACE_NSI' or 'MS_BUILDINGS'; "
            f"got {structure_inventory_source!r}"
        )

    # --- Step 3: postprocess_pelicun ------------------------------------ #
    logger.info(
        "compute_impact_envelope: postprocess_pelicun damage_uri=%s flood_uri=%s",
        damage_uri,
        flood_layer_uri,
    )
    try:
        # M5.5 provenance threading: pass the fragility set actually used in
        # the upstream Pelicun step so the envelope's provenance reflects the
        # run that happened (rather than postprocess_pelicun's hardcoded
        # default). realization_count is not exposed on this composer's surface
        # today, so it is left to the postprocess default (100).
        envelope = await postprocess_pelicun(
            damage_layer_uri=damage_uri,
            flood_layer_uri=flood_layer_uri,
            fragility_set=fragility,
        )
    except PelicunPostprocessError as exc:
        raise ComputeImpactEnvelopePostprocessError(
            f"postprocess_pelicun failed ({exc.error_code}): {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — defensive: anything from the
        # geopandas read path / IO that escapes the typed hierarchy.
        raise ComputeImpactEnvelopePostprocessError(
            f"postprocess_pelicun failed: {exc}"
        ) from exc

    # --- Step 4: response shape ----------------------------------------- #
    envelope_summary = {
        "n_structures_total": envelope.get("n_structures_total"),
        "n_structures_damaged": envelope.get("n_structures_damaged"),
        "n_structures_destroyed": envelope.get("n_structures_destroyed"),
        "expected_loss_usd": envelope.get("expected_loss_usd"),
        "loss_percentile_95_usd": envelope.get("loss_percentile_95_usd"),
        "total_replacement_value_usd": envelope.get("total_replacement_value_usd"),
        "damaged_replacement_value_usd": envelope.get(
            "damaged_replacement_value_usd"
        ),
        "population_total": envelope.get("population_total"),
        "population_displaced": envelope.get("population_displaced"),
        "population_at_high_risk": envelope.get("population_at_high_risk"),
        "impact_area_km2": envelope.get("impact_area_km2"),
        "structure_inventory_source": envelope.get("structure_inventory_source"),
        "pelicun_run_id": envelope.get("pelicun_run_id"),
    }

    narrative = _format_narrative(envelope)

    provenance = {
        "flood_layer_uri": flood_layer_uri,
        "assets_uri": assets_uri,
        "damage_layer_uri": damage_uri,
        "structure_inventory_source": structure_inventory_source,
        "fragility_set": fragility,
        "bbox": list(resolved_bbox),
        "location_query": location_query,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "compute_impact_envelope: done — narrative=%r damage_uri=%s",
        narrative,
        damage_uri,
    )

    return {
        "envelope_summary": envelope_summary,
        "raw_envelope": envelope,
        "narrative": narrative,
        "provenance": provenance,
    }


