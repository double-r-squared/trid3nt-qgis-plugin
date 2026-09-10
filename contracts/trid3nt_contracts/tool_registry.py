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
    "ResolutionConstraintSource",
    "ResolutionSpec",
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


#: WHO owns a resolution bound. ``"solver"`` - a MODEL constraint, living with
#: the template. ``"data"`` - a DATA-native fact, living with the fetcher. A gate
#: card COMPOSES both layers rather than picking one.
ResolutionConstraintSource = Literal["solver", "data"]


class ResolutionSpec(GraceModel):
    """A DECLARED valid-resolution range for one granularity-bearing param.
    Silent coercion to an undeclared resolution is BANNED: a tool declares what
    it can actually run, and an out-of-range ask gets the declared range quoted
    back, typed or gated, never a silent snap.
    """

    #: The tool argument this constrains. Specs are matched to params by name.
    param: str = Field(min_length=1)
    #: The value unit - ``"m"``, ``"px"`` for a pixel cap, ``"arcsec"`` for a
    #: lat/long-native tier.
    unit: str = "m"
    #: The FINEST and COARSEST declared values, INCLUSIVE. ``None`` on a side
    #: declares that side UNBOUNDED. At least one bound, or ``options``, must be
    #: present.
    min_value: float | None = None
    max_value: float | None = None
    #: A human string for the data-native or default resolution, quoted beside
    #: the range. ``None`` when there is no meaningful native.
    native_hint: str | None = None
    #: A DISCRETE valid-value set, mutually exclusive with the min/max window,
    #: for a tool that supports only specific cells.
    options: tuple[float, ...] | None = None
    #: A discretization step within a continuous window. Informational.
    step: float | None = None
    constraint_source: ResolutionConstraintSource
    #: WHY these are the bounds, from evidence - a solve time, an accepted
    #: window, a source's native cell. REQUIRED, so a later reader can check the
    #: bound is real rather than a guess.
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_bounds(self) -> ResolutionSpec:
        """A spec declares a continuous window XOR a discrete option set.
        Unbounded is legitimate ONLY when the ``rationale`` says so, so an empty
        spec cannot masquerade as a forgotten one. With both bounds, ``min <= max``.
        """
        has_window = self.min_value is not None or self.max_value is not None
        has_options = bool(self.options)
        if has_window and has_options:
            raise ValueError(
                "ResolutionSpec: declare a continuous min/max window OR a discrete "
                "options set, not both."
            )
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(
                f"ResolutionSpec: min_value {self.min_value} > max_value "
                f"{self.max_value} for param {self.param!r}."
            )
        if not has_window and not has_options and "unbounded" not in self.rationale.lower():
            raise ValueError(
                "ResolutionSpec: no min/max and no options declares an UNBOUNDED range; "
                "the rationale must say so (contain 'unbounded') so it cannot be "
                "confused with a forgotten declaration."
            )
        return self

    def contains(self, value: float) -> bool:
        """True when ``value`` is inside the declared range (inclusive) / option set."""
        if self.options is not None:
            return any(abs(value - o) <= 1e-9 for o in self.options)
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value > self.max_value:
            return False
        return True

    def range_phrase(self) -> str:
        """Human phrase for the supported range, e.g. ``"20-200 m"`` / ``">=10 m"``."""
        if self.options is not None:
            return f"one of {', '.join(f'{o:g}' for o in self.options)} {self.unit}"
        lo, hi = self.min_value, self.max_value
        if lo is not None and hi is not None:
            return f"{lo:g}-{hi:g} {self.unit}"
        if lo is not None:
            return f">={lo:g} {self.unit}"
        if hi is not None:
            return f"<={hi:g} {self.unit}"
        return f"unbounded ({self.unit})"

    def docstring_line(self) -> str:
        """One line for the tool docstring, front-loading the RANGE so a request
        routes to a valid value, then the constraint owner and native hint."""
        owner = "mesh/solver" if self.constraint_source == "solver" else "data-native"
        native = f"; data native {self.native_hint}" if self.native_hint else ""
        return (
            f"{self.param}: supported {self.range_phrase()} ({owner}){native}. "
            f"Out-of-range asks are quoted the range (typed/gated), never silently "
            f"snapped."
        )

    def quote_back(self, requested: float, *, measured: str | None = None) -> str:
        """The card text for an out-of-range request: the value asked for, the
        supported range and its owner, the data-native hint, and the measured
        cost when there is one - both layers of truth in ONE card."""
        owner = "mesh/solver" if self.constraint_source == "solver" else "data-native"
        parts = [
            f"{requested:g} {self.unit} requested; this tool supports "
            f"{self.range_phrase()} ({owner})"
        ]
        if self.native_hint:
            parts.append(f"data native {self.native_hint}")
        if measured:
            parts.append(measured)
        parts.append(f"pick a {self.param} in range")
        return "; ".join(parts) + "."


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
    #: Explicit rather than inferred, so the uncacheable set is enumerated.
    cacheable: bool = True

    # Both default to the safe, opted-out value, so a tool opts in by passing
    # the keyword rather than by remembering to.

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

    # Annotation hints a consumer reads for exposure, parallelization and
    # capability auditing. All four default to the most CONSERVATIVE value, so
    # a tool that says nothing is treated as the least dangerous case.

    read_only_hint: bool = Field(
        default=True,
        description=(
            "MCP annotation: readOnlyHint. True when the tool has no side "
            "effects and does not mutate any external state (object storage, "
            "the QGIS project, the persisted store). Defaults to True — the "
            "safe assumption for fetchers and compute tools. Set to False for "
            "publish_layer, run_solver, and any other tool that writes."
        ),
    )

    open_world_hint: bool = Field(
        default=False,
        description=(
            "MCP annotation: openWorldHint. True when the tool reaches beyond "
            "the local deployment — external APIs or public data endpoints. "
            "Defaults to False — compute, clip, and local-substrate-only tools "
            "opt out. All fetch_* tools and web_fetch are True; "
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

    # A tool with a granularity-bearing param declares the resolutions it can
    # ACTUALLY run, so the user picks from reality and an out-of-range ask is
    # quoted the range rather than silently snapped. This declaration is the
    # SINGLE source the docstring, the gate card and the registry sweep all read.
    # Default () = no resolution-class param.
    resolution_specs: tuple[ResolutionSpec, ...] = Field(
        default=(),
        description=(
            "Declared valid-resolution ranges, one ResolutionSpec per "
            "granularity-bearing parameter. Read by the docstring, the "
            "payload/input-review gate card, and the registry sweep test that FAILS "
            "when a future resolution-class param ships without a declaration."
        ),
    )

    def resolution_spec_for(self, param: str) -> ResolutionSpec | None:
        """The declared :class:`ResolutionSpec` for ``param``, or ``None``."""
        for spec in self.resolution_specs:
            if spec.param == param:
                return spec
        return None

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
