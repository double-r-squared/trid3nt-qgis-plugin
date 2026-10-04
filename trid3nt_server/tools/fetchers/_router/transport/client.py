"""Pooled httpx client and the ONE retry authority for every remote read.

One process-wide client reuses connections across every read and every parallel range
frame, and a library that owns its own socket borrows :func:`retried`. Retry
lives here and nowhere else; a caller whose retries were already spent
elsewhere uses the un-retried :func:`get_once`."""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from typing import Any, Callable, TypeVar

import httpx

from .errors import TransportUpstreamError, classify_status

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.transport.client"
)

__all__ = [
    "get_client", "get_bytes", "get_once", "post_bytes",
    "retried", "MAX_RETRIES", "RETRYABLE_STATUS",
]

MAX_RETRIES = 4
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0
_DEFAULT_TIMEOUT = 60.0

T = TypeVar("T")

_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> httpx.Client:
    """Return the process-wide pooled client (lazy, thread-safe singleton)."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    timeout=_DEFAULT_TIMEOUT,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=16, max_keepalive_connections=8
                    ),
                )
    return _CLIENT


def _retry_after_seconds(raw: str | float | None) -> float | None:
    """Parse a ``Retry-After`` hint (seconds, delta-seconds or HTTP-date) to seconds."""
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


def _sleep_backoff(attempt: int, retry_after: str | float | None) -> None:
    """Sleep before the next attempt: honor ``Retry-After`` else exp backoff+jitter."""
    hinted = _retry_after_seconds(retry_after)
    if hinted is not None:
        delay = min(hinted, _BACKOFF_CAP)
    else:
        delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
        delay += random.uniform(0.0, _BACKOFF_BASE)
    time.sleep(delay)


def retried(call: Callable[[], T], *, transient: Callable[[Exception], bool],
            label: str) -> T:
    """Run ``call`` under this module's attempt budget and backoff, for a library
    that owns its own socket. ``transient`` names which of its exceptions another
    attempt may cure; any other raises at once, and the last transient one raises
    unchanged once the budget is spent, so the caller maps it to its typed error.
    A ``retry_after`` the exception carries is the provider's wait, and is honoured."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == MAX_RETRIES or not transient(exc):
                raise
            logger.warning("transport.retried %s attempt=%d: %s: %s",
                           label, attempt, type(exc).__name__, exc)
            _sleep_backoff(attempt, getattr(exc, "retry_after", None))


def _send(client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    """One request under :func:`retried`: a network failure or a 429/5xx answer is
    another attempt, and exhaustion raises the provider's status and body verbatim.
    Any other answer, 4xx included, comes back for the caller to classify."""
    def call() -> httpx.Response:
        try:
            resp = client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransportUpstreamError(f"{method} network failure url={url}: {exc}") from exc
        if resp.status_code in RETRYABLE_STATUS:
            err = TransportUpstreamError(
                f"{method} exhausted retries at HTTP {resp.status_code} url={url}: "
                f"{resp.text[:400]!r}", status=resp.status_code, body=resp.text)
            err.retry_after = resp.headers.get("retry-after")  # type: ignore[attr-defined]
            raise err
        return resp

    return retried(call, transient=lambda e: isinstance(e, TransportUpstreamError),
                   label=f"{method} {url}")


def _body(resp: httpx.Response, url: str) -> tuple[bytes, str, str]:
    if resp.status_code >= 400:
        raise classify_status(resp.status_code, resp.text, url)
    return resp.content, resp.headers.get("content-type", ""), str(resp.url)


def get_bytes(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """GET a whole object; return ``(body, content_type, final_url)``. Redirects are
    followed; any 4xx classifies to a typed transport error at once."""
    return _body(_send(client, "GET", url, headers=headers, params=params), url)


def get_once(client: httpx.Client, url: str, *, headers: dict[str, str] | None = None
             ) -> tuple[bytes, int]:
    """ONE GET, no retry, no typed error: the body and status exactly as answered, for
    a caller that already spent its retries elsewhere. Any status (500 included)
    comes back to be read; only a network failure raises, with nothing retried."""
    try:
        resp = client.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("transport.get_once network error url=%s: %s", url, exc)
        raise TransportUpstreamError(f"GET network failure url={url}: {exc}") from exc
    return resp.content, resp.status_code


def post_bytes(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None, json_body: Any = None,
    data: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """POST a body and return ``(body, content_type, final_url)``: ``json_body`` sends
    JSON, ``data`` sends form-encoded. Retried like a GET, on the assumption every
    routed endpoint is a pure query with no side effect; a 4xx classifies at once."""
    return _body(_send(client, "POST", url, headers=headers, params=params,
                       json=json_body, data=data), url)
