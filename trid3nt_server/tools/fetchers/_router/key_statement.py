"""What a provider states, machine-readably, about needing a key.

Each reader takes a body the provider published and answers ``True`` (it states a key is needed), ``False`` (it states it
is open) or ``None`` (it states nothing); a hostname is never evidence.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .key_answer import ARCGIS_TOKEN_CODES

__all__ = ["arcgis_states_key", "ogc_states_key", "openapi_states_key"]

_KEY_WORDS = re.compile(r"api[ _-]?key|\btoken\b|\blogin\b|\bcredential|authenticat|subscription|registration", re.IGNORECASE)
_NO_CONSTRAINT = {"none", "no constraints", "no restrictions", "n/a"}


def arcgis_states_key(body: bytes | str) -> bool | None:
    """An ArcGIS REST service description: its 498/499 token answer says a key is needed; a service document says open."""
    try:
        doc: Any = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    error = doc.get("error")
    if isinstance(error, dict):
        return True if error.get("code") in ARCGIS_TOKEN_CODES else None
    auth = doc.get("authInfo")
    if isinstance(auth, dict) and "isTokenBasedSecurity" in auth:
        return bool(auth["isTokenBasedSecurity"])
    return False if "currentVersion" in doc or "serviceDescription" in doc or "layers" in doc or "fields" in doc else None


def _secret_scheme_names(schemes: dict) -> set[str]:
    """The schemes that take a secret: a ``User-Agent`` header scheme names the client and takes none."""
    return {name for name, scheme in schemes.items() if isinstance(scheme, dict)
            and not (scheme.get("type") == "apiKey" and scheme.get("in") == "header"
                     and str(scheme.get("name", "")).lower() == "user-agent")}


def openapi_states_key(body: bytes | str) -> bool | None:
    """An OpenAPI document: a key is needed when every alternative of its ``security`` (or of every operation) names a secret scheme."""
    try:
        doc: Any = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not ("openapi" in doc or "swagger" in doc):
        return None
    schemes = (doc.get("components") or {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
    secret = _secret_scheme_names(schemes)

    def needs(alternatives: list) -> bool:
        return bool(alternatives) and all(any(name in secret for name in alt) for alt in alternatives)

    if doc.get("security") is not None:
        return needs(doc["security"])
    operations = [op for item in (doc.get("paths") or {}).values() if isinstance(item, dict)
                  for method, op in item.items() if method in {"get", "post", "put", "delete"} and isinstance(op, dict)]
    if operations and all(needs(op.get("security") or []) for op in operations):
        return True
    return False if not secret else None


def ogc_states_key(body: bytes | str) -> bool | None:
    """An OGC capabilities document: ``AccessConstraints`` of ``NONE`` says open; one naming a key, token or login says a key."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    found = re.search(r"<(?:\w+:)?AccessConstraints>(.*?)</(?:\w+:)?AccessConstraints>", text, re.IGNORECASE | re.DOTALL)
    if found is None:
        return None
    stated = re.sub(r"\s+", " ", found.group(1)).strip()
    if stated.lower() in _NO_CONSTRAINT:
        return False
    return True if _KEY_WORDS.search(stated) else None
