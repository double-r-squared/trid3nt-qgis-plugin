"""The declared structure, as a footprint the mesher can remove water with.

A survey maps a breakwater as a CENTRELINE, and a centreline bounds no area:
subtracting it from the water domain removes nothing and the triangulation closes
straight over it, leaving conformal nodes on a barrier that is not there. The
footprint is that centreline given a DECLARED width, which is what
``set_obstacle`` can actually punch out.

Only this question needs it, so it lives beside the recipe that declares it
rather than in a shared tree.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.workflows.telemac.templates.agitation.barrier")

__all__ = ["barrier_footprint"]


async def barrier_footprint(*, structure: Any,
                            width_m: float) -> dict[str, Any]:
    """The declared structure -> the water-removing footprint, as GeoJSON.

    Returns the footprint inline rather than as a file, because the mesh op that
    reads it stages whatever it is handed into the rundir the container mounts,
    and a second object-store round trip for a shape this small would be a file
    nobody but the next call opens.
    """
    import asyncio

    return await asyncio.to_thread(_footprint, structure, float(width_m))


def _footprint(structure: Any, width_m: float) -> dict[str, Any]:
    """The buffered outline, in the 4326 the mesh recipe speaks."""
    import geopandas as gpd
    from shapely.geometry import LineString

    from trid3nt_server.workflows.shared.supplied_geometry import supplied_polylines

    lines = supplied_polylines(structure, label="structure",
                               code="ARTEMIS_STRUCTURE_INVALID")
    if not lines:
        raise ValueError(
            "artemis_harbor_agitation is asked WHETHER A STRUCTURE SHELTERS the "
            "water behind it, so the structure is the question and cannot be "
            "left out. Hand the slot a breakwater layer "
            "(fetch_osm_breakwaters) or a drawn line.")
    series = gpd.GeoSeries([LineString(line) for line in lines], crs=4326)
    metric = series.estimate_utm_crs()
    footprint = series.to_crs(metric).buffer(width_m / 2.0).union_all()
    doc = json.loads(
        gpd.GeoSeries([footprint], crs=metric).to_crs(4326).to_json())
    logger.info("agitation barrier: %d line(s) cut at %g m", len(lines), width_m)
    return doc
