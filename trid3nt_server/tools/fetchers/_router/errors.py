"""Router typed-error hierarchy over the shared ``_fetch_common`` bases.

A spec-driven source raises a per-source ``error_code`` -- ``<PREFIX>_UPSTREAM_ERROR``
/ ``<PREFIX>_INPUT_ERROR`` / ``<PREFIX>_EMPTY`` -- stamped at raise time from the
spec, so no source needs a Python error class of its own."""

from __future__ import annotations

from typing import Any

from .._fetch_common import FetchError

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
    """Base for router-driven fetch failures, carrying a dynamic ``error_code``.
    Every router error is the upstream 4xx-arg/429/5xx/timeout class, so
    ``actionability`` stays "agent"; no router error carries a credential concept."""

    error_code: str = "ROUTER_ERROR"
    retryable: bool = True
    actionability: str = "agent"


class RouterInputError(RouterError):
    """Bad inputs (malformed bbox, unknown enum, bad dates, gate rejection)."""

    error_code = "ROUTER_INPUT_ERROR"
    retryable = False


class RouterUpstreamError(RouterError):
    """Upstream endpoint open / read / parse / serialize failed (retryable)."""

    error_code = "ROUTER_UPSTREAM_ERROR"
    retryable = True


class RouterEmptyError(RouterError):
    """The request produced no finite data where empty is a typed error: raster,
    station and tiled sources. A vector source emits a header-only FGB instead."""

    error_code = "ROUTER_EMPTY"
    retryable = False


class RouterNotAvailableError(RouterError):
    """Requested extent/window falls outside the published source coverage."""

    error_code = "ROUTER_NOT_AVAILABLE"
    retryable = False


# --------------------------------------------------------------------------- #
# Factories that stamp the per-source ``error_code``.
# --------------------------------------------------------------------------- #


def _stamp(cls: type[RouterError], code_prefix: str, suffix: str, message: str) -> RouterError:
    """Build a RouterError whose ``error_code`` is ``<PREFIX>_<SUFFIX>``, the prefix
    being ``SourceSpec.error_code_prefix``: the surfaced token, NOT necessarily the
    cache ``source_class``, which diverges from it."""
    exc = cls(message)
    # Instance-level override wins over the class attribute the server reads.
    exc.error_code = f"{code_prefix.upper()}_{suffix}"
    exc.retryable = cls.retryable
    return exc


def router_input_error(
    code_prefix: str, message: str, suffix: str = "INPUT_ERROR"
) -> RouterInputError:
    """Typed bad-input error. ``suffix`` defaults to ``INPUT_ERROR``; a source may
    stamp its own (``INPUT_INVALID``, per-param ``BBOX_INVALID`` / ``YEAR_INVALID``)."""
    return _stamp(RouterInputError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_upstream_error(code_prefix: str, message: str) -> RouterUpstreamError:
    return _stamp(RouterUpstreamError, code_prefix, "UPSTREAM_ERROR", message)  # type: ignore[return-value]


def router_empty_error(
    code_prefix: str, message: str, suffix: str = "EMPTY"
) -> RouterEmptyError:
    """Typed empty/no-coverage error. ``suffix`` defaults to ``EMPTY``; a source may
    stamp its own, e.g. ``NO_COVERAGE``."""
    return _stamp(RouterEmptyError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_not_available_error(code_prefix: str, message: str) -> RouterNotAvailableError:
    return _stamp(RouterNotAvailableError, code_prefix, "NOT_AVAILABLE", message)  # type: ignore[return-value]


def bbox_error_suffix(spec: Any) -> str:
    """The input-error suffix for a bbox-class failure (gate or malformed bbox):
    the bbox param's ``error_suffix`` when it pins one, else the spec-level
    ``input_error_suffix``. Duck-typed over SourceSpec to stay import-cycle free."""
    for pspec in getattr(spec, "params", {}).values():
        if getattr(pspec, "type", None) == "bbox" and getattr(pspec, "error_suffix", None):
            return pspec.error_suffix
    return getattr(spec, "input_error_suffix", "INPUT_ERROR")
