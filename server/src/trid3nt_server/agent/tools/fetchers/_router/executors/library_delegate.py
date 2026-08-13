"""Generic library-delegate executor (ADR 0074, generalizing ADR 0040).

Some sources' maintained LIBRARY owns discovery AND the socket (pfdf's USGS TNM /
STATSGO readers, the HRRR-Zarr fsspec/xarray store) -- so the router cannot build
the request itself without re-implementing (and decaying against) the library. For
these the router DELEGATES the one network step to a registered hook that calls the
library and returns arrays/frames; the router keeps everything else (params, gates,
cache, payload gate, LayerURI, typed errors, publish). This is the ONE sanctioned
impurity in the hook contract -- a hook that owns a socket -- so it is CONSTRAINED:

  * a DECLARED timeout (``ingest.delegate.timeout_s``) is passed to the hook, which
    forwards it to the library call (never an unbounded hang);
  * the call is TELEMETRY-marked library-owned (the impurity boundary is logged);
  * ERROR MAPPING: the hook maps the library's own typed failures to the router's
    A.6 classes (input / empty / upstream) via the shared ``router_*_error``
    factories -- exactly as the twin did; any library exception the hook did NOT
    map is caught HERE as a retryable upstream error (verbatim reason), never
    leaking a raw library traceback (the upstream-provider-errors rule). There is
    no HTTP status for a library socket, so ``classify_status`` does not apply --
    the hook owns the taxonomy, this wrapper is the backstop.

The dataretrieval delegate (ADR 0040) is the vector precedent this generalizes; it
keeps its own module (``dataretrieval_delegate``) for its service-dispatch shape,
routed by the legacy ``ingest.delegate.library == 'dataretrieval'`` selector. A new
generic delegate declares ``hooks.delegate`` (+ optional ``hooks.delegate_validate``)
and returns features (vector) or ``(array, transform, crs)`` (raster).
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import FetchError
from ..errors import RouterError, router_upstream_error
from ..hooks import resolve_hook
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.library_delegate"
)

__all__ = ["invoke", "pre_validate", "resolve", "execute"]

#: Default declared timeout (seconds) when a spec omits ``ingest.delegate.timeout_s``.
_DEFAULT_TIMEOUT_S = 60.0


def _delegate_cfg(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("delegate") or {}


def pre_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Run the source-specific pre-cache input gate (``hooks.delegate_validate``).

    No-op when the spec declares no delegate-validate hook. Raises the twin's typed
    INPUT error BEFORE ``read_through`` (pre-cache / pre-network), offline-testable.
    """
    name = spec.hooks.delegate_validate if spec.hooks is not None else None
    if name:
        resolve_hook(name)(spec, params)


def resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Run the socketed pre-cache-key resolve (``hooks.delegate_resolve``, ADR 0076).

    The delegate sibling of the chained-resolution resolve phase, for a source whose
    cycle/key resolution walks a LIBRARY socket (HRRR-Zarr's s3fs cycle walk). Runs
    under the SAME constraints as :func:`invoke` (declared timeout, telemetry marks it
    library-owned, an unmapped library exception -> retryable upstream). Returns the
    dict the caller MERGES into params before ``read_through`` so the resolved cycle
    enters the cache key. No-op (returns ``{}``) when the spec declares no resolve hook.
    """
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
        # Any typed FetchError the hook already raised -- a RouterError's A.6
        # class OR a source-specific FetchError subclass carrying a pinned
        # error_code (fetch_dem's Dem*Error twins, ADR 0097) -- propagates
        # UNCHANGED so its exact typed code survives. Only a NON-FetchError
        # library exception hits the generic upstream backstop below.
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
    """Call the delegate hook under the router's constraints; return its result.

    The hook OWNS the library socket. This wrapper passes the declared timeout,
    marks the call library-owned in telemetry, and backstops any unmapped library
    exception as a retryable upstream error (verbatim). A ``RouterError`` the hook
    already raised (its twin-identical input / empty / upstream mapping) propagates
    unchanged.
    """
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
        # PASSTHROUGH (ADR 0097): the hook already raised a typed FetchError --
        # either a RouterError (its twin-identical A.6 input/empty/upstream
        # mapping) OR a source-specific FetchError subclass carrying a PINNED
        # error_code the router must not clobber (fetch_dem's DemPartialCoverageError
        # / DemPrimaryTimeoutError / DemAutoFallbackGateError / DemOutOfCoverageError,
        # whose DEM_* codes are test-pinned). Broadened from the original
        # ``except RouterError`` so those survive verbatim. A NON-FetchError library
        # exception still hits the generic upstream backstop below (unchanged for
        # every other delegate source: pfdf, dataretrieval, HRRR-Zarr).
        raise
    except Exception as exc:  # noqa: BLE001 -- backstop: never leak a raw library error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"{library} delegate call failed: {type(exc).__name__}: {exc}",
        )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """VECTOR delegate: call the hook for features and serialize to FGB bytes.

    The raster delegate does NOT route here -- a ``shape: raster-cog`` spec with
    ``ingest.access: library_delegate`` routes through ``raster_cog.execute`` (its
    ``fetch_source_array`` calls :func:`invoke` for the array), so the shared COG
    writer serializes the result. This body is the vector serialization seam.
    """
    features = invoke(spec, params)
    return features_to_fgb_bytes(features, spec, params)
