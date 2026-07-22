"""Case-workflow result envelopes (job-0118).

This module holds typed result shapes for the **higher-order Case workflows**
that compose existing atomic tools and modeling workflows end-to-end. Unlike
``AssessmentEnvelope`` (Appendix B.2) which is the canonical single-hazard /
single-discovery payload, ``CaseOneResult`` (and future siblings) bundle
multiple per-tool outputs (flood layer + N species layers + protected-area
layer + cross-layer impact metrics + a narration-ready summary string) into
one typed return so the higher-order composer can be cited atomically.

The shapes here are deliberately narrow: they reference ``LayerURI`` objects
(per the layer-emission contract, ``docs/decisions/layer-emission-contract.md``,
ADOPTED 2026-06-07) but do not duplicate the AssessmentEnvelope's full
provenance machinery. Per-tool ``DataSource`` records still live on the
underlying tool returns; the case-result is the composition headline.

Invariant 1 (Determinism boundary): every field carries a typed value the
narration cites verbatim — ``impact_metrics`` is a structured dict (not a
prose blob), and ``case_summary_text`` is the deterministic format-string
output of the composer (not LLM-generated narration).

Invariant 7 (Claims carry provenance): per-layer URIs identify their source
data; the composer threads each underlying ``LayerURI`` through unchanged.

v0.1 scope: ``CaseOneResult`` is the Case 1 (Everglades / Big Cypress /
Apalachicola flood + habitat exposure) composer's return. Future cases
land their own result types in this module.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import BBox, GraceModel
from .execution import LayerURI

__all__ = [
    "CaseOneResult",
    # Case 2 — news/alert event ingest (job-0119)
    "EventIngestResult",
    "DerivedEventParam",
    "EventIngestProvenance",
]


class CaseOneResult(GraceModel):
    """Return type for ``model_flood_habitat_scenario`` (job-0118).

    Carries the full result bundle for the Case 1 composer: one flood-depth
    layer (from the SFINCS modeling pipeline), zero-or-more per-species
    occurrence layers (one ``LayerURI`` per species — per-species discipline
    per the conservation-tools memory rule), one WDPA protected-areas layer,
    a structured ``impact_metrics`` dict carrying zonal-statistics output
    over the flood × WDPA overlap, and a deterministic
    ``case_summary_text`` narration string the agent surface can cite.

    Fields:
        bbox: the case bbox (EPSG:4326, [min_lon, min_lat, max_lon, max_lat]).
        flood_layer_uri: ``LayerURI`` for the published flood-depth raster
            (typically a WMS URL after ``publish_layer`` substitution; falls
            back to the ``gs://`` URI when publication is skipped or fails).
            ``None`` when the underlying ``model_flood_scenario`` returned a
            failed envelope (no layers produced).
        species_layers: ordered list of per-species occurrence ``LayerURI``
            objects (one per ``species_keys[i]`` passed to the composer).
            Empty when ``species_keys=[]`` was passed.
        wdpa_layer_uri: ``LayerURI`` for the WDPA protected-areas FlatGeobuf.
            ``None`` when the WDPA fetch returned no features in the bbox.
        impact_metrics: structured zonal-statistics output. Shape is the
            dict returned by ``compute_zonal_statistics`` (``by_zone`` keyed
            by WDPA polygon id when WDPA polygons are the zones, plus
            ``aggregate``); empty dict ``{}`` when zonal stats could not be
            computed (no flood layer OR no WDPA polygons).
        case_summary_text: a deterministic, narration-ready summary string
            built from the layer URIs + impact metrics (e.g.
            ``"Within Big Cypress National Preserve: 246 species
            occurrences (210 Florida panther, 36 Roseate spoonbill), max
            flood depth 1.20 m, mean 0.42 m"``). Never LLM-generated.
        species_counts: per-species occurrence point counts keyed by the
            ``species_key`` passed to the composer (string for cache-key
            stability — ints + names both stringify deterministically).
    """

    schema_version: str = "v1"

    bbox: BBox
    flood_layer_uri: LayerURI | None = None
    species_layers: list[LayerURI] = Field(default_factory=list)
    wdpa_layer_uri: LayerURI | None = None
    impact_metrics: dict[str, Any] = Field(default_factory=dict)
    case_summary_text: str
    species_counts: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Case 2 — news / alert event-ingest composer result (job-0119)
# --------------------------------------------------------------------------- #


class DerivedEventParam(GraceModel):
    """One derived event parameter from cross-source claim aggregation.

    Returned per-target inside ``EventIngestResult.derived_params``. Shape
    mirrors the per-target claim dict that ``aggregate_claims_across_sources``
    returns, promoted into a typed envelope so downstream solvers (MODFLOW in
    sprint-13, etc.) consume a stable contract rather than a free-form dict.

    ``value`` is intentionally ``Any`` because the per-target shape varies:

    - ``location`` -> ``str`` (e.g. ``"Longview, Texas"``)
    - ``scale`` -> ``dict`` (e.g. ``{"value": 15000.0, "unit": "gallon"}``)
    - ``contaminant`` -> ``str`` (e.g. ``"vinyl chloride"``)
    - ``date`` -> ``str`` (ISO-8601 ``yyyy-mm-dd``)
    - ``casualties`` -> ``int``

    ``confidence`` is the source-agreement score from the claim aggregator
    (0.0–1.0; 0.5 for one source, ramps to 0.99 with cross-source agreement).
    ``supporting_sources`` is the list of source URLs that backed this value;
    ``alternatives`` are competing values surfaced for the user to inspect.
    """

    schema_version: Literal["v1"] = "v1"

    value: Any = None
    confidence: float = 0.0
    supporting_sources: list[str] = Field(default_factory=list)
    alternatives: list[dict] = Field(default_factory=list)
    below_threshold: bool = False


class EventIngestProvenance(GraceModel):
    """One source-level provenance entry on an ``EventIngestResult``.

    Carries enough information for the user to drill from a derived parameter
    back to the originating source: the input identifier (URL or alert ID),
    the source type tag, a post-redirect final URL, the page/alert title, a
    short citation snippet (typically a description excerpt), the FR-HEP-2
    source-authority tier, and the fetch timestamp.
    """

    schema_version: Literal["v1"] = "v1"

    source_type: Literal["url", "nws_alert", "storm_event"]
    identifier: str
    final_url: str | None = None
    title: str | None = None
    citation_snippet: str | None = None
    source_authority_tier: int | None = None
    fetched_at: str | None = None  # ISO-8601 UTC


class EventIngestResult(GraceModel):
    """Result of the Case 2 ``model_news_event_ingest`` composer (job-0119).

    Returned by the news/alert-ingest workflow BEFORE any downstream solver
    runs. The user reviews this envelope (via the ``case2-event-ingest-result``
    web envelope rendered in the chat) and approves the derived parameters;
    only then does sprint-13's MODFLOW (or another downstream modeler) pick
    up. **No solver dispatch happens inside the workflow that returns this.**

    Invariant 9 (confirmation before consequence): this envelope is itself
    the confirmation substrate — the workflow STOPS here, and the agent
    asks the user "proceed to model the groundwater plume?" before any
    solver is dispatched.

    Fields:

    - ``event_type`` — hazard label routed from caller intent (e.g. ``"spill"``,
      ``"flood"``, ``"wildfire"``, ``"hurricane"``). Open enum so a new event
      type does not break the envelope.
    - ``derived_params`` — per-target ``DerivedEventParam`` map. Targets that
      were extracted appear here; missing targets are simply absent (not
      null-valued) so callers can distinguish "no mention found" from "value
      is None".
    - ``provenance`` — per-source ``EventIngestProvenance`` entries (one per
      input source), with citation snippets and source-authority tiers.
    - ``bbox`` — resolved bounding box for the event location (EPSG:4326), or
      ``None`` if location could not be resolved to a bbox.
    - ``presentation_text`` — deterministic human-readable summary the web UI
      renders in the chat-pipeline-card review modal. Composed by the
      workflow body from the derived params — never LLM-generated.
    """

    schema_version: Literal["v1"] = "v1"

    event_type: str
    derived_params: dict[str, DerivedEventParam] = Field(default_factory=dict)
    provenance: list[EventIngestProvenance] = Field(default_factory=list)
    bbox: BBox | None = None
    presentation_text: str
