"""Atomic-tool registration metadata (FR-DC-2, FR-CE-8, FR-AS-3).

This module owns ``AtomicToolMetadata`` — the pydantic v2 model every
external-API atomic tool declares at registration time so the cache shim
(SRS §3.9 / FR-DC-1..6) can route the call correctly. ``agent`` consumes
this model in the ADK FunctionTool registry; ``schema`` owns the shape.

Why a dedicated ``tool_registry`` module rather than extending ``agent.py``
(which currently holds tool-docstring conventions and the
``tool_category`` vocabulary)?

- ``tool_metadata`` is convention-only (docstring sections, allowed
  ``tool_category`` strings). It carries no pydantic model.
- ``AtomicToolMetadata`` IS a pydantic v2 model with a cross-field
  ``model_validator`` — a different shape of contract surface. Mixing
  validators into a convention-only module would obscure both.
- The agent service will likely accrete other tool-registration models
  (tool-result schemas, retry-policy descriptors, etc.); giving the
  registry its own module keeps the seam clean.

The four TTL classes match SRS §3.9 FR-DC-2 verbatim. Misconfigured tools
fail-fast at import time (FR-CE-8: "cache class is a required property
validated at tool-registration time").

Invariants this module is responsible for:
- **Invariant 1 (Determinism boundary).** ``ttl_class`` is workflow-declared,
  never LLM-judged; the validator refuses inconsistent combinations.
- **Invariant 9 (No cost theater).** No cost / dollar / latency-estimate
  fields. The cache shim's job is correctness + freshness, not pricing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel

__all__ = [
    "TTLClass",
    "TTL_CLASSES",
    "AtomicToolMetadata",
]


# Re-export ToolInputError + codes here as a convenience for tools that
# already import from ``trid3nt_contracts.tool_registry``. Authoritative
# home is ``trid3nt_contracts.errors``; consumers may use either path.
from .errors import (  # noqa: E402  (intentional: keep __all__ above the re-export)
    TOOL_INPUT_ERROR_CODES,
    ToolInputError,
    ToolInputErrorCode,
)

__all__ += [
    "ToolInputError",
    "ToolInputErrorCode",
    "TOOL_INPUT_ERROR_CODES",
]


#: The four TTL classes registered per atomic tool (SRS FR-DC-2).
#:
#: Names match the kickoff verbatim. NOTE: SRS FR-DC-2 prose at
#: ``docs/srs/03-functional-requirements.md`` describes the live class as
#: "encoded as ``ttl_class: 'none'``" — that prose-vs-kickoff naming gap is
#: surfaced as an Open Question in this job's report. The pydantic value here
#: is ``"live-no-cache"`` (kickoff-frozen); a follow-up SRS amendment may
#: harmonize the prose to the same literal.
TTLClass = Literal["static-30d", "semi-static-7d", "dynamic-1h", "live-no-cache"]

#: Tuple form of the four TTL classes (useful for parametrized tests + the
#: agent-side registry's known-class assertions).
TTL_CLASSES: tuple[str, ...] = (
    "static-30d",
    "semi-static-7d",
    "dynamic-1h",
    "live-no-cache",
)


class AtomicToolMetadata(GraceModel):
    """Cache-shim metadata for an atomic tool's registration (FR-CE-8, FR-DC-2).

    Every atomic tool that may issue a network call to an external public data
    source declares one of these at registration time. The agent service's
    tool-registry refuses to register a tool whose metadata is missing,
    incomplete, or fails the cross-field validator below.

    Fields:

    - ``name`` — atomic-tool function name (Python identifier, e.g.
      ``"fetch_dem"``). The agent registry uses this as the registry key.
    - ``ttl_class`` — one of the four FR-DC-2 classes. Required for every
      external-API tool. ``"live-no-cache"`` is reserved for the FR-DC-6
      uncacheable-by-construction enumeration (interactive solicitation
      tools, envelope emitters, MongoDB writes, solver dispatchers).
    - ``source_class`` — the ``<source-class>`` prefix in the cache bucket
      layout per FR-DC-1 (e.g. ``"dem"``, ``"buildings"``, ``"geocode"``).
      Required when ``cacheable=True``; MAY be omitted when ``cacheable=False``
      (no bucket prefix is needed if nothing is written).
    - ``cacheable`` — explicit boolean for FR-DC-6 enumeration; defaults to
      ``True`` because the cacheable case is the common case. ``False`` for
      interactive solicitation tools, envelope emitters, MongoDB writes,
      and solver dispatchers per FR-DC-6.

    Cross-field rule (``_validate_cacheable_consistency``):

    - ``cacheable=True`` ⇒ ``ttl_class != "live-no-cache"`` AND
      ``source_class`` is non-empty. A cacheable tool with a live-no-cache
      class would never hit; a cacheable tool with no source_class can't
      construct a cache key path.
    - ``cacheable=False`` ⇒ ``ttl_class == "live-no-cache"``. The other
      classes would suggest the cache is in play.

    The validator runs at construction time, so a misconfigured registration
    raises ``ValidationError`` before the tool is reachable on the wire.
    """

    name: str = Field(min_length=1)
    ttl_class: TTLClass
    source_class: str | None = None
    cacheable: bool = True

    # --- Wave 1.5 additions (job-0114-schema-20260608) --- #
    #
    # Both fields default to safe / opt-out values so the ~30 existing
    # ``AtomicToolMetadata(...)`` call sites in server/src/
    # trid3nt_server/tools/*.py keep working untouched. New tools and
    # follow-ups opt in by passing the keyword.

    supports_global_query: bool = Field(
        default=False,
        description=(
            "True if this tool accepts ``bbox=None`` to mean global/CONUS-wide "
            "query. Default False (safer — tools opt in). When False, calling "
            "with ``bbox=None`` must raise ``ToolInputError(code='BBOX_REQUIRED', "
            "retryable=False)`` BEFORE issuing any network call. See memory: "
            "feedback_layer_global_bbox_policy."
        ),
    )

    payload_mb_estimator_name: str | None = Field(
        default=None,
        description=(
            "Optional reference (Python identifier) to a callable in the tool "
            "module's ``__init__`` that estimates expected payload MB given "
            "the tool's args. The callable signature is "
            "``estimate_payload_mb(**args) -> float``. The Wave 2 chat-warning "
            "system (``tool-payload-warning`` envelope) reads this metadata to "
            "decide when to gate a large fetch behind explicit user "
            "confirmation. See memory: feedback_large_payload_chat_warning."
        ),
    )

    # --- Wave 4.10 MCP annotation hints (job-B12) --- #
    #
    # MCP-emerging-standard annotation fields for downstream consumers
    # (MCP exposure, parallelization decisions, lethal-trifecta auditing).
    # All four default to the safest / most-conservative value so existing
    # call sites are backward-compatible; individual tools opt in by passing
    # the keyword at registration or via model_copy(update=...).

    read_only_hint: bool = Field(
        default=True,
        description=(
            "MCP annotation: readOnlyHint. True when the tool has no side "
            "effects and does not mutate any external state (GCS, QGIS project, "
            "MongoDB, Cloud Run). Defaults to True — the safe assumption for "
            "fetchers and compute tools. Set to False for publish_layer, "
            "run_solver, qgis_process, run_pelicun_damage_assessment, and any "
            "other tool that writes."
        ),
    )

    open_world_hint: bool = Field(
        default=False,
        description=(
            "MCP annotation: openWorldHint. True when the tool issues calls to "
            "external APIs or public data endpoints outside the GCP "
            "project boundary. Defaults to False — compute, clip, and intra-GCP "
            "tools opt out. All fetch_* tools and web_fetch are True; "
            "catalog_search/catalog_fetch are True because they ultimately hit "
            "Tier-2/3 external endpoints."
        ),
    )

    destructive_hint: bool = Field(
        default=False,
        description=(
            "MCP annotation: destructiveHint. True when the tool can overwrite "
            "or permanently alter existing state in a way that is difficult to "
            "reverse (e.g. mutating the canonical .qgs project via publish_layer). "
            "Defaults to False. Distinguished from read_only_hint=False: a tool "
            "may be non-readonly (it writes) without being destructive (the write "
            "is additive / ephemeral). publish_layer is the only current True case "
            "because it overwrites a layer entry in the shared .qgs project."
        ),
    )

    idempotent_hint: bool = Field(
        default=True,
        description=(
            "MCP annotation: idempotentHint. True when calling the tool multiple "
            "times with the same arguments produces the same result without "
            "additional side effects. Defaults to True — fetchers with the cache "
            "shim satisfy this property. Set to False for tools that emit pipeline "
            "state (wait_for_completion), dispatch Cloud Run jobs (run_solver, "
            "qgis_process), write GCS artifacts (run_pelicun_damage_assessment, "
            "publish_layer), or interact with stateful systems in non-idempotent ways."
        ),
    )

    # --- Deterministic layer auto-publish (NATE 2026-06-26) --- #
    #
    # When a tool returns a renderable RASTER LayerURI carrying a raw object-store
    # uri (s3:// / gs://), the layer_uri_emit seam DROPS it (MapLibre cannot fetch
    # an object-store uri), so historically it only ever rendered if the LLM
    # SEPARATELY called publish_layer to convert the COG to an http(s) TiTiler
    # tile URL. NATE's directive: "we should not have the LLM enforce publishing
    # of layers — this should just be done without LLM intervention." The server
    # dispatch wrapper now auto-calls publish_layer for any such droppable raster.
    #
    # Default True: terminal raster products (compute_hillshade / slope / aspect /
    # colored_relief / ndvi / blended / canopy, clip_raster_*, and the raster
    # FETCHERS the user normally wants to see) auto-publish. Set False for pure
    # INTERMEDIATE rasters whose raw output the user should not auto-see (e.g. the
    # raw DEM that exists only to feed compute_hillshade / compute_slope). An
    # intermediate that the LLM explicitly chooses to publish still renders via the
    # publish_layer wrap-site; auto-publish=False only suppresses the AUTOMATIC
    # render of the raw input.
    auto_publish: bool = Field(
        default=True,
        description=(
            "When True (default), a renderable raster LayerURI this tool returns "
            "carrying a raw object-store uri (s3:// / gs://) is AUTOMATICALLY "
            "published server-side (publish_layer -> http(s) tile URL) so it "
            "renders without the LLM calling publish_layer. Set False for pure "
            "intermediate rasters (e.g. fetch_dem's raw DEM) whose raw output the "
            "user should not auto-see. Has no effect on vector layers, on layers "
            "that already carry an http(s) uri, or on publish_layer itself."
        ),
    )

    @model_validator(mode="after")
    def _validate_cacheable_consistency(self) -> AtomicToolMetadata:
        """Enforce the FR-DC-6 cross-field consistency rule."""
        if self.cacheable:
            if self.ttl_class == "live-no-cache":
                raise ValueError(
                    "cacheable=True is inconsistent with ttl_class='live-no-cache'; "
                    "a cacheable tool must declare static-30d / semi-static-7d / dynamic-1h."
                )
            if not self.source_class:
                raise ValueError(
                    "cacheable=True requires a non-empty source_class "
                    "(used as the <source-class> prefix in gs://<bucket>/cache/<source-class>/<hash>.<ext>)."
                )
        else:
            if self.ttl_class != "live-no-cache":
                raise ValueError(
                    f"cacheable=False requires ttl_class='live-no-cache'; "
                    f"got ttl_class={self.ttl_class!r}."
                )
        return self
