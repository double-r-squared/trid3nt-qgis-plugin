"""Promotion registration: a ``source.yaml`` spec becomes THE tool under its name.

Every consumer surface is built from the declaration: the docstring carried verbatim,
the signature synthesized from ``spec.params``, the callable seam a router closure,
and a per-spec synthetic module carrying ``estimate_payload_mb``."""

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
    "trid3nt_server.tools.fetchers._router.registration"
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

#: Catalog-surfacing experiment flag.
#: UNSET (default) -> the spec-served sources register tier="general" (ambient).
#: "1" (card-carried), "2" (discovery-expands-declaration) or "3" -> they register
#: tier="catalog": EXCLUDED from the default declarable pool but KEPT in the search
#: index. The flag is read at import so each arm runs in its OWN process with a
#: clean pool; DEFAULT behaviour is unchanged when it is unset.
CATALOG_ARM_ENV = "TRID3NT_CATALOG_ARM"


def catalog_arm() -> str | None:
    """The active catalog-surfacing arm ("1" / "2" / "3") or None when unset/invalid."""
    val = os.environ.get(CATALOG_ARM_ENV, "").strip()
    return val if val in ("1", "2", "3") else None


def _annotation_for(ptype: str) -> Any:
    """The schema-compatible Python annotation for a spec param type: bbox becomes
    ``list[float]``, int and float pass through, and every string-ish type
    (``iso_date`` / ``enum`` / ``str``) becomes ``str``."""
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
    """Synthesize the promoted tool's signature and annotations from ``spec.params``:
    required params first, defaulted next, then a VAR_KEYWORD absorber. A param is
    required-in-signature iff it is ``required`` and carries no ``default``."""
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
            # required-in-schema (min_voltage_kv / year_range / date rely on that).
            # A param declared ``T | None = None`` is NOT required; ``schema_optional``
            # reproduces that by annotating ``X | None``, so the adapter keeps it out
            # of required (wqp bbox, nldi seed_point / comid).
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
    # An animation_frames source returns an ordered list[LayerURI]; every other shape
    # returns a single LayerURI / record dict. The return annotation is cosmetic to
    # the inputSchema, which is built from params, but is kept honest anyway.
    ret = list if spec.shape == "animation_frames" else dict
    annotations["return"] = ret
    return inspect.Signature(params, return_annotation=ret), annotations


def _synthesize_doc(spec: SourceSpec) -> str:
    """The promoted tool's docstring: the spec's own when it carries one, else a
    surface derived from its caveats and corpus phrasings."""
    if spec.docstring:
        return spec.docstring
    lines = [f"{spec.name} (spec-driven, source_class={spec.source_class})."]
    if spec.caveats:
        lines.append("Caveats: " + " ".join(spec.caveats))
    if spec.corpus:
        lines.append("Use this when: " + "; ".join(spec.corpus[:6]))
    return "\n".join(lines)


def _estimator_module(spec: SourceSpec) -> str:
    """Create a per-spec synthetic module carrying ``estimate_payload_mb``. The
    payload-warning seam resolves the estimator off ``entry.module``, so each
    promoted tool needs a module of its own for that lookup to land."""
    mod_name = f"trid3nt_server.tools.fetchers._router._promoted.{spec.name}"
    mod = types.ModuleType(mod_name)
    mod.estimate_payload_mb = router.synthesize_payload_estimator(spec)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


