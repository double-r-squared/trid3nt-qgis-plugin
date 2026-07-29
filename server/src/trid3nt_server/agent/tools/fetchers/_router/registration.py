"""Registration + fold-arm surfacing (contract sec 3).

Synthesizes ``AtomicToolMetadata`` + ``estimate_payload_mb`` + a callable virtual
tool from a :class:`SourceSpec`, registers it under an internal alias
(``fetch_X__spec``) with ``tier="template"`` so the EXISTING template-exclusion
filter keeps it out of the default pool with ZERO producer changes (baseline pool
byte-identical to today). Owns the env-gated pool-substitution map the three pool
producers consult in the fold arm.

Toggle (contract sec 3.3): ``TRID3NT_FETCHER_FOLD_ARM`` env var. UNSET = baseline
(tree exactly as today, virtual tools pool-excluded as ``tier=template``). SET =
fold arm (virtual surfaces UNDER the twin's name in the pool; twin pool-excluded).
The switch operates ONLY at the pool-producer seams, never on TOOL_REGISTRY
membership: flip the env, re-run the arm, flip back -> identical tree.

Safety: every ``apply_fold_substitution_*`` helper returns its input UNCHANGED
when the env is unset OR no specs are registered (the default) -- so the default
pool is provably unchanged when off.
"""

from __future__ import annotations

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
    "FOLD_ARM_ENV",
    "fold_arm_enabled",
    "virtual_alias",
    "register_spec",
    "register_specs_from_tree",
    "registered_spec_names",
    "substitution_map",
    "apply_fold_substitution_registry",
    "apply_fold_substitution_names",
    "resolve_fold_callable",
    "clear_specs_for_tests",
]

#: The single experiment toggle (contract sec 3.3).
FOLD_ARM_ENV = "TRID3NT_FETCHER_FOLD_ARM"

#: twin_name -> SourceSpec for every spec whose virtual tool is registered.
_SPEC_REGISTRY: dict[str, SourceSpec] = {}


def fold_arm_enabled() -> bool:
    """True iff the fold-arm env toggle is set (non-empty)."""
    return bool(os.environ.get(FOLD_ARM_ENV))


def virtual_alias(name: str) -> str:
    """The internal registry alias for a spec-driven virtual tool (open decision #2)."""
    return f"{name}__spec"


def _synthesize_doc(spec: SourceSpec, twin_doc: str | None) -> str:
    """The virtual tool's docstring: the twin's verbatim (indistinguishability)
    when available, else a spec-synthesized surface carrying the corpus phrasings."""
    if twin_doc:
        return twin_doc
    lines = [f"{spec.name} (spec-driven, source_class={spec.source_class})."]
    if spec.caveats:
        lines.append("Caveats: " + " ".join(spec.caveats))
    if spec.corpus:
        lines.append("Use this when: " + "; ".join(spec.corpus[:6]))
    return "\n".join(lines)


def _virtual_module(spec: SourceSpec) -> str:
    """Create a per-spec synthetic module carrying ``estimate_payload_mb``.

    The payload-warning seam resolves the estimator via
    ``getattr(import_module(entry.module), "estimate_payload_mb")``; giving each
    spec its own module makes that resolution point at the spec's synthesized
    estimator (indistinguishable from a twin's module-level estimator).
    """
    mod_name = f"trid3nt_server.agent.tools.fetchers._router._virtual.{spec.name}"
    mod = types.ModuleType(mod_name)
    mod.estimate_payload_mb = router.synthesize_payload_estimator(spec)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


