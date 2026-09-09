"""Every HyRiver call rides here: the retry, the status filter, and the error the
library hands back as data.

HyRiver's HTTP layer (``async_retriever``) has no retry, no status filter and no
``Retry-After`` read, and a 4xx whose body parses as JSON is RETURNED AS A VALUE -
so an upstream refusal would otherwise reach the router as an answer and become an
honest-looking empty layer. :func:`hyriver_call` restores the norm for pygeohydro
and pynhd alike: an error document is raised carrying the upstream text verbatim,
429 and 5xx back off and retry, and a ``Retry-After`` is obeyed wherever the
provider puts one within reach, and a connection that never produced a response is
retried the way ``transport/client.py`` retries one.

WITHIN REACH is measured, and it is the body only: the library reads the response
and discards the headers, so a status integer and a ``Retry-After`` are honored
exactly when the service repeats them in the payload - RFC 7807's ``status``, an
ESRI ``error.code``, the status an ArcGIS error PAGE prints beside ``Code:``. A
body that names no status is refused once rather than retried blind, EXCEPT when
there was no response at all: a reset or timed-out connection is retryable on its
exception class alone, which is the one signal the library does hand over intact.

CACHE. The library keeps its own SQLite response cache, a second cache under the
router's tier. Its expiry is pinned to the SHORTEST router TTL window so it can
never hand a stale body to a fresh router key, and both files are written under the
runs dir rather than the process's working directory.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any, Callable, TypeVar

import aiohttp
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_upstream_error

__all__ = ["hyriver_call", "configure_cache"]

T = TypeVar("T")

#: The dynamic-1h class, the shortest router cache window: a library body may not
#: outlive the narrowest key the router would recompute.
_CACHE_EXPIRE_S = 3600

#: The retry policy is transport/client.py's, restated for a library that owns its
#: own HTTP: the same statuses, the same backoff shape, the same attempt budget.
_RETRY_STATUS = (429, 500, 502, 503, 504)
_ATTEMPTS = 5
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 20.0

#: A request that never became a response. The library discards the status but not
#: the exception class, so this is classified on type where a body cannot be read.
_NO_RESPONSE = (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError)

_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"']?\s*[:=]\s*[\"']?(\d+)", re.IGNORECASE)
#: A status the body names, whatever sits between the word and the number: a JSON
#: separator, or the markup an ArcGIS error page wraps it in.
_STATUS_RE = re.compile(r"\b(?:status|code)\D{0,12}?([45]\d\d)\b", re.IGNORECASE)


class _Refused(RuntimeError):
    """An upstream refusal, verbatim, plus the server's own wait when it sent one."""

    def __init__(self, text: str, wait: float | None = None) -> None:
        super().__init__(text)
        self.wait = wait


class _Throttled(_Refused):
    """A refusal worth retrying."""


def configure_cache() -> None:
    """Point HyRiver's two SQLite caches under the runs dir and pin their expiry."""
    root = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / "hyriver-cache"
    root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HYRIVER_CACHE_NAME", str(root / "aiohttp_cache.sqlite"))
    os.environ.setdefault("HYRIVER_CACHE_NAME_HTTP", str(root / "http_cache.sqlite"))
    os.environ.setdefault("HYRIVER_CACHE_EXPIRE", str(_CACHE_EXPIRE_S))


def _error_text(value: Any) -> str | None:
    """The upstream error text a RETURNED value carries, or None when it is data."""
    if isinstance(value, list) and len(value) == 1:
        return _error_text(value[0])
    if not isinstance(value, dict):
        return None
    err = value.get("error")
    if isinstance(err, dict) and "code" in err:
        return repr(value)
    status = value.get("status")
    if isinstance(status, int) and status >= 400 and "title" in value:
        return repr(value)
    return None


def _caused_by_no_response(exc: BaseException | None) -> bool:
    """True when the failure is a connection that never produced a response."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, _NO_RESPONSE):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _refusal(text: str, exc: BaseException | None = None) -> _Refused:
    """Classify one refusal: retryable on a retryable status, or on no response."""
    status = _STATUS_RE.search(text)
    after = _RETRY_AFTER_RE.search(text)
    wait = float(after.group(1)) if after else None
    retryable = _caused_by_no_response(exc) or (
        status is not None and int(status.group(1)) in _RETRY_STATUS
    )
    return (_Throttled if retryable else _Refused)(text, wait)


def _wait(state: RetryCallState) -> float:
    """The server's own number when it sent one, else backoff with jitter."""
    given = getattr(state.outcome.exception() if state.outcome else None, "wait", None)
    if given is not None:
        return min(given, _BACKOFF_CAP_S)
    delay = min(_BACKOFF_BASE_S * 2 ** (state.attempt_number - 1), _BACKOFF_CAP_S)
    return delay + random.uniform(0.0, _BACKOFF_BASE_S)


def hyriver_call(spec: SourceSpec, what: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call one pygeohydro / pynhd entry point under the upstream-provider norm.

    ``what`` names the call in the error a caller reads. Anything left after the
    retries is a typed upstream error whose message is the library's text verbatim.
    """
    configure_cache()
    try:
        for attempt in Retrying(
            retry=retry_if_exception_type(_Throttled),
            stop=stop_after_attempt(_ATTEMPTS),
            wait=_wait,
            reraise=True,
        ):
            with attempt:
                try:
                    out = fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 -- classified, never leaked raw
                    raise _refusal(f"{type(exc).__name__}: {exc}", exc) from exc
                text = _error_text(out)
                if text is not None:
                    raise _refusal(text)
                return out
    except _Refused as exc:
        raise router_upstream_error(spec.error_code_prefix, f"{what} failed: {exc}")
    raise router_upstream_error(spec.error_code_prefix, f"{what} failed: exhausted retries")
