"""Promotion registration (data-router fold, phase-2 wave 1 -- the first real cut).

Both parity gates PASSED for the 5 pilots (replication 5/5 edge-matrix +
routing SUPPORTED byte-identical), so per the cull doctrine the hand-written
twins DIE and their spec-driven surfaces take their names. This module registers
each ``source.yaml`` spec as THE tool under its twin name at import time
(``tier="general"`` -> the DEFAULT retrieval pool), NOT behind an env toggle: the
experiment machinery retired here; promotion is the default.

INDISTINGUISHABILITY (data-router-fold.md retention principle): the promoted tool
is byte-identical to the twin at every consumer surface EXCEPT the callable body:

- Docstring: carried VERBATIM from the twin (``spec.docstring``), so the
  ``FunctionDeclaration`` description AND the BM25/dense retrieval-index document
  text do not shift (the routing-parity index invariant).
- Signature: SYNTHESIZED from ``spec.params`` (:func:`promoted_signature`) with
  the twin's exact param names / required set / defaults, so
  ``FunctionDeclaration.from_callable`` builds the same inputSchema and the
  dispatch-time ``tool_arg_normalizer`` (``inspect.signature`` + ``get_type_hints``)
  sees the twin's params.
- Callable seam: ``TOOL_REGISTRY[name].fn`` is the router closure, so every nested
  consumer (``sfincs_forcing_autowire`` et al.) resolves the same name to a
  ``(**kwargs) -> LayerURI`` callable -- a mechanical repoint, envelope unchanged.
- Payload gate: a per-spec synthetic module exposes the synthesized
  ``estimate_payload_mb`` so the server ``tool-payload-warning`` seam resolves it
  exactly as it resolved the twin's module-level estimator.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from . import router
from .spec import compose_specs_from_tree

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.registration"
)

__all__ = [
    "promoted_signature",
    "register_spec",
    "register_specs_from_tree",
    "registered_spec_names",
    "clear_specs_for_tests",
    "catalog_arm",
    "CATALOG_ARM_ENV",
    "spec_card",
    "search_spec_cards",
]

#: twin_name -> SourceSpec for every promoted spec-driven tool (diagnostics/tests).
_SPEC_REGISTRY: dict[str, SourceSpec] = {}

#: Catalog-surfacing experiment flag (experiments/catalog_surfacing/DESIGN.md).
#: UNSET (default) -> the 14 spec-served sources register tier="general" (ambient,
#: today's behaviour, registry unchanged at 190). "1" (Design 1, card-carried),
#: "2" (Design 2, discovery-expands-declaration), or "3" (Design 3, stratified-pool
#: auto-trigger composed declaration; docs/specs/stratified-pools.md) -> they
#: register tier="catalog": EXCLUDED from the default declarable pool but KEPT in
#: the search index. The flag is read at import so each arm runs in its OWN process
#: with a clean pool; DEFAULT behaviour is byte-identical when it is unset.
CATALOG_ARM_ENV = "TRID3NT_CATALOG_ARM"


def catalog_arm() -> str | None:
    """The active catalog-surfacing arm ("1" / "2" / "3") or None when unset/invalid."""
    val = os.environ.get(CATALOG_ARM_ENV, "").strip()
    return val if val in ("1", "2", "3") else None


def _annotation_for(ptype: str) -> Any:
    """The schema-compatible Python annotation for a spec param type.

    bbox -> ``list[float]`` (the adapter simplifies the twin's tuple annotation to
    the same array schema); int/float pass through; every string-ish type
    (``iso_date`` / ``enum`` / ``str``) -> ``str``.
    """
    if ptype == "bbox":
        return list[float]
    if ptype == "point":
        return list[float]
    if ptype == "int_range":
        return list[int]
    if ptype == "datetime_range":
        return list[str]
    if ptype == "float_list":
        return list[float]
    if ptype == "str_list":
        return list[str]
    if ptype == "bool":
        return bool
    if ptype == "int":
        return int
    if ptype == "float":
        return float
    # iso_date / enum / str / date_compact -> str.
    return str


def promoted_signature(spec: SourceSpec) -> tuple[inspect.Signature, dict[str, Any]]:
    """Synthesize the promoted tool's signature + annotations from ``spec.params``.

    Required (no-default) params first, defaulted params next, then a VAR_KEYWORD
    absorber (the twin's ``**_extra_ignored``). A param is REQUIRED-in-signature iff
    it is ``required`` and carries no ``default`` -- exactly the twin's contract, so
    ``from_callable`` reproduces the twin's inputSchema ``properties`` + ``required``.
    """
    required: list[inspect.Parameter] = []
    optional: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for pname, pspec in spec.params.items():
        ann = _annotation_for(pspec.type)
        if pspec.required and pspec.default is None:
            annotations[pname] = ann
            required.append(
                inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
            )
        else:
            # The adapter marks a None-default NON-Optional annotation as
            # required-in-schema (the "None-default is required" quirk wave-2
            # relies on: min_voltage_kv / year_range / date). A param that the
            # twin author wrote ``T | None = None`` (Optional) is NOT required in
            # the twin schema; ``schema_optional`` reproduces that by annotating
            # ``X | None`` so the adapter keeps it OUT of required (wqp bbox, nldi
            # seed_point / comid). Default preserves the wave-2 required quirk.
            opt_ann = (ann | None) if (pspec.default is None and getattr(pspec, "schema_optional", False)) else ann
            annotations[pname] = opt_ann
            optional.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=pspec.default,
                    annotation=opt_ann,
                )
            )
    params = required + optional + [
        inspect.Parameter("_extra_ignored", inspect.Parameter.VAR_KEYWORD, annotation=Any)
    ]
    annotations["_extra_ignored"] = Any
    annotations["return"] = dict
    return inspect.Signature(params, return_annotation=dict), annotations


def _synthesize_doc(spec: SourceSpec) -> str:
    """The promoted tool's docstring: the twin's verbatim (indistinguishability)
    when the spec carries it, else a spec-derived surface from caveats + corpus."""
    if spec.docstring:
        return spec.docstring
    lines = [f"{spec.name} (spec-driven, source_class={spec.source_class})."]
    if spec.caveats:
        lines.append("Caveats: " + " ".join(spec.caveats))
    if spec.corpus:
        lines.append("Use this when: " + "; ".join(spec.corpus[:6]))
    return "\n".join(lines)


def _estimator_module(spec: SourceSpec) -> str:
    """Create a per-spec synthetic module carrying ``estimate_payload_mb``.

    The payload-warning seam resolves the estimator via
    ``getattr(import_module(entry.module), "estimate_payload_mb")``; giving each
    promoted tool its own module makes that resolution point at the spec's
    synthesized estimator (indistinguishable from a twin's module-level estimator).
    """
    mod_name = f"trid3nt_server.agent.tools.fetchers._router._promoted.{spec.name}"
    mod = types.ModuleType(mod_name)
    mod.estimate_payload_mb = router.synthesize_payload_estimator(spec)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


def _validate_hooks(spec: SourceSpec) -> None:
    """Assert every ``hooks.*`` name the spec declares resolves at load (ADR 0056).

    The hook contract is a name-string reference; a typo or a deleted hook must
    fail LOUDLY at registration, not silently at first call. Importing
    ``_router.hooks`` populates ``HOOK_REGISTRY`` via the hook modules' decorators.
    """
    from .hooks import HookResolutionError, has_hook

    if spec.hooks is not None:
        for point in (
            "build_request", "parse_response",
            "resolve_build", "resolve_parse", "next_page", "enrich_plan", "enrich_merge",
            "classify_status", "envelope",
            "delegate", "delegate_validate", "delegate_resolve",
            "record", "pre_resolve", "colormap",
        ):
            name = getattr(spec.hooks, point)
            if name and not has_hook(name):
                raise HookResolutionError(
                    f"spec {spec.name!r} references unknown hook {point}={name!r}"
                )

    # variant_by_emptiness (ADR 0081): the emptiness-switch hook name must resolve.
    vbe = spec.output.variant_by_emptiness
    if vbe and not has_hook(vbe):
        raise HookResolutionError(
            f"spec {spec.name!r} references unknown variant_by_emptiness hook {vbe!r}"
        )

    # record shape (ADR 0076): a record source MUST declare hooks.record (the router
    # has nothing else to shape the dict); a delegate_resolve pairs with a delegate.
    if spec.output.layer_type == "record":
        record_hook = spec.hooks.record if spec.hooks is not None else None
        if not record_hook:
            raise HookResolutionError(
                f"spec {spec.name!r}: output.layer_type=record requires hooks.record"
            )
    if spec.hooks is not None and spec.hooks.delegate_resolve and not spec.hooks.delegate:
        raise HookResolutionError(
            f"spec {spec.name!r}: hooks.delegate_resolve requires hooks.delegate"
        )

    # result_model (ADR 0073): if the spec names a LayerURI-subclass result model
    # it must resolve, and it pairs with an envelope hook (each is meaningless
    # without the other). Fail LOUD per-spec at load, never silently at first call.
    from trid3nt_contracts.execution import LAYER_RESULT_MODELS

    result_model = spec.output.result_model
    envelope = spec.hooks.envelope if spec.hooks is not None else None
    if result_model and result_model not in LAYER_RESULT_MODELS:
        raise HookResolutionError(
            f"spec {spec.name!r} names unknown result_model {result_model!r}; "
            f"known: {sorted(LAYER_RESULT_MODELS)}"
        )
    if bool(result_model) != bool(envelope):
        raise HookResolutionError(
            f"spec {spec.name!r}: output.result_model and hooks.envelope must be "
            f"declared together (got result_model={result_model!r}, envelope={envelope!r})"
        )


def register_spec(spec: SourceSpec) -> str:
    """Register the spec-driven surface as THE tool under ``spec.name`` (tier=general).

    Idempotent: a second registration of an already-present name is a no-op (it
    only re-records the spec). Returns the registered name.
    """
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    from trid3nt_server.agent import tools as _tools

    _validate_hooks(spec)
    name = spec.name
    if name in _tools.TOOL_REGISTRY:
        _SPEC_REGISTRY[name] = spec
        return name

    mod_name = _estimator_module(spec)
    sig, annotations = promoted_signature(spec)

    def _promoted(**kwargs: Any):
        return router.route(spec, kwargs)

    _promoted.__name__ = name
    _promoted.__qualname__ = name
    _promoted.__doc__ = _synthesize_doc(spec)
    _promoted.__module__ = mod_name
    _promoted.__signature__ = sig  # type: ignore[attr-defined]
    _promoted.__annotations__ = dict(annotations)

    # An ``internal_only`` spec (an absorbed seam resolved in-process, e.g.
    # fetch_copernicus_dem <- fetch_dem) registers tier="internal": registry-
    # resolvable but off BOTH the declarable pool and the search index, regardless
    # of any catalog arm. Otherwise the catalog-surfacing experiment tier applies:
    # under an arm flag the spec-served sources register tier="catalog" (excluded
    # from the default declarable pool, kept in the search index) instead of the
    # default tier="general". Unset -> "general", so the DEFAULT daemon surface is
    # unchanged.
    if spec.internal_only:
        tier = "internal"
    else:
        tier = "catalog" if catalog_arm() else "general"
    metadata = AtomicToolMetadata(
        name=name,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,
        # live-no-cache specs register uncacheable (the validator forbids
        # cacheable=True with ttl_class=live-no-cache); no-op for every cacheable spec.
        cacheable=spec.cache.ttl_class != "live-no-cache",
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        tier=tier,
        # ADR 0075: propagate output.auto_publish so the server dispatch wrapper
        # renders (or suppresses) the raster exactly as it did for the twin's
        # metadata flag. Default True = the terminal-product behaviour for every
        # prior spec (none set output.auto_publish); an INTERMEDIATE raster spec
        # (fetch_3dep_extra) sets it False to opt out of the automatic render.
        auto_publish=spec.output.auto_publish,
    )
    _tools.register_tool(metadata)(_promoted)
    _SPEC_REGISTRY[name] = spec
    logger.info(
        "router.registration: promoted spec-driven tool %s (source_class=%s)",
        name,
        spec.source_class,
    )
    return name


def register_specs_from_tree(root: Path | None = None) -> list[str]:
    """Walk ``fetchers/**/source.yaml`` and promote each spec to a registered tool.

    Returns the registered names. Called ONCE from ``agent/tools/__init__.py`` at
    import time (replacing the deleted twins' eager imports). A single spec that
    fails to register (e.g. an unresolved hook name) is logged and skipped so one
    broken co-located file never takes down startup -- mirroring the compose walk.
    """
    registered: list[str] = []
    for spec in compose_specs_from_tree(root).values():
        try:
            registered.append(register_spec(spec))
        except Exception:  # noqa: BLE001 -- one bad spec must not brick the daemon
            logger.error("router.registration: failed to register spec %r", spec.name, exc_info=True)
    return registered


def registered_spec_names() -> set[str]:
    """Twin names now served by a promoted spec-driven tool."""
    return set(_SPEC_REGISTRY)


def get_spec(name: str) -> SourceSpec | None:
    """The promoted ``SourceSpec`` for ``name`` (None if not spec-served).

    The in-process seam for a consumer that needs a source's raw fetched bytes
    without the cache/publish round trip (region_choice's admin-boundary candidate
    build): resolve the spec, then run ``router.validate_params`` + the executor.
    """
    return _SPEC_REGISTRY.get(name)


def _param_schema_entry(pspec: Any) -> dict[str, Any]:
    """The typed param schema for one spec param (card projection, Design 1)."""
    entry: dict[str, Any] = {"type": pspec.type, "required": bool(pspec.required)}
    if pspec.default is not None:
        entry["default"] = pspec.default
    if getattr(pspec, "values", None):
        entry["values"] = list(pspec.values)
    if getattr(pspec, "min", None) is not None:
        entry["min"] = pspec.min
    if getattr(pspec, "max", None) is not None:
        entry["max"] = pspec.max
    return entry


def spec_card(spec: SourceSpec, relevance_score: float | None = None) -> dict[str, Any]:
    """Project a ``SourceSpec`` into a Design-1 catalog CARD.

    Carries the FULL untruncated docstring (NOT clipped at the provider ~1000-char
    tool limit), the typed param schema derived from ``spec.params``, and the
    honesty context (gates / caveats / fallback) -- the model's ONLY view of
    per-source detail in Design 1.
    """
    card: dict[str, Any] = {
        "name": spec.name,
        "source_class": spec.source_class,
        "docstring": _synthesize_doc(spec),
        "params": {pn: _param_schema_entry(ps) for pn, ps in spec.params.items()},
        "gates": spec.gates.model_dump(mode="json") if spec.gates is not None else {},
        "caveats": list(spec.caveats),
        "fallback": list(spec.fallback),
    }
    if relevance_score is not None:
        card["relevance_score"] = float(relevance_score)
    return card


def search_spec_cards(topic: str, k: int = 10) -> list[dict[str, Any]]:
    """Rank spec-served source CARDS for a free-text topic (Design 1 search path).

    Ranks through the SAME BM25/dense retrieval index Design 2's discovery uses
    (ranking parity), then keeps only the spec-served sources and projects each to
    a card. Returns ``[]`` on a cold index (caller fails open / escalates).
    """
    from trid3nt_server.agent.tools.search.tool_retrieval import (
        MAX_K,
        retrieve_ranked_tools,
    )

    cards: list[dict[str, Any]] = []
    for name, score in retrieve_ranked_tools(topic, MAX_K):
        spec = _SPEC_REGISTRY.get(name)
        if spec is not None:
            cards.append(spec_card(spec, score))
        if len(cards) >= k:
            break
    return cards


def clear_specs_for_tests() -> None:
    """Drop all recorded specs (tests only). Does NOT unregister the tools from
    TOOL_REGISTRY -- pair with ``clear_registry_for_tests`` when needed."""
    _SPEC_REGISTRY.clear()
