"""Unified response-shape classifier (the NATE shape principle, one place).

Every upstream fetch response gets classified into exactly one of four shapes:

- ``"data"``            -- valid-shape, non-empty -- use it.
- ``"empty"``            -- valid-shape, honestly empty (a genuine
  zero-result JSON body/list) -- an "empty" result, never confused with an
  error.
- ``"error_envelope"``   -- a RECOGNIZED error envelope carrying the
  upstream's VERBATIM message: ArcGIS/ESRI ``{"error": ...}`` (a JSON object
  wrapping a 200), a WMS ``<ServiceException>`` block, or an S3-style
  ``<Error><Code>...</Code></Error>`` XML document.
- ``"unparseable"``      -- not data-shaped and not a recognized error
  envelope (garbage / an unexpected content-type / truncated body) -- the
  caller's typed error should quote a body excerpt, never fabricate meaning.

This module only classifies the BODY. HTTP-status classification (404/403/
429/5xx) is a separate, transport-layer concern
(``transport.errors.classify_status``); that function migrates its S3-XML
body-code extraction onto ``classify_response`` (item 4) while keeping its own
status-first branching and typed-exception return shape untouched.

Importable by BOTH the router executors (``executors/vector_fgb.py``) and
bespoke fetchers (``fetch_wdpa_protected_areas``, ``fetch_usace_dams``, ...) --
callers keep composing their OWN exception text/type from a ``ShapeVerdict``;
this module never raises and never picks an exception class, so a migration
onto it changes WHERE the shape logic lives, not what a caller does with it
(behavior-identical migrations are proven by each caller's existing tests).
"""

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
    """The classification of one upstream response body.

    - ``kind`` -- the four-way discriminator (see module docstring).
    - ``body`` -- the parsed JSON value (dict/list/other) for ``"data"`` /
      ``"empty"`` / the ArcGIS ``"error_envelope"`` case (the FULL parsed
      object, e.g. so a caller can still read ``body["type"]``); ``None``
      otherwise.
    - ``error_message`` -- the upstream's VERBATIM error text (already
      ``str()``-rendered), set only when ``kind == "error_envelope"``.
    - ``error_payload`` -- the RAW (unstringified) error value for the ArcGIS
      case (``body["error"]``, often a dict carrying a ``code``) so a caller
      needing structured access (e.g. an ESRI token-gate code) doesn't have
      to re-parse ``error_message``. ``None`` for the WMS/S3 XML cases (their
      structured signal, when present, is on ``error_code`` instead).
    - ``error_code`` -- a short upstream error CODE when the envelope carries
      one as a distinct token (S3 ``<Code>``, e.g. ``"NoSuchKey"`` /
      ``"AccessDenied"``); ``None`` otherwise.
    - ``error_source`` -- which recognized envelope matched: ``"arcgis"`` /
      ``"wms_service_exception"`` / ``"s3_xml"``, or ``None``.
    - ``excerpt`` -- the first ~400 chars of the raw body text, set for
      ``"unparseable"`` so a caller's typed error can quote what actually
      came back instead of a bare "non-JSON" message.
    """

    kind: ShapeKind
    body: Any = None
    error_message: str | None = None
    error_payload: Any = None
    error_code: str | None = None
    error_source: str | None = None
    excerpt: str | None = None


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    return raw


def _default_is_empty(body: Any) -> bool:
    """Honest-empty default: an empty list/dict, or a GeoJSON

    FeatureCollection whose ``features`` list is empty (the common vector
    fetch shape across this codebase's ArcGIS/GeoJSON sources).
    """
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
    """Classify one upstream response body per the NATE shape principle.

    ``raw`` is the ALREADY-FETCHED body: bytes, decoded text, OR a value a
    caller already parsed itself for another reason (e.g. ``httpx.Response.
    json()``) -- a dict/list/other JSON value is accepted directly and is
    never re-serialized/re-parsed. HTTP-status classification is a separate
    concern; this function only looks at what a response HANDED TO IT as a
    body actually contains.

    Verdicts, checked in this order:

    1. A recognized error envelope -- see ``ShapeVerdict`` -- ->
       ``"error_envelope"``.
    2. A parsed JSON value that is not an error envelope:
       ``is_empty(body)`` (default ``_default_is_empty``) -> ``"empty"``;
       else -> ``"data"``.
    3. Anything else -> ``"unparseable"``.
    """
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
