"""Promotion registration: a ``source.yaml`` row becomes THE tool under its name.

Every consumer surface is built from the declaration: the docstring carried verbatim,
the signature synthesized from ``spec.params``, the callable seam a router closure,
and a per-spec synthetic module carrying ``estimate_payload_mb``."""

from __future__ import annotations

import inspect
import logging
import sys
import types
from pathlib import Path
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from . import router
from .params import annotation_for
from .executors.qgis_provider import ACCESS as QGIS_PROVIDER_ACCESS
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
]

_SPEC_REGISTRY: dict[str, SourceSpec] = {}

def promoted_signature(spec: SourceSpec) -> tuple[inspect.Signature, dict[str, Any]]:
    """Synthesize the promoted signature from ``spec.params``: required params, defaulted, then a VAR_KEYWORD absorber."""
    required: list[inspect.Parameter] = []
    optional: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for pname, pspec in spec.params.items():
        ann = annotation_for(pspec)
        annotations[pname] = ann
        if pspec.required and pspec.default is None:
            required.append(
                inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
            )
        else:
            optional.append(
                inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=pspec.default, annotation=ann)
            )
    params = required + optional + [
        inspect.Parameter("_extra_ignored", inspect.Parameter.VAR_KEYWORD, annotation=Any)
    ]
    annotations["_extra_ignored"] = Any
    ret = list if spec.shape == "animation_frames" else dict
    annotations["return"] = ret
    return inspect.Signature(params, return_annotation=ret), annotations


def _synthesize_doc(spec: SourceSpec) -> str:
    if spec.docstring:
        return spec.docstring
    lines = [f"{spec.name} (row-driven, source_class={spec.source_class})."]
    if spec.caveats:
        lines.append("Caveats: " + " ".join(spec.caveats))
    if spec.corpus:
        lines.append("Use this when: " + "; ".join(spec.corpus[:6]))
    return "\n".join(lines)


def _estimator_module(spec: SourceSpec) -> str:
    """Create a per-spec synthetic module carrying ``estimate_payload_mb``; the payload seam resolves it off ``entry.module``."""
    mod_name = f"trid3nt_server.tools.fetchers._router._promoted.{spec.name}"
    mod = types.ModuleType(mod_name)
    mod.estimate_payload_mb = router.synthesize_payload_estimator(spec)  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return mod_name


def _validate_hooks(spec: SourceSpec) -> None:
    """Assert every ``hooks.*`` name the row declares resolves, failing LOUDLY at registration rather than at first call."""
    from .hooks import HookResolutionError, has_hook

    if spec.hooks is not None:
        # Every HookSpec field IS a hook point; a hand-kept list drifts.
        for point in type(spec.hooks).model_fields:
            name = getattr(spec.hooks, point)
            if name and not has_hook(name):
                raise HookResolutionError(
                    f"row {spec.name!r} references unknown hook {point}={name!r}"
                )

    # endpoint_fallback entries must name a key in this row's OWN endpoints: ``resolve_endpoints``
    # indexes ``spec.endpoints``, so a sibling TOOL name would silently resolve to nothing.
    for fb in spec.endpoint_fallback:
        if fb not in spec.endpoints:
            raise ValueError(
                f"row {spec.name!r} declares endpoint_fallback {fb!r}, which names "
                f"no endpoint of this row (has: {sorted(spec.endpoints)}). "
                "endpoint_fallback is SAME-DATA mirrors of this source only; a "
                "CROSS-DATASET alternative is a row of its own."
            )

    vbe = spec.output.variant_by_emptiness
    if vbe and not has_hook(vbe):
        raise HookResolutionError(
            f"row {spec.name!r} references unknown variant_by_emptiness hook {vbe!r}"
        )

    # A record source MUST declare hooks.record unless its executor shapes the dict itself
    # (a borrowed-provider overlay's record IS the session's answer); delegate_resolve pairs with a delegate.
    if spec.output.layer_type == "record":
        record_hook = spec.hooks.record if spec.hooks is not None else None
        executor_shapes_it = (
            (spec.ingest or {}).get("access") == QGIS_PROVIDER_ACCESS
        )
        if not record_hook and not executor_shapes_it:
            raise HookResolutionError(
                f"row {spec.name!r}: output.layer_type=record requires hooks.record"
            )
    if spec.hooks is not None and spec.hooks.delegate_resolve and not spec.hooks.delegate:
        raise HookResolutionError(
            f"row {spec.name!r}: hooks.delegate_resolve requires hooks.delegate"
        )

    # A frames-list source MUST declare both frames_plan and frame_bytes.
    if spec.shape == "animation_frames":
        fp = spec.hooks.frames_plan if spec.hooks is not None else None
        fb = spec.hooks.frame_bytes if spec.hooks is not None else None
        if not (fp and fb):
            raise HookResolutionError(
                f"row {spec.name!r}: shape=animation_frames requires "
                f"hooks.frames_plan + hooks.frame_bytes"
            )

    # result_model must resolve and pairs with an envelope hook; each is meaningless without the other.
    from trid3nt_contracts.execution import LAYER_RESULT_MODELS

    result_model = spec.output.result_model
    envelope = spec.hooks.envelope if spec.hooks is not None else None
    if result_model and result_model not in LAYER_RESULT_MODELS:
        raise HookResolutionError(
            f"row {spec.name!r} names unknown result_model {result_model!r}; "
            f"known: {sorted(LAYER_RESULT_MODELS)}"
        )
    if bool(result_model) != bool(envelope):
        raise HookResolutionError(
            f"row {spec.name!r}: output.result_model and hooks.envelope must be "
            f"declared together (got result_model={result_model!r}, envelope={envelope!r})"
        )

    # output.provenance MUST pair with an envelope hook, else the recorded dict has nowhere to land.
    if spec.output.provenance and not envelope:
        raise HookResolutionError(
            f"row {spec.name!r}: output.provenance requires hooks.envelope "
            "(the provenance dict is delivered to the envelope hook)"
        )


def register_spec(spec: SourceSpec) -> str:
    """Register the row-driven surface as THE tool under ``spec.name`` and return it; idempotent (a repeat re-records the row)."""
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

    metadata = router.synthesize_metadata(spec)
    _tools.register_tool(metadata)(_promoted)
    _SPEC_REGISTRY[name] = spec
    logger.info(
        "router.registration: promoted row-driven tool %s (source_class=%s)",
        name,
        spec.source_class,
    )
    return name


def register_specs_from_tree(root: Path | None = None) -> list[str]:
    """Walk ``fetchers/**/source.yaml``, promote each row and return the names; a failing row is logged and skipped."""
    registered: list[str] = []
    for spec in compose_specs_from_tree(root).values():
        try:
            registered.append(register_spec(spec))
        except Exception:  # noqa: BLE001 -- one bad row must not brick the daemon
            logger.error("router.registration: failed to register row %r", spec.name, exc_info=True)
    return registered


def registered_spec_names() -> set[str]:
    return set(_SPEC_REGISTRY)


def get_spec(name: str) -> SourceSpec | None:
    """The promoted ``SourceSpec`` for ``name``, or None when it is not row-served."""
    return _SPEC_REGISTRY.get(name)


def clear_specs_for_tests() -> None:
    """Drop all recorded rows (tests only); does NOT unregister from TOOL_REGISTRY."""
    _SPEC_REGISTRY.clear()
