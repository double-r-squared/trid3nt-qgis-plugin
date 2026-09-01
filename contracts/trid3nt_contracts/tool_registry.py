"""Atomic-tool registration metadata.

This module owns ``AtomicToolMetadata`` — the pydantic v2 model every
external-API atomic tool declares at registration time so the cache shim
(SRS §3.9..6) can route the call correctly. ``agent`` consumes
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

The four TTL classes match SRS §3.9 verbatim. Misconfigured tools
fail-fast at import time (: "cache class is a required property
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


#: The four TTL classes registered per atomic tool (SRS).
#:
#: Names match the kickoff verbatim. NOTE: SRS prose at
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


#: Retrieval tier for the engine-door refactor (docs/specs/engine-door-refactor.md).
#: ``general`` (default) is the ordinary per-turn retrieval pool; ``door`` is a
#: read-only engine concierge that ALSO competes in the per-turn pool; ``template``
#: is a registered engine template EXCLUDED from the default pool and surfaced only
#: by its door's gate expansion (select-then-call). Registration is thereby
#: decoupled from retrieval visibility.
#: ``catalog`` (catalog-surfacing experiment) is a spec-served data source EXCLUDED
#: from the default declarable pool (like ``template``) BUT KEPT in the search index
#: so a discovery hit can rank + gate-expand it (Design 2) or a card projection can
#: surface it (Design 1). It diverges from ``template`` precisely in staying indexed.
#: ``internal`` is a registry-resolvable tool with NO model-facing surface at all:
#: excluded from the default declarable pool AND from the search index (like
#: ``template``, but with no door to gate-expand it), so it is reachable ONLY by an
#: in-process ``TOOL_REGISTRY[name].fn`` call from another tool. Used for an absorbed
#: seam that a public tool resolves internally (fetch_copernicus_dem <- fetch_dem).
EngineTier = Literal["general", "door", "template", "catalog", "internal"]


#: WHO owns a resolution bound (the two-layer-truth architecture).
#: ``"solver"`` - the bound is a MODEL constraint (a mesh-generator's edge-length
#: window, a node-budget ceiling, an output-raster cap); it lives with the template.
#: ``"data"`` - the bound is a DATA-native fact (a source's finest cell, a tier
#: floor); it lives with the fetcher. The gate card COMPOSES both layers.
ResolutionConstraintSource = Literal["solver", "data"]


class ResolutionSpec(GraceModel):
    """A DECLARED valid-resolution range for one granularity-bearing tool param.

    NATE's clamp ruling: silent coercion to an undeclared resolution is
    BANNED. A tool DECLARES the resolutions it can actually run (min/max or a discrete
    option set) so the user picks from REALITY; an out-of-range ask gets the declared
    range QUOTED BACK (typed / gated), never a silent snap. This is the machine-readable
    carrier of that declaration - one per resolution-class parameter. It is read by
    (a) the tool docstring (via :meth:`docstring_line`), (b) the payload / input-review
    gate card (via :meth:`quote_back`), and (c) the self-enforcing registry sweep test.

    Fields (the kickoff shape ``{min, max, native_hint, step/options, unit,
    constraint_source, rationale}``):

    - ``param`` - the tool argument this constrains (e.g. ``"resolution_m"``,
      ``"min_edge_length_m"``). The registry sweep matches specs to params by this name.
    - ``unit`` - the value unit (``"m"`` default; ``"px"`` for an output-pixel cap,
      ``"arcsec"`` for a lat/long-native source tier).
    - ``min_value`` / ``max_value`` - the FINEST (smallest) and COARSEST (largest)
      declared values, INCLUSIVE. ``None`` on either side declares that side UNBOUNDED
      (e.g. no coarse ceiling). At least one bound, or ``options``, must be present.
    - ``native_hint`` - a human string for the DATA-native / default resolution the
      card quotes alongside the range (e.g. ``"CUDEM 1/9\" ~3 m nearshore; ETOPO ~450 m
      offshore"``, ``"3DEP 10 m"``). ``None`` when there is no meaningful native.
    - ``options`` - a DISCRETE valid-value set (mutually exclusive with a continuous
      min/max window); used when only specific cells are supported.
    - ``step`` - a discretization step within a continuous window, when the tool snaps
      to a grid of allowed values (informational; ``None`` = continuous).
    - ``constraint_source`` - ``"solver"`` or ``"data"`` (the two-layer-truth owner).
    - ``rationale`` - WHY these are the bounds, evidence-based (the node-budget solve
      time, the mesh-generator's accepted window, the source's native cell). Required so
      a future reader/auditor can check the bound is real, not a guess.
    """

    param: str = Field(min_length=1)
    unit: str = "m"
    min_value: float | None = None
    max_value: float | None = None
    native_hint: str | None = None
    options: tuple[float, ...] | None = None
    step: float | None = None
    constraint_source: ResolutionConstraintSource
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_bounds(self) -> ResolutionSpec:
        """A spec must declare a real constraint: continuous window OR discrete options.

        Unbounded is a legitimate declaration (both bounds ``None`` + no options) ONLY
        when the ``rationale`` explicitly says so, so an empty spec cannot masquerade as
        a forgotten one. A discrete ``options`` set and a continuous ``min/max`` window
        are mutually exclusive - pick one. When both bounds are set, ``min <= max``.
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
        """One-line declaration for the tool docstring (consistent across tools).

        Front-loads the range so the LLM routes a request to a valid value; names the
        constraint owner + native hint so an out-of-range ask is self-explanatory.
        """
        owner = "mesh/solver" if self.constraint_source == "solver" else "data-native"
        native = f"; data native {self.native_hint}" if self.native_hint else ""
        return (
            f"{self.param}: supported {self.range_phrase()} ({owner}){native}. "
            f"Out-of-range asks are quoted the range (typed/gated), never silently "
            f"snapped."
        )

    def quote_back(self, requested: float, *, measured: str | None = None) -> str:
        """The gate/typed-error card text for an out-of-range request.

        Composes the two-layer truth in ONE card: the requested value, the supported
        range + its owner, the data-native hint, and (optionally) the measured cost.
        e.g. ``"30 m requested; this tool supports 20-200 m (mesh/solver); data native
        10 m; pick a value in range."``
        """
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
    """Cache-shim metadata for an atomic tool's registration.

    Every atomic tool that may issue a network call to an external public data
    source declares one of these at registration time. The agent service's
    tool-registry refuses to register a tool whose metadata is missing,
    incomplete, or fails the cross-field validator below.

    Fields:

    - ``name`` — atomic-tool function name (Python identifier, e.g.
      ``"fetch_dem"``). The agent registry uses this as the registry key.
    - ``ttl_class`` — one of the four classes. Required for every
      external-API tool. ``"live-no-cache"`` is reserved for the
      uncacheable-by-construction enumeration (interactive solicitation
      tools, envelope emitters, persistence writes, solver dispatchers).
    - ``source_class`` — the ``<source-class>`` prefix in the cache layout
      (e.g. ``"dem"``, ``"buildings"``, ``"geocode"``).
      Required when ``cacheable=True``; MAY be omitted when ``cacheable=False``
      (no cache prefix is needed if nothing is written).
    - ``cacheable`` — explicit boolean for enumeration; defaults to
      ``True`` because the cacheable case is the common case. ``False`` for
      interactive solicitation tools, envelope emitters, persistence writes,
      and solver dispatchers.

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

    # --- Wave 1.5 additions (schema-20260608)
    #
    # Both fields default to safe / opt-out values so the ~30 existing
    # ``AtomicToolMetadata(...)`` call sites in src/
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


    # --- Engine-door refactor additions (docs/specs/engine-door-refactor.md) --- #
    #
    # Two OPTIONAL fields for the engine-door family. Both default to the
    # zero-impact value so all existing ``AtomicToolMetadata(...)`` call sites
    # keep working untouched (additive, same pattern as the Wave 1.5 / 4.10
    # additions above). They are ORTHOGONAL to the cacheable/ttl_class rule -
    # no new cross-field validator. The soft convention "tier in {door,
    # template} SHOULD carry a non-null engine" is enforced server/audit-side,
    # NOT here, to keep the contract a pure shape.

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

    # --- Declared resolutions (NATE's clamp ruling)
    #
    # A tool with a granularity-bearing param DECLARES the resolutions it can
    # actually run so the user picks from reality; an out-of-range ask is quoted
    # the range (typed/gated), never silently snapped. The declaration is the
    # SINGLE source read by the docstring, the gate card, and the self-enforcing
    # registry sweep test. Default () = no resolution-class param (zero impact on
    # the ~200 non-granularity tools). The two-layer-truth architecture: a fetcher
    # declares constraint_source='data' specs, a template declares 'solver' specs;
    # the gate card composes both.
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

    # --- Declared confirm gate (the gate-collapse carrier)
    #
    # A consequential solver run or a heavy raster fetch DECLARES its confirm gate
    # here so the server gate engine reads membership from METADATA, not a hand-wired
    # SOLVER_CONFIRM_TOOLS / FETCH_CONFIRM_TOOLS name set. Presence of a GateSpec is the
    # ONE membership signal; the spec names the pure card/pin providers (by dotted import
    # path) exported from the tool's own module and declares the levers the card offers.
    # Default None = un-gated (zero impact on every non-gated tool), mirroring the
    # resolution_specs default-() additive shape.
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
        """Enforce the cross-field consistency rule."""
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
