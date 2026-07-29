"""Pooled httpx client + the ONE retry authority for remote-FILE range reads.

A single process-wide ``httpx.Client`` reuses connections across every read and
every parallel range frame (amortizes the cold-TLS the bench measured at ~0.30s).
The retry authority lives here and nowhere else: backoff + ``Retry-After`` honored
on 429/5xx/timeout at BLOCK granularity, per the upstream-provider norm (log the
upstream error VERBATIM, retry with backoff, surface honestly on exhaustion).
GDAL-side retries stay off everywhere -- reads never touch ``/vsicurl/``.
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time

import httpx

from .errors import TransportUpstreamError, classify_status

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.transport.client"
)

__all__ = ["get_client", "range_get", "head", "MAX_RETRIES"]

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


def range_get(client: httpx.Client, url: str, lo: int, hi: int) -> bytes:
    """GET ``bytes=lo-hi`` with the retry authority; return the body bytes.

    Retries 429/5xx/timeout/connection with backoff + ``Retry-After``; a 404/403
    (or any other 4xx) is classified to a typed transport error immediately (no
    retry). On retry exhaustion the verbatim upstream status/body is surfaced.
    """
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
