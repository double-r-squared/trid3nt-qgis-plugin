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
]

#: twin_name -> SourceSpec for every promoted spec-driven tool (diagnostics/tests).
_SPEC_REGISTRY: dict[str, SourceSpec] = {}


def _annotation_for(ptype: str) -> Any:
    """The schema-compatible Python annotation for a spec param type.

    bbox -> ``list[float]`` (the adapter simplifies the twin's tuple annotation to
    the same array schema); int/float pass through; every string-ish type
    (``iso_date`` / ``enum`` / ``str``) -> ``str``.
    """
    if ptype == "bbox":
        return list[float]
    if ptype == "int":
        return int
    if ptype == "float":
        return float
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
        annotations[pname] = ann
        if pspec.required and pspec.default is None:
            required.append(
                inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
            )
        else:
            optional.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=pspec.default,
                    annotation=ann,
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


def register_spec(spec: SourceSpec) -> str:
    """Register the spec-driven surface as THE tool under ``spec.name`` (tier=general).

    Idempotent: a second registration of an already-present name is a no-op (it
    only re-records the spec). Returns the registered name.
    """
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    from trid3nt_server.agent import tools as _tools

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

    metadata = AtomicToolMetadata(
        name=name,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,
        cacheable=True,
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        # tier defaults to "general" -> the DEFAULT retrieval pool (not a template).
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
    import time (replacing the deleted twins' eager imports).
    """
    return [register_spec(spec) for spec in compose_specs_from_tree(root).values()]


def registered_spec_names() -> set[str]:
    """Twin names now served by a promoted spec-driven tool."""
    return set(_SPEC_REGISTRY)


def clear_specs_for_tests() -> None:
    """Drop all recorded specs (tests only). Does NOT unregister the tools from
    TOOL_REGISTRY -- pair with ``clear_registry_for_tests`` when needed."""
    _SPEC_REGISTRY.clear()
