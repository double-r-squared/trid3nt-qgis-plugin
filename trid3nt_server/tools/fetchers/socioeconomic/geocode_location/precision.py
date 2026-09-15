"""The geocode PRECISION option: reject an answer too coarse to place a thing on.

A whole-state bbox has a centroid a hundred kilometres from anywhere the caller
meant, and a domain, a seed or a station pick placed on it is placed on the
wrong ground entirely. The two pure decisions a precise geocode needs live here:
whether an answer fell back to a state snap, and what locality a compound place
name carries to retry on.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["PRECISION_LOCALITY", "is_state_snap", "locality_tail"]

#: The one precision a caller can ask for today: an answer that pins a locality
#: rather than falling back to a whole state.
PRECISION_LOCALITY = "locality"

#: The words a compound place name hangs its locality off. "the Eel River near
#: Scotia" has no feature of its own; "Scotia" does.
_SEPARATORS = ("near", "at", "by", "outside", "in")


def is_state_snap(geo: Any) -> bool:
    """True when a geocode answer fell back to a WHOLE-STATE bbox.

    The fetcher states it on the answer - the source it served from, or the
    reason it degraded - so nothing here re-derives it from coordinates."""
    if not isinstance(geo, dict):
        return False
    return (geo.get("source") == "state-bbox-fallback"
            or geo.get("fallback_reason") is not None)


def locality_tail(query: str) -> str | None:
    """The locality a compound place name carries, or ``None``.

    What follows near/at/by/outside/in, when that is not the whole query."""
    for separator in _SEPARATORS:
        found = re.search(rf"\b{separator}\b(.+)$", str(query), flags=re.IGNORECASE)
        if found:
            tail = found.group(1).strip(" ,")
            if tail and tail.lower() != str(query).strip().lower():
                return tail
    return None
