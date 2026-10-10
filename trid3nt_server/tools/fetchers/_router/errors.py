"""Router typed-error hierarchy over the shared ``_fetch_common`` bases.

A row-driven source raises a per-source ``error_code`` -- ``<PREFIX>_UPSTREAM_ERROR``
/ ``<PREFIX>_INPUT_ERROR`` / ``<PREFIX>_EMPTY`` -- stamped at raise time from the
row, so no source needs a Python error class of its own."""

from __future__ import annotations

from typing import Any

from .._fetch_common import FetchError, UpstreamAPIError

__all__ = [
    "RouterError",
    "RouterInputError",
    "RouterUpstreamError",
    "RouterEmptyError",
    "RouterNotAvailableError",
    "router_input_error",
    "router_upstream_error",
    "router_empty_error",
    "router_not_available_error",
    "bbox_error_suffix",
]


class RouterError(FetchError):
    """Base for router-driven fetch failures, carrying a dynamic ``error_code``; ``actionability`` stays "agent"."""

    error_code: str = "ROUTER_ERROR"
    retryable: bool = True
    actionability: str = "agent"


class RouterInputError(RouterError):
    error_code = "ROUTER_INPUT_ERROR"
    retryable = False


class RouterUpstreamError(RouterError, UpstreamAPIError):
    error_code = "ROUTER_UPSTREAM_ERROR"
    retryable = True


class RouterEmptyError(RouterError):
    """The request produced no finite data where empty is a typed error (raster, station, tiled); a vector source emits a header-only FGB instead."""

    error_code = "ROUTER_EMPTY"
    retryable = False


class RouterNotAvailableError(RouterError):
    error_code = "ROUTER_NOT_AVAILABLE"
    retryable = False




def _stamp(cls: type[RouterError], code_prefix: str, suffix: str, message: str) -> RouterError:
    """Build a RouterError whose ``error_code`` is ``<PREFIX>_<SUFFIX>`` with ``SourceSpec.error_code_prefix``, which may differ from the cache ``source_class``."""
    exc = cls(message)
    exc.error_code = f"{code_prefix.upper()}_{suffix}"
    exc.retryable = cls.retryable
    return exc


def router_input_error(
    code_prefix: str, message: str, suffix: str = "INPUT_ERROR"
) -> RouterInputError:
    """Typed bad-input error; ``suffix`` defaults to ``INPUT_ERROR``."""
    return _stamp(RouterInputError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_upstream_error(
    code_prefix: str, message: str, retryable: bool = True
) -> RouterUpstreamError:
    """Typed upstream failure; ``retryable`` carries the TRANSPORT's verdict (a 404 is not worth a second call), else True."""
    exc = _stamp(RouterUpstreamError, code_prefix, "UPSTREAM_ERROR", message)
    exc.retryable = retryable
    return exc  # type: ignore[return-value]


def router_empty_error(
    code_prefix: str, message: str, suffix: str = "EMPTY"
) -> RouterEmptyError:
    """Typed empty/no-coverage error; ``suffix`` defaults to ``EMPTY``."""
    return _stamp(RouterEmptyError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_not_available_error(code_prefix: str, message: str) -> RouterNotAvailableError:
    return _stamp(RouterNotAvailableError, code_prefix, "NOT_AVAILABLE", message)  # type: ignore[return-value]


def bbox_error_suffix(spec: Any) -> str:
    """The input-error suffix for a bbox failure: the bbox param's ``error_suffix``, else the row's ``input_error_suffix``. Duck-typed to avoid an import cycle."""
    for pspec in getattr(spec, "params", {}).values():
        if getattr(pspec, "type", None) == "bbox" and getattr(pspec, "error_suffix", None):
            return pspec.error_suffix
    return getattr(spec, "input_error_suffix", "INPUT_ERROR")
