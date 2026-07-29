"""Router typed-error hierarchy over the shared ``_fetch_common`` bases.

INDISTINGUISHABILITY (router-pilot-contract sec 0 + 2): a spec-driven source's
A.6 error frame must be byte-identical to the hand-written twin's. The twins
carry per-source subclasses (``GRIDMETUpstreamError`` etc.) whose ``error_code``
is ``<SOURCE>_UPSTREAM_ERROR`` / ``<SOURCE>_INPUT_ERROR`` / ``<SOURCE>_EMPTY``.
The router reproduces that shape from the spec's ``source_class`` at raise time
via ``router_error(...)``, so the surfaced ``error_code`` + ``retryable`` match
the twin without a per-source Python class.

These subclass the shared ``FetchError`` base in ``_fetch_common`` (the same
base the twins subclass), so the server-side A.6 mapping treats them identically.
"""

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
    """Base for router-driven fetch failures. Carries a dynamic ``error_code``."""

    error_code: str = "ROUTER_ERROR"
    retryable: bool = True


class RouterInputError(RouterError):
    """Bad inputs (malformed bbox, unknown enum, bad dates, gate rejection)."""

    error_code = "ROUTER_INPUT_ERROR"
    retryable = False


class RouterUpstreamError(RouterError):
    """Upstream endpoint open / read / parse / serialize failed (retryable)."""

    error_code = "ROUTER_UPSTREAM_ERROR"
    retryable = True


class RouterEmptyError(RouterError):
    """The request produced no finite data where an empty result is a typed error.

    (raster/station/tiled sources; vector sources emit an honest header-only FGB
    instead, never this error.)
    """

    error_code = "ROUTER_EMPTY"
    retryable = False


class RouterNotAvailableError(RouterError):
    """Requested extent/window falls outside the published source coverage."""

    error_code = "ROUTER_NOT_AVAILABLE"
    retryable = False


# --------------------------------------------------------------------------- #
# Factories that stamp the twin-identical per-source ``error_code``.
# --------------------------------------------------------------------------- #


def _stamp(cls: type[RouterError], code_prefix: str, suffix: str, message: str) -> RouterError:
    """Build a RouterError whose ``error_code`` is ``<PREFIX>_<SUFFIX>``.

    ``code_prefix`` is ``SourceSpec.error_code_prefix`` -- the twin's exact A.6
    token (e.g. ``"COOPS_TIDES"``, ``"GRIDMET"``), NOT necessarily the cache
    ``source_class``. e.g. code_prefix="GRIDMET" + "UPSTREAM_ERROR" ->
    "GRIDMET_UPSTREAM_ERROR" -- byte-identical to the ``fetch_gridmet`` twin's
    ``GRIDMETUpstreamError``; code_prefix="COOPS_TIDES" -> "COOPS_TIDES_EMPTY"
    even though the cache source_class is ``noaa_coops_tides`` (VERDICT #1).
    """
    exc = cls(message)
    # Instance-level override wins over the class attribute the server reads.
    exc.error_code = f"{code_prefix.upper()}_{suffix}"
    exc.retryable = cls.retryable
    return exc


def router_input_error(
    code_prefix: str, message: str, suffix: str = "INPUT_ERROR"
) -> RouterInputError:
    """Typed bad-input error. ``suffix`` defaults to the byte-identical
    ``INPUT_ERROR`` but a source may stamp its own (hifld/census ``INPUT_INVALID``,
    esri per-param ``BBOX_INVALID`` / ``YEAR_INVALID``)."""
    return _stamp(RouterInputError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_upstream_error(code_prefix: str, message: str) -> RouterUpstreamError:
    return _stamp(RouterUpstreamError, code_prefix, "UPSTREAM_ERROR", message)  # type: ignore[return-value]


def router_empty_error(
    code_prefix: str, message: str, suffix: str = "EMPTY"
) -> RouterEmptyError:
    """Typed empty/no-coverage error. ``suffix`` defaults to ``EMPTY`` but esri
    stamps ``NO_COVERAGE`` (ESRI_LANDCOVER_NO_COVERAGE)."""
    return _stamp(RouterEmptyError, code_prefix, suffix, message)  # type: ignore[return-value]


def router_not_available_error(code_prefix: str, message: str) -> RouterNotAvailableError:
    return _stamp(RouterNotAvailableError, code_prefix, "NOT_AVAILABLE", message)  # type: ignore[return-value]


def bbox_error_suffix(spec: Any) -> str:
    """The A.6 input-error suffix for a bbox-class failure (gate / malformed bbox).

    Returns the bbox param's ``error_suffix`` when it pins one (esri
    ``BBOX_INVALID``), else the spec-level ``input_error_suffix`` (gridmet/coops
    ``INPUT_ERROR``, hifld/census ``INPUT_INVALID``). Duck-typed over SourceSpec
    so ``errors`` stays import-cycle free.
    """
    for pspec in getattr(spec, "params", {}).values():
        if getattr(pspec, "type", None) == "bbox" and getattr(pspec, "error_suffix", None):
            return pspec.error_suffix
    return getattr(spec, "input_error_suffix", "INPUT_ERROR")
