"""The site the SWMM soil/aquifer templates sample their real properties at.

These decks are SCHEMATIC - one subcatchment, one node - so the site is not a
domain to fetch a raster over. It is the point at which the deck's soil
properties are sampled from real data, which is why it resolves through the
DERIVED door and lands on the form card beside the values it produced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_server.workflows.shared.site_resolve import (
    SiteUnresolvedError,
    geocode_point,
)

__all__ = ["resolve_site", "site_latlon"]

logger = logging.getLogger("trid3nt_server.workflows.swmm.steps.site")


def resolve_site(params: Any) -> tuple[float, float] | None:
    """``(lat, lon)`` from explicit coordinates or a place name; ``None`` if neither.

    Reads the sheet DIRECTLY rather than through the ``site_latlon`` row, so a
    derivation that needs the point never depends on which order the resolver
    happened to reach the two in.
    """
    lat, lon = params.get("lat"), params.get("lon")
    if lat is not None and lon is not None:
        return (float(lat), float(lon))
    place = params.get("location")
    if not (place and str(place).strip()):
        return None
    point = geocode_point(str(place))
    logger.info("swmm site %r geocoded to (%.5f, %.5f)", place, *point)
    return point


async def site_latlon(params: Any) -> tuple[float, float] | None:
    """The site row on the form card. ``None`` when the run carries no site.

    Absence is legal here: a caller who supplies the whole soil column explicitly
    is not modelling anywhere in particular, and the derivations that DO need a
    point refuse on their own behalf.
    """
    try:
        return await asyncio.to_thread(resolve_site, params)
    except SiteUnresolvedError as exc:
        logger.warning("swmm site could not be geocoded: %s", exc)
        return None
