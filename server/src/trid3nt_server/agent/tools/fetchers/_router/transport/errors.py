"""Transport-layer typed errors for the remote-FILE reader.

The transport owns every socket and every HTTP error for remote raster reads.
It raises these structured exceptions carrying a status INT + verbatim body +
retryable class; the raster_cog executor maps them into the router's A.6 error
frame (``router_empty_error`` / ``router_upstream_error``) at raise time, stamping
the spec's ``error_code_prefix``. Keeping status/body structured here is exactly
what the ingest-transport decision requires: a transport that hides the status
strips the router's ability to classify retryability (decision doc sec 7).
"""

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
    """Base for transport failures. Subclasses OSError so a raise inside a GDAL

    C read frame degrades to an IO error GDAL can propagate; the caller recovers
    the structured original via the opener's recorded-error bridge. Carries the
    HTTP ``status`` (int or None), verbatim ``body`` text, and a ``retryable`` class.

    ``actionability`` (item 3, observability/retention batch): closed
    ``{"agent", "user", "operator"}`` -- the routing hint
    ``agent.gates.actionability.classify_actionability`` reads FIRST before
    falling back to heuristics. Default "agent" (upstream 4xx/429/5xx/timeout
    -- unchanged behavior: rich verbatim function_response, the model retries
    or narrates). ``TransportAuthError`` overrides to "user" (missing-
    credential/auth-config).
    """

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
    """A range fetch returned fewer bytes than requested (block-completeness

    assertion failure). Makes silent truncation structurally impossible -- a short
    read is a typed, retryable upstream error, never a partial block."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message, status=status, body=body, retryable=True)


def classify_status(status: int, body: str | None, url: str) -> TransportError:
    """Map an HTTP error status + verbatim body to a typed transport error.

    404 or a ``NoSuchKey`` body -> not-found; 403 or ``AccessDenied`` -> auth;
    429/5xx -> retryable upstream; anything else >= 400 -> upstream. The S3 XML
    ``<Code>...</Code>`` body distinguishes NoSuchKey from AccessDenied even when
    the numeric status alone is ambiguous -- extracted via the shared
    ``classify_response`` shape classifier (item 4 of the observability/
    retention batch); the raw-substring check stays as a belt-and-suspenders
    fallback for a body the classifier doesn't recognize as S3 XML, so this
    migration is additive/behavior-identical, never narrower than before.
    """
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
