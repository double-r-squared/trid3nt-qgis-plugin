"""The AOI derivations every MODFLOW archetype template declares.

The archetypes model a point-centred domain: a place name or an explicit
``(lat, lon)``. Both resolve through the DERIVED door, so the point and the name
are rows on the form card - with the geocode already done - rather than values a
step resolves after the review.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_server.workflows.shared.site_resolve import (
    SiteUnresolvedError,
    geocode_point,
)

from .errors import ModflowAoiInputError

__all__ = ["aoi_latlon", "location_name"]

logger = logging.getLogger("trid3nt_server.workflows.modflow.steps.aoi")


async def aoi_latlon(params: Any) -> tuple[float, float]:
    """``(lat, lon)`` for the modeled domain, geocoded from ``location``.

    Only runs when the caller passed no explicit point - an ``aoi_latlon`` on the
    invocation wins at door 1 and this derivation never fires.
    """
    place = params.get("location")
    if not (place and str(place).strip()):
        raise ModflowAoiInputError(
            "supply exactly one of location (a place name, geocoded) or "
            "aoi_latlon (an explicit (lat, lon) point). Neither was given."
        )
    try:
        lat, lon = await asyncio.to_thread(geocode_point, str(place))
    except SiteUnresolvedError as exc:
        raise ModflowAoiInputError(str(exc)) from exc
    logger.info("modflow AOI %r geocoded to (%.5f, %.5f)", place, lat, lon)
    return (lat, lon)


async def location_name(params: Any) -> str:
    """What the run narrates the domain as: the asked-for place, else the point."""
    place = params.get("location")
    if place and str(place).strip():
        return str(place)
    # Attribute access, not .get: an ``aoi_latlon`` the sheet has not seated yet
    # raises ParamNotResolved, which is how the resolver's fixpoint knows to run
    # this derivation again AFTER the point derivation rather than reading a hole.
    point = params.aoi_latlon
    lat, lon = float(point[0]), float(point[1])
    return f"({lat:.4f}, {lon:.4f})"
