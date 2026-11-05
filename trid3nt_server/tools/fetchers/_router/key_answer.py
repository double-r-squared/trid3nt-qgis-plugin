"""A provider's answer about a key, read in one place.

``key_error_from`` turns a fetch's 401/403 or ArcGIS
498/499 answer into the typed ask. No value of a key reaches a message.
"""

from __future__ import annotations

import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .._fetch_common import KeyAskError, KeyRejectedError, KeyRequiredError
from .transport import TransportError

__all__ = ["ARCGIS_TOKEN_CODES", "key_error_from"]

#: ArcGIS REST answers a token it needs or refuses under HTTP 200 with these error codes.
ARCGIS_TOKEN_CODES = (498, 499)

_NAMES_A_KEY = re.compile(r"api[ _-]?key|map[ _-]?key|subscription[ _-]?key|\btoken\b|apikey", re.IGNORECASE)
_LINK = re.compile(r"https://[^\s\"'<>)\]\\]{4,300}")


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen and len(seen) < 8:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def key_error_from(spec: SourceSpec, exc: BaseException) -> KeyAskError | None:
    """The typed ask a fetch failure is, or ``None`` when it is not one.

    A 401/403 whose body names a key, any 401/403 from a row that declares a credential, an ArcGIS 498/499 and a refusal a
    credentialed row spells ``*_AUTH_ERROR`` are the provider asking for a key: required when none is stored, rejected when one is."""
    from trid3nt_server.model.credentials.resolver import stored_value

    for link in _chain(exc):
        if isinstance(link, KeyAskError):
            return None
        credential = spec.auth.credential
        status = getattr(link, "status", None)
        refused_by_row = credential is not None and str(getattr(link, "error_code", "")).endswith("_AUTH_ERROR")
        if not refused_by_row:
            if not isinstance(link, TransportError) or status not in (401, 403, *ARCGIS_TOKEN_CODES):
                continue
            body = (link.body or "")[:2000]
            names_key = status in ARCGIS_TOKEN_CODES or (
                "AccessDenied" not in body and "AWS" not in body and _NAMES_A_KEY.search(body) is not None)
            if credential is None and not names_key:
                continue
        else:
            body = ""
        name = credential.name if credential else spec.name
        label = credential.label if credential else spec.name
        link_found = _LINK.search(body)
        stored = stored_value(name, credential.env_var if credential else None) is not None
        cls = KeyRejectedError if stored else KeyRequiredError
        return cls(spec.name, name, label, link_found.group(0) if link_found else None)
    return None
