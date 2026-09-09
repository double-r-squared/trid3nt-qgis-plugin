"""Transport-layer typed errors for the remote-FILE reader.

Each carries the HTTP status as an INT, the verbatim body, and a retryable
class. A transport that hides the status strips the caller's ability to classify
retryability, so status and body stay structured all the way to the raise site."""

from __future__ import annotations

from ..shape_classifier import classify_response

__all__ = [
    "TransportError",
    "TransportNotFound",
    "TransportAuthError",
    "TransportUpstreamError",
    "TransportTruncatedError",
    "classify_status",
]


class TransportError(OSError):
    """Base for transport failures, carrying HTTP ``status``, verbatim ``body`` and
    a ``retryable`` class. Subclasses OSError so a raise inside a GDAL C read frame
    degrades to an IO error the opener's recorded-error bridge can recover."""

    #: Closed over {"agent", "user", "operator"}: who can act on this failure. The
    #: upstream 4xx/429/5xx/timeout class is "agent"; an auth subclass overrides.
    actionability: str = "agent"

    def __init__(self, message: str, *, status: int | None = None,
                 body: str | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class TransportNotFound(TransportError):
    """404 / S3 ``NoSuchKey`` -- the object does not exist (non-retryable)."""

    def __init__(self, message: str, *, status: int | None = 404, body: str | None = None):
        super().__init__(message, status=status, body=body, retryable=False)


class TransportAuthError(TransportError):
    """403 / S3 ``AccessDenied`` -- auth-class upstream failure (non-retryable)."""

    actionability: str = "user"

    def __init__(self, message: str, *, status: int | None = 403, body: str | None = None):
        super().__init__(message, status=status, body=body, retryable=False)


class TransportUpstreamError(TransportError):
    """429 / 5xx / timeout / connection failure -- retryable upstream error."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message, status=status, body=body, retryable=True)


class TransportTruncatedError(TransportError):
    """A range fetch returned fewer bytes than requested. A short read is a typed,
    retryable upstream error, never a silently accepted partial block."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message, status=status, body=body, retryable=True)


def classify_status(status: int, body: str | None, url: str) -> TransportError:
    """Map an HTTP error status and verbatim body to a typed transport error: 404 or
    a ``NoSuchKey`` body to not-found, 403 or ``AccessDenied`` to auth, 429/5xx to
    retryable upstream, anything else >= 400 to upstream."""
    snippet = (body or "")[:2000]
    verdict = classify_response(body) if body else None
    s3_code = (
        verdict.error_code
        if verdict is not None and verdict.error_source == "s3_xml"
        else None
    )
    code_hint = snippet
    if status == 404 or s3_code == "NoSuchKey" or "NoSuchKey" in code_hint:
        return TransportNotFound(
            f"object not found (HTTP {status}) url={url}: {snippet[:400]!r}",
            status=status, body=body,
        )
    if status == 403 or s3_code == "AccessDenied" or "AccessDenied" in code_hint:
        return TransportAuthError(
            f"access denied (HTTP {status}) url={url}: {snippet[:400]!r}",
            status=status, body=body,
        )
    if status == 429 or 500 <= status < 600:
        return TransportUpstreamError(
            f"upstream HTTP {status} url={url}: {snippet[:400]!r}",
            status=status, body=body,
        )
    return TransportUpstreamError(
        f"unexpected HTTP {status} url={url}: {snippet[:400]!r}",
        status=status, body=body,
    )
