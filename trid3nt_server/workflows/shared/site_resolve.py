"""Resolve a place name to the AOI point every point-sampling derivation needs.

Declared workflows resolve the AOI through the DERIVED door, and several params
derive from the same point (aquifer K and porosity; the four two-zone column
properties). Each one calls this, so the geocode is memoized: one lookup per
place per process, not one per param.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from trid3nt_server.tools import TOOL_REGISTRY

logger = logging.getLogger("trid3nt_server.workflows.shared.site_resolve")

__all__ = ["SiteUnresolvedError", "geocode_point"]


class SiteUnresolvedError(RuntimeError):
    """A place name could not be turned into an AOI point.

    Every downstream derivation samples real data AT a point, so a failed
    geocode has to stop the run rather than let a physics value resolve
    somewhere else.
    """


@lru_cache(maxsize=64)
def _geocode(place: str) -> tuple[float, float]:
    entry = TOOL_REGISTRY.get("geocode_location")
    if entry is None:
        raise SiteUnresolvedError("geocode_location is not registered.")
    result = entry.fn(place)
    lat = (result.get("latitude") if isinstance(result, dict)
           else getattr(result, "latitude", None))
    lon = (result.get("longitude") if isinstance(result, dict)
           else getattr(result, "longitude", None))
    if lat is None or lon is None:
        raise SiteUnresolvedError(
            f"geocode_location({place!r}) returned no centroid lat/lon."
        )
    return float(lat), float(lon)


def geocode_point(place: str) -> tuple[float, float]:
    """``(lat, lon)`` for a place name. Raises :class:`SiteUnresolvedError`.

    Memoized on the place string: a geocode is a lookup of a fixed fact, and the
    derivations that share an AOI must share its coordinates exactly.
    """
    text = str(place or "").strip()
    if not text:
        raise SiteUnresolvedError("no place name to geocode.")
    return _geocode(text)
