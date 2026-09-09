"""Response-shape classifier: one upstream body, one of four shapes.

``data`` is valid and non-empty, ``empty`` is honestly zero-result and never an
error, ``error_envelope`` carries the upstream's verbatim message, ``unparseable`` is
anything else. Classifies the BODY only, and never raises or picks an error class."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

__all__ = ["ShapeKind", "ShapeVerdict", "classify_response"]

ShapeKind = Literal["data", "empty", "error_envelope", "unparseable"]

#: Sentinel distinguishing "no parseable JSON" from a body that parsed to the
#: JSON value ``null`` (Python ``None``) -- both would otherwise collapse to
#: the same falsy marker.
_UNSET = object()

_EXCERPT_LEN = 400

#: WMS error body: ``<ServiceException ...>message</ServiceException>``.
_WMS_SERVICE_EXCEPTION_RE = re.compile(
    r"<ServiceException[^>]*>(.*?)</ServiceException>", re.DOTALL | re.IGNORECASE
)
#: S3-style XML error: ``<Error><Code>NoSuchKey</Code><Message>...</Message></Error>``.
_S3_ERROR_CODE_RE = re.compile(r"<Code>([^<]*)</Code>", re.IGNORECASE)
_S3_ERROR_MESSAGE_RE = re.compile(r"<Message>([^<]*)</Message>", re.IGNORECASE)


@dataclass(frozen=True)
class ShapeVerdict:
    """The classification of one upstream response body; which fields carry a
    value depends on ``kind``."""

    kind: ShapeKind
    #: The FULL parsed JSON value for "data", "empty" and the ArcGIS envelope; None
    #: otherwise, so a caller can still read e.g. body["type"] off an envelope.
    body: Any = None
    #: The upstream's VERBATIM error text, str()-rendered, only for "error_envelope".
    error_message: str | None = None
    #: The RAW error value for the ArcGIS case (body["error"], often a dict carrying
    #: a code), so structured access needs no re-parse of error_message. None for the
    #: WMS/S3 XML cases: their structured signal rides on error_code instead.
    error_payload: Any = None
    #: A short upstream error CODE when the envelope carries one as a distinct token
    #: (S3 <Code>, e.g. "NoSuchKey" / "AccessDenied"); None otherwise.
    error_code: str | None = None
    #: Which recognized envelope matched: "arcgis" / "wms_service_exception" / "s3_xml".
    error_source: str | None = None
    #: First ~400 chars of the raw body text, set for "unparseable" so a typed error
    #: can quote what came back instead of a bare "non-JSON" message.
    excerpt: str | None = None


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    return raw


def _default_is_empty(body: Any) -> bool:
    """Honest-empty default: an empty list or dict, or a GeoJSON FeatureCollection
    whose ``features`` list is empty."""
    if isinstance(body, list):
        return len(body) == 0
    if isinstance(body, dict):
        feats = body.get("features")
        if isinstance(feats, list):
            return len(feats) == 0
        return len(body) == 0
    return False


def classify_response(
    raw: bytes | str | dict | list | None,
    *,
    is_empty: Callable[[Any], bool] | None = None,
) -> ShapeVerdict:
    """Classify one ALREADY-FETCHED body: bytes, decoded text, or a JSON value the
    caller parsed itself, which is taken directly and never re-serialized. Verdicts
    are checked in order: error envelope, then empty vs data, then unparseable."""
    text: str | None
    body: Any

    if isinstance(raw, (dict, list)):
        # Already parsed by the caller -- classify it directly, no re-parse.
        text = None
        body = raw
    elif raw is None:
        text = ""
        body = _UNSET
    else:
        text = _decode(raw)
        stripped = text.strip()
        body = _UNSET
        if stripped:
            try:
                body = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                body = _UNSET

    # 1a. ArcGIS/ESRI error envelope: {"error": ...} inside an otherwise-valid
    #     JSON object (a 200 that still carries a query/auth failure).
    if body is not _UNSET and isinstance(body, dict) and "error" in body:
        return ShapeVerdict(
            kind="error_envelope",
            body=body,
            error_message=str(body["error"]),
            error_payload=body["error"],
            error_source="arcgis",
        )

    # 1b/1c. XML-shaped error envelopes -- only relevant when the body did NOT
    # parse as JSON (a WMS/S3 error body is never valid JSON) and text was
    # actually supplied (not an already-parsed object).
    if body is _UNSET and text:
        wms_match = _WMS_SERVICE_EXCEPTION_RE.search(text)
        if wms_match:
            return ShapeVerdict(
                kind="error_envelope",
                error_message=wms_match.group(1).strip(),
                error_source="wms_service_exception",
            )
        if "<Error>" in text or "<Error " in text:
            code_match = _S3_ERROR_CODE_RE.search(text)
            msg_match = _S3_ERROR_MESSAGE_RE.search(text)
            code = code_match.group(1).strip() if code_match else None
            message = (
                msg_match.group(1).strip()
                if msg_match
                else (code or text[:_EXCERPT_LEN])
            )
            return ShapeVerdict(
                kind="error_envelope",
                error_message=message,
                error_code=code,
                error_source="s3_xml",
            )

    # 2. A parsed JSON value that isn't a recognized error envelope.
    if body is not _UNSET:
        empty = is_empty(body) if is_empty is not None else _default_is_empty(body)
        return ShapeVerdict(kind="empty" if empty else "data", body=body)

    # 3. Not JSON, no recognized error envelope.
    excerpt = (text or "")[:_EXCERPT_LEN]
    return ShapeVerdict(kind="unparseable", excerpt=excerpt)
