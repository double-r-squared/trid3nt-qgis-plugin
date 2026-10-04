"""Every ``dataretrieval`` call rides here, under the shared transport's retry.

The agency-maintained client owns discovery and the socket, absorbing the NWIS to
Water Data OGC API migration; what outlasts the retries is the provider's own
message, typed."""

from __future__ import annotations

from typing import Any, Callable

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from ..transport.client import retried

__all__ = ["retrieve"]


def retrieve(spec: SourceSpec, call: Callable[..., Any], *, input_on_400: bool = True,
             **kwargs: Any) -> Any:
    """Call one ``dataretrieval`` function under the shared transport's retry: a
    failed connection (reset, timeout, TLS handshake), a 429 or a 5xx is tried again,
    any other status is not. What outlasts the retries is a typed upstream error with
    the provider's own message verbatim, which names its host; an HTTP 400 is the
    caller's bad request and a non-retryable input error when ``input_on_400``."""
    from dataretrieval.exceptions import DataRetrievalError, NetworkError, TransientError

    def transient(exc: Exception) -> bool:
        return isinstance(exc, (NetworkError, TransientError))

    try:
        return retried(lambda: call(**kwargs), transient=transient,
                       label=f"{spec.name} {getattr(call, '__name__', call)}")
    except DataRetrievalError as exc:
        prefix = spec.error_code_prefix
        if input_on_400 and getattr(exc, "status_code", None) == 400:
            raise router_input_error(prefix, f"upstream rejected the request (HTTP 400): {exc}",
                                     spec.input_error_suffix) from exc
        raise router_upstream_error(prefix, f"{type(exc).__name__}: {exc}",
                                    retryable=transient(exc)) from exc
