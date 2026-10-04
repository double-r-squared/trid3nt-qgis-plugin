"""Atomic-tool registration metadata.

Every tool that may issue a network call declares one ``AtomicToolMetadata`` at
registration, and a misconfigured one raises at construction - before the tool
is reachable at all. ``ttl_class`` is declared by the tool, never judged by a
model, and no cost or latency-estimate field lives here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel
from .gate_spec import GateKind, GateSpec, LeverSpec

__all__ = [
    "TTLClass",
    "TTL_CLASSES",
    "EngineTier",
    "AtomicToolMetadata",
    "GateKind",
    "GateSpec",
    "LeverSpec",
]


# Re-exported for callers that import them from here. The authoritative home
# is the errors module; either path resolves the same objects.
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


#: The four TTL classes, one declared per atomic tool.
TTLClass = Literal["static-30d", "semi-static-7d", "dynamic-1h", "live-no-cache"]

#: Tuple form of the same four classes.
TTL_CLASSES: tuple[str, ...] = (
    "static-30d",
    "semi-static-7d",
    "dynamic-1h",
    "live-no-cache",
)


#: Retrieval tier - what DECOUPLES registration from model-facing visibility.
#:
#: - ``general`` - the ordinary per-turn retrieval pool.
#: - ``door`` - a read-only engine concierge that ALSO competes in that pool.
#: - ``template`` - excluded from the default pool and surfaced only by its
#:   door's gate expansion.
#: - ``catalog`` - excluded from the default pool like a template, but KEPT in
#:   the search index, so a discovery hit can still rank and expand it.
#: - ``internal`` - excluded from BOTH the pool and the index, with no door to
#:   expand it: reachable only by an in-process call from another tool.
EngineTier = Literal["general", "door", "template", "catalog", "internal"]


class AtomicToolMetadata(GraceModel):
    """Cache-shim metadata for one atomic tool's registration.
    Registration is REFUSED when this is missing, incomplete, or fails the
    cross-field validator - so a misconfigured tool never reaches the wire.
    """

    #: The tool's function name, and the registry key.
    name: str = Field(min_length=1)
    #: ``"live-no-cache"`` is reserved for the uncacheable-by-construction set:
    #: interactive solicitation, envelope emission, persistence writes, solver
    #: dispatch.
    ttl_class: TTLClass
    #: The prefix in the cache layout. Required when cacheable, and omittable
    #: when not, since nothing is written.
    source_class: str | None = None
    #: DERIVED from ``ttl_class``, which already decides it: every class but
    #: ``live-no-cache`` is cached. Unstated it resolves; stated it must agree
    #: with the class, which the cross-field validator below is.
    cacheable: bool | None = None

    # Both default to the safe, opted-out value, so a tool opts in by passing
    # the keyword rather than by remembering to.

    supports_global_query: bool = Field(
        default=False,
        description=(
            "True if this tool accepts ``bbox=None`` to mean global/CONUS-wide "
            "query. Default False (safer - tools opt in). When False, calling "
            "with ``bbox=None`` must raise ``ToolInputError(code='BBOX_REQUIRED', "
            "retryable=False)`` BEFORE issuing any network call."
        ),
    )

    payload_mb_estimator_name: str | None = Field(
        default=None,
        description=(
            "Optional reference (Python identifier) to a callable in the tool "
            "module's ``__init__`` that estimates expected payload MB given "
            "the tool's args. The callable signature is "
            "``estimate_payload_mb(**args) -> float``. The chat-warning system "
            "(``tool-payload-warning`` envelope) reads this metadata to decide "
            "when to gate a large fetch behind explicit user confirmation."
        ),
    )

    # Annotation hints a consumer reads for exposure, parallelization and
    # capability auditing. All four default to the most CONSERVATIVE value, so
    # a tool that says nothing is treated as the least dangerous case.

    read_only_hint: bool = Field(
        default=True,
        description=(
            "MCP annotation: readOnlyHint. True when the tool has no side "
            "effects and does not mutate any external state (object storage, "
            "the QGIS project, the persisted store). Defaults to True - the "
            "safe assumption for fetchers and compute tools. Set to False for "
            "publish_layer, run_solver, and any other tool that writes."
        ),
    )

    open_world_hint: bool = Field(
        default=False,
        description=(
            "MCP annotation: openWorldHint. True when the tool reaches beyond "
            "the local deployment - external APIs or public data endpoints. "
            "Defaults to False - compute, clip, and local-substrate-only tools "
            "opt out. All fetch_* tools are True; "
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
            "additional side effects. Defaults to True - fetchers with the cache "
            "shim satisfy this property. Set to False for tools that emit pipeline "
            "state (wait_for_completion), dispatch a solver run (run_solver), "
            "write stored artifacts (publish_layer), or interact with stateful "
            "systems in non-idempotent ways."
        ),
    )


    # Two OPTIONAL fields for the engine-door family, ORTHOGONAL to the
    # cacheable / ttl_class rule - no cross-field validator joins them. The soft
    # convention that a door or template carries an engine slug is enforced
    # outside this module, so the contract stays a pure shape.

    engine: str | None = Field(
        default=None,
        description=(
            "Owning engine slug for an engine-door family member (e.g. "
            "'modflow', 'sfincs'). None (default) for every non-engine tool - "
            "zero impact on existing registrations. A door lists / gate-expands "
            "over its engine's tier=template members filtered by this slug."
        ),
    )

    tier: EngineTier = Field(
        default="general",
        description=(
            "Retrieval tier. 'general' (default) - the ordinary per-turn "
            "retrieval pool (today's behaviour). 'door' - a read-only engine "
            "concierge; ALSO retrievable in the per-turn pool (doors compete "
            "with general). 'template' - a registered engine template EXCLUDED "
            "from the default pool, surfaced only by its door's gate expansion "
            "(select-then-call). Excluding tier=template decouples registration "
            "from retrieval visibility."
        ),
    )

    # A consequential run or a heavy fetch DECLARES its confirm gate here, so
    # membership is read from METADATA rather than from a hand-wired name set.
    # Presence is the ONE membership signal. Default None = un-gated.
    gate_spec: GateSpec | None = Field(
        default=None,
        description=(
            "Declared confirm gate. Presence is the server gate engine's "
            "membership signal; names the pure estimate/pin providers + the card's "
            "levers. None (default) for every un-gated tool."
        ),
    )

    @model_validator(mode="after")
    def _validate_cacheable_consistency(self) -> AtomicToolMetadata:
        if self.cacheable is None:
            object.__setattr__(self, "cacheable",
                               self.ttl_class != "live-no-cache")
            return self
        """A cacheable tool must name a source class and must not be live-only:
        the first cannot build a cache key, the second would never hit. An
        uncacheable tool must be live-only, or the cache appears to be in play.
        """
        if self.cacheable:
            if self.ttl_class == "live-no-cache":
                raise ValueError(
                    "cacheable=True is inconsistent with ttl_class='live-no-cache'; "
                    "a cacheable tool must declare static-30d / semi-static-7d / dynamic-1h."
                )
            if not self.source_class:
                raise ValueError(
                    "cacheable=True requires a non-empty source_class "
                    "(used as the <source-class> prefix in cache/<source-class>/<hash>.<ext>)."
                )
        else:
            if self.ttl_class != "live-no-cache":
                raise ValueError(
                    f"cacheable=False requires ttl_class='live-no-cache'; "
                    f"got ttl_class={self.ttl_class!r}."
                )
        return self