def _validate_hooks(spec: SourceSpec) -> None:
    """Assert every ``hooks.*`` name the spec declares resolves at load. The hook
    contract is a name-string reference, so a typo or a deleted hook must fail
    LOUDLY at registration rather than silently at first call."""
    from .hooks import HookResolutionError, has_hook

    if spec.hooks is not None:
        for point in (
            "build_request", "parse_response",
            "resolve_build", "resolve_parse", "next_page", "enrich_plan", "enrich_merge",
            "classify_status", "envelope",
            "delegate", "delegate_validate", "delegate_resolve",
            "record", "pre_resolve", "colormap",
            "frames_plan", "frame_bytes",
        ):
            name = getattr(spec.hooks, point)
            if name and not has_hook(name):
                raise HookResolutionError(
                    f"spec {spec.name!r} references unknown hook {point}={name!r}"
                )

    # endpoint_fallback: every entry must name a key in this spec's OWN endpoints.
    # ``resolve_endpoints`` indexes ``spec.endpoints``, never the tool registry, so
    # an entry naming a sibling TOOL silently resolves to nothing while the spec
    # card advertises it to the model as a fallback this source has. A promise no
    # code path can keep fails at load instead of shipping as catalog text.
    for fb in spec.endpoint_fallback:
        if fb not in spec.endpoints:
            raise ValueError(
                f"spec {spec.name!r} declares endpoint_fallback {fb!r}, which names "
                f"no endpoint of this spec (has: {sorted(spec.endpoints)}). "
                "endpoint_fallback is SAME-DATA mirrors of this source only; a "
                "CROSS-DATASET alternative belongs on a declared fallback ladder "
                "(trid3nt_server.fallbacks), which gates and stamps it."
            )

    # variant_by_emptiness: the emptiness-switch hook name must resolve.
    vbe = spec.output.variant_by_emptiness
    if vbe and not has_hook(vbe):
        raise HookResolutionError(
            f"spec {spec.name!r} references unknown variant_by_emptiness hook {vbe!r}"
        )

    # record shape: a record source MUST declare hooks.record (the router
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

    # animation_frames shape: a frames-list source MUST declare both
    # frames_plan (pre-loop resolve) and frame_bytes (per-frame COG builder); the
    # executor has nothing else to resolve the frame set / build a frame.
    if spec.shape == "animation_frames":
        fp = spec.hooks.frames_plan if spec.hooks is not None else None
        fb = spec.hooks.frame_bytes if spec.hooks is not None else None
        if not (fp and fb):
            raise HookResolutionError(
                f"spec {spec.name!r}: shape=animation_frames requires "
                f"hooks.frames_plan + hooks.frame_bytes"
            )

    # result_model: if the spec names a LayerURI-subclass result model
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

    # provenance channel: the fetch-time provenance is delivered to the
    # envelope hook, so a spec that declares output.provenance MUST declare an
    # envelope hook to consume it (else the recorded dict has nowhere to land).
    if spec.output.provenance and not envelope:
        raise HookResolutionError(
            f"spec {spec.name!r}: output.provenance requires hooks.envelope "
            "(the provenance dict is delivered to the envelope hook)"
        )


def register_spec(spec: SourceSpec) -> str:
    """Register the spec-driven surface as THE tool under ``spec.name`` and return
    that name. Idempotent: a second registration of a present name only re-records
    the spec."""
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    from trid3nt_server import tools as _tools

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
        # DATA-native resolution declarations ride from the spec onto the
        # metadata so the gate card can quote them (two-layer truth: data facts here).
        resolution_specs=spec.resolution_declarations,
        # The declared confirm gate: a heavy fetcher's resolution gate rides onto the
        # metadata, so the server gate engine reads membership here.
        gate_spec=router._gate_spec_for_source(spec),
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
    """Walk ``fetchers/**/source.yaml``, promote each spec, and return the registered
    names. A spec that fails to register -- an unresolved hook name, say -- is logged
    and skipped, so one broken co-located file never takes down startup."""
    registered: list[str] = []
    for spec in compose_specs_from_tree(root).values():
        try:
            registered.append(register_spec(spec))
        except Exception:  # noqa: BLE001 -- one bad spec must not brick the daemon
            logger.error("router.registration: failed to register spec %r", spec.name, exc_info=True)
    return registered


def registered_spec_names() -> set[str]:
    """The names now served by a promoted spec-driven tool."""
    return set(_SPEC_REGISTRY)


def get_spec(name: str) -> SourceSpec | None:
    """The promoted ``SourceSpec`` for ``name``, or None when it is not spec-served.
    The in-process seam for a consumer that needs a source's raw bytes without the
    cache and publish round trip: resolve, validate params, run the executor."""
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
    """Project a ``SourceSpec`` into a catalog CARD carrying the FULL untruncated
    docstring (never clipped at the provider tool-description limit), the typed param
    schema, and the honesty context of gates, caveats and fallback."""
    card: dict[str, Any] = {
        "name": spec.name,
        "source_class": spec.source_class,
        "docstring": _synthesize_doc(spec),
        "params": {pn: _param_schema_entry(ps) for pn, ps in spec.params.items()},
        "gates": spec.gates.model_dump(mode="json") if spec.gates is not None else {},
        "caveats": list(spec.caveats),
        "endpoint_fallback": list(spec.endpoint_fallback),
    }
    if relevance_score is not None:
        card["relevance_score"] = float(relevance_score)
    return card


def search_spec_cards(topic: str, k: int = 10) -> list[dict[str, Any]]:
    """Rank spec-served source CARDS for a free-text topic through the same retrieval
    index discovery uses, keeping only spec-served sources. Returns ``[]`` on a cold
    index, for the caller to fail open on."""
    from trid3nt_server.tools.search.tool_retrieval import (
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