def register_spec(spec: SourceSpec, *, twin_doc: str | None = None) -> str:
    """Register the spec-driven virtual tool under ``fetch_X__spec`` (tier=template).

    Returns the alias. Idempotent-ish: a second registration of the same spec
    name is a no-op (the alias already exists). The twin (``fetch_X``) is NOT
    touched -- both surfaces coexist in TOOL_REGISTRY per the HARD RULE.
    """
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    from trid3nt_server.agent import tools as _tools

    alias = virtual_alias(spec.name)
    if alias in _tools.TOOL_REGISTRY:
        _SPEC_REGISTRY[spec.name] = spec
        return alias

    # Copy the twin's docstring verbatim when the twin is registered (fair A/B).
    if twin_doc is None:
        twin_entry = _tools.TOOL_REGISTRY.get(spec.name)
        if twin_entry is not None:
            twin_doc = getattr(twin_entry.fn, "__doc__", None)

    mod_name = _virtual_module(spec)

    def _virtual(**kwargs: Any):
        return router.route(spec, kwargs)

    _virtual.__name__ = spec.name
    _virtual.__qualname__ = spec.name
    _virtual.__doc__ = _synthesize_doc(spec, twin_doc)
    _virtual.__module__ = mod_name

    metadata = AtomicToolMetadata(
        name=alias,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,
        cacheable=True,
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        tier="template",  # EXCLUDED from the default pool (baseline unchanged)
    )
    _tools.register_tool(metadata)(_virtual)
    _SPEC_REGISTRY[spec.name] = spec
    logger.info("router.registration: registered virtual tool %s (twin=%s)", alias, spec.name)
    return alias


def register_specs_from_tree(root: Path | None = None) -> list[str]:
    """Walk ``fetchers/**/source.yaml`` and register a virtual tool per spec.

    Returns the list of registered aliases. When no ``source.yaml`` exists (the
    current tree) this registers nothing -- zero impact on the baseline.
    """
    aliases: list[str] = []
    for spec in compose_specs_from_tree(root).values():
        aliases.append(register_spec(spec))
    return aliases


def registered_spec_names() -> set[str]:
    """Twin names that have a registered spec-driven virtual tool."""
    return set(_SPEC_REGISTRY)


def substitution_map() -> dict[str, str]:
    """``{twin_name: virtual_alias}`` for the pool producers (fold arm only)."""
    return {name: virtual_alias(name) for name in _SPEC_REGISTRY}


def _relabel_virtual(entry: Any, twin_name: str) -> Any:
    """Return a RegisteredTool copy of the virtual entry re-keyed to the twin
    name with ``tier="general"`` so the pool producers INCLUDE it under the
    twin's name (fold arm)."""
    from trid3nt_server.agent import tools as _tools

    relabeled_meta = entry.metadata.model_copy(update={"name": twin_name, "tier": "general"})
    return _tools.RegisteredTool(metadata=relabeled_meta, fn=entry.fn, module=entry.module)


def apply_fold_substitution_registry(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Transform a ``{name: RegisteredTool}`` pool snapshot for the fold arm.

    OFF (default) or no specs registered -> returns the SAME object unchanged
    (the default pool is provably unchanged when off). ON -> for each twin with a
    registered spec, replace ``snapshot[twin]`` with the relabeled virtual entry
    (spec-driven surfaces under the twin's name) and drop the ``__spec`` alias.
    """
    if not fold_arm_enabled():
        return snapshot
    subs = substitution_map()
    if not subs:
        return snapshot
    out = dict(snapshot)
    for twin, alias in subs.items():
        virt = out.get(alias)
        if virt is None:
            continue
        out[twin] = _relabel_virtual(virt, twin)  # twin pool entry -> spec-driven
        out.pop(alias, None)                       # alias never leaks into the pool
    return out


def apply_fold_substitution_names(names: set[str]) -> set[str]:
    """Name-set pool producers: the twin name is present in BOTH arms (the spec
    surfaces UNDER the twin's name), and the ``__spec`` alias is a ``tier=template``
    entry already excluded by the producer's own filter. So the visible NAME set
    is identical in both arms -- this is a documented no-op kept for symmetry."""
    return names


def resolve_fold_callable(tool_name: str):
    """Fold-arm dispatch helper (NOT wired into the hot path by B1).

    Returns the router closure for ``tool_name`` when the fold arm is ON and a
    spec is registered, else ``None`` (dispatch keeps the twin). Exposed for the
    fold-arm/replication lane; the replication harness calls ``router.route``
    directly, so B1 does not wire this into server.py dispatch.
    """
    if not fold_arm_enabled():
        return None
    spec = _SPEC_REGISTRY.get(tool_name)
    if spec is None:
        return None

    def _call(**kwargs: Any):
        return router.route(spec, kwargs)

    return _call


def clear_specs_for_tests() -> None:
    """Drop all registered specs (tests only). Does NOT unregister virtual tools
    from TOOL_REGISTRY -- pair with ``clear_registry_for_tests`` when needed."""
    _SPEC_REGISTRY.clear()
