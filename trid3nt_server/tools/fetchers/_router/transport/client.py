"""Pooled httpx client and the ONE retry authority for remote-FILE range reads.

One process-wide client reuses connections across every read and every parallel range
frame. Retry lives here and nowhere else; a caller whose retries were already spent
elsewhere uses the un-retried :func:`get_once`."""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time

import httpx

from .errors import TransportUpstreamError, classify_status

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.transport.client"
)

__all__ = [
    "get_client", "range_get", "get_bytes", "get_once", "post_bytes", "head",
    "MAX_RETRIES",
]

MAX_RETRIES = 4
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 20.0
_DEFAULT_TIMEOUT = 60.0

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


def _retry_after_seconds(raw: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) to seconds."""
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


def _sleep_backoff(attempt: int, retry_after: str | None) -> None:
    """Sleep before the next attempt: honor ``Retry-After`` else exp backoff+jitter."""
    hinted = _retry_after_seconds(retry_after)
    if hinted is not None:
        delay = min(hinted, _BACKOFF_CAP)
    else:
        delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
        delay += random.uniform(0.0, _BACKOFF_BASE)
    time.sleep(delay)


def head(client: httpx.Client, url: str) -> httpx.Response:
    """HEAD with the retry authority (retry 429/5xx/timeout). Raises typed on
    exhaustion; a non-retryable 4xx is returned to the caller to classify."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.head(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning("transport.head network error url=%s attempt=%d: %s",
                           url, attempt, exc)
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, None)
                continue
            raise TransportUpstreamError(
                f"HEAD network failure url={url}: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("transport.head HTTP %d url=%s attempt=%d",
                           resp.status_code, url, attempt)
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, resp.headers.get("retry-after"))
                continue
            raise TransportUpstreamError(
                f"HEAD exhausted retries at HTTP {resp.status_code} url={url}",
                status=resp.status_code, body=None)
        return resp
    assert last_exc is not None
    raise TransportUpstreamError(f"HEAD failed url={url}: {last_exc}") from last_exc


def get_bytes(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """GET a whole object; return ``(body, content_type, final_url)``. Redirects are
    followed. 429/5xx/timeout/connection retry with backoff; any other 4xx classifies
    to a typed transport error immediately, and exhaustion surfaces verbatim."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.get(url, headers=headers, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning("transport.get_bytes network error url=%s attempt=%d: %s",
                           url, attempt, exc)
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, None)
                continue
            raise TransportUpstreamError(
                f"GET network failure url={url}: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("transport.get_bytes HTTP %d url=%s attempt=%d body=%r",
                           resp.status_code, url, attempt, resp.text[:400])
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, resp.headers.get("retry-after"))
                continue
            raise TransportUpstreamError(
                f"GET exhausted retries at HTTP {resp.status_code} url={url}: {resp.text[:400]!r}",
                status=resp.status_code, body=resp.text)
        if resp.status_code >= 400:
            raise classify_status(resp.status_code, resp.text, url)
        return resp.content, resp.headers.get("content-type", ""), str(resp.url)
    assert last_exc is not None
    raise TransportUpstreamError(f"GET failed url={url}: {last_exc}") from last_exc


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
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.post(url, headers=headers, params=params, json=json_body, data=data)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning("transport.post_bytes network error url=%s attempt=%d: %s",
                           url, attempt, exc)
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, None)
                continue
            raise TransportUpstreamError(
                f"POST network failure url={url}: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("transport.post_bytes HTTP %d url=%s attempt=%d body=%r",
                           resp.status_code, url, attempt, resp.text[:400])
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, resp.headers.get("retry-after"))
                continue
            raise TransportUpstreamError(
                f"POST exhausted retries at HTTP {resp.status_code} url={url}: {resp.text[:400]!r}",
                status=resp.status_code, body=resp.text)
        if resp.status_code >= 400:
            raise classify_status(resp.status_code, resp.text, url)
        return resp.content, resp.headers.get("content-type", ""), str(resp.url)
    assert last_exc is not None
    raise TransportUpstreamError(f"POST failed url={url}: {last_exc}") from last_exc


def range_get(client: httpx.Client, url: str, lo: int, hi: int) -> bytes:
    """GET ``bytes=lo-hi`` and return the body bytes. 429/5xx/timeout/connection retry
    with backoff; any other 4xx classifies to a typed transport error with no retry,
    and exhaustion surfaces the verbatim upstream status and body."""
    headers = {"Range": f"bytes={lo}-{hi}"}
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning("transport.range_get network error url=%s bytes=%d-%d "
                           "attempt=%d: %s", url, lo, hi, attempt, exc)
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, None)
                continue
            raise TransportUpstreamError(
                f"range GET network failure bytes={lo}-{hi} url={url}: {exc}"
            ) from exc
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("transport.range_get HTTP %d url=%s bytes=%d-%d attempt=%d "
                           "body=%r", resp.status_code, url, lo, hi, attempt,
                           resp.text[:400])
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt, resp.headers.get("retry-after"))
                continue
            raise TransportUpstreamError(
                f"range GET exhausted retries at HTTP {resp.status_code} "
                f"bytes={lo}-{hi} url={url}: {resp.text[:400]!r}",
                status=resp.status_code, body=resp.text)
        if resp.status_code >= 400:
            raise classify_status(resp.status_code, resp.text, url)
        return resp.content
    assert last_exc is not None
    raise TransportUpstreamError(
        f"range GET failed bytes={lo}-{hi} url={url}: {last_exc}") from last_exc
