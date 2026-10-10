"""Generic library-delegate executor.

Where a maintained library owns discovery AND the socket, the router delegates that
one network step to a registered hook and keeps everything else. It is the ONE
sanctioned impurity in the hook contract, so a declared timeout bounds it."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import FetchError
from ..errors import RouterError, router_upstream_error
from ..hooks import resolve_hook
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.library_delegate"
)

__all__ = ["invoke", "pre_validate", "resolve", "execute"]

_DEFAULT_TIMEOUT_S = 60.0


def _delegate_cfg(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("delegate") or {}


def pre_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Run the pre-cache input gate (``hooks.delegate_validate``), a no-op when undeclared; raises BEFORE read_through."""
    name = spec.hooks.delegate_validate if spec.hooks is not None else None
    if name:
        resolve_hook(name)(spec, params)


def resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Run the socketed pre-cache-key resolve (``hooks.delegate_resolve``) like :func:`invoke`; returns the dict to merge into params, ``{}`` if undeclared."""
    name = spec.hooks.delegate_resolve if spec.hooks is not None else None
    if not name:
        return {}
    cfg = _delegate_cfg(spec)
    library = str(cfg.get("library", "unknown"))
    try:
        timeout_s = float(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT_S
    hook = resolve_hook(name)
    logger.info(
        "library_delegate: LIBRARY-OWNED resolve library=%s source=%s timeout=%ss",
        library, spec.name, timeout_s,
    )
    try:
        merged = hook(spec, params, timeout_s=timeout_s)
    except FetchError:
        # A typed FetchError the hook raised (including a pinned error_code) propagates UNCHANGED;
        # only a NON-FetchError library exception hits the upstream backstop.
        raise
    except Exception as exc:  # noqa: BLE001 -- backstop: never leak a raw library error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"{library} delegate resolve failed: {type(exc).__name__}: {exc}",
        )
    if not isinstance(merged, dict):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"{library} delegate resolve returned {type(merged).__name__}, expected dict",
        )
    return merged


def invoke(spec: SourceSpec, params: dict[str, Any]) -> Any:
    """Call the delegate hook (which OWNS the socket) under the declared timeout, backstopping an unmapped library exception verbatim."""
    if spec.hooks is None or not spec.hooks.delegate:
        raise router_upstream_error(
            spec.error_code_prefix, "library_delegate: row declares no hooks.delegate"
        )
    cfg = _delegate_cfg(spec)
    library = str(cfg.get("library", "unknown"))
    try:
        timeout_s = float(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT_S
    hook = resolve_hook(spec.hooks.delegate)
    logger.info(
        "library_delegate: LIBRARY-OWNED call library=%s source=%s timeout=%ss",
        library, spec.name, timeout_s,
    )
    try:
        return hook(spec, params, timeout_s=timeout_s)
    except FetchError:
        # A typed FetchError from the hook propagates UNCHANGED so a pinned error_code (the DEM_* codes) survives.
        raise
    except Exception as exc:  # noqa: BLE001 -- backstop: never leak a raw library error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"{library} delegate call failed: {type(exc).__name__}: {exc}",
        )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """VECTOR delegate: call the hook for features and serialize to FGB; a raster row reaches :func:`invoke` through the raster executor."""
    features = invoke(spec, params)
    return features_to_fgb_bytes(features, spec, params)
