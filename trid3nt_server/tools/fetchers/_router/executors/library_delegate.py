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

#: Default declared timeout (seconds) when a spec omits ``ingest.delegate.timeout_s``.
_DEFAULT_TIMEOUT_S = 60.0


def _delegate_cfg(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("delegate") or {}


def pre_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Run the source-specific pre-cache input gate (``hooks.delegate_validate``), a
    no-op when none is declared. Raises the source's typed INPUT error BEFORE
    read_through, so a bad request never reaches the cache or the network."""
    name = spec.hooks.delegate_validate if spec.hooks is not None else None
    if name:
        resolve_hook(name)(spec, params)


def resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Run the socketed pre-cache-key resolve (``hooks.delegate_resolve``) under the
    same constraints as :func:`invoke`, returning the dict the caller merges into
    params so the resolved value enters the cache key. ``{}`` when undeclared."""
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
        # Any typed FetchError the hook already raised -- a RouterError, or a
        # source-specific FetchError subclass carrying a pinned error_code -- propagates
        # UNCHANGED so its exact typed code survives. Only a NON-FetchError library
        # exception hits the generic upstream backstop below.
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
    """Call the delegate hook under the router's constraints and return its result. The
    hook OWNS the socket; this wrapper passes the declared timeout, marks the call
    library-owned, and backstops an unmapped library exception verbatim."""
    if spec.hooks is None or not spec.hooks.delegate:
        raise router_upstream_error(
            spec.error_code_prefix, "library_delegate: spec declares no hooks.delegate"
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
        # PASSTHROUGH: the hook already raised a typed FetchError -- either a
        # RouterError with its input/empty/upstream mapping, or a source-specific
        # FetchError subclass carrying a PINNED error_code the router must not clobber
        # (DemPartialCoverageError / DemPrimaryTimeoutError / DemAutoFallbackGateError /
        # DemOutOfCoverageError, whose DEM_* codes are pinned). A NON-FetchError library
        # exception still hits the generic upstream backstop below.
        raise
    except Exception as exc:  # noqa: BLE001 -- backstop: never leak a raw library error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"{library} delegate call failed: {type(exc).__name__}: {exc}",
        )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """VECTOR delegate: call the hook for features and serialize to FGB bytes. A raster
    spec does NOT route here -- it reaches :func:`invoke` for its array through the
    raster executor, so the shared COG writer serializes that result."""
    features = invoke(spec, params)
    return features_to_fgb_bytes(features, spec, params)
