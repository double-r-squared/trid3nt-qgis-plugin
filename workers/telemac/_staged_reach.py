"""Read the RIVER the run directory was staged with: centerline and banks.

The reach pipeline used to navigate NLDI, re-seed off two NHDPlus_HR flowline
queries and query NHDArea for bank polygons, all from inside the solver
container. Those are server tier now - a fetch changes if the box moves - and
what arrives instead is two GeoJSON files. What is left here is the part that
was ever the worker's business: turn the geometry it was handed into the arrays
the mesher builds on.

GeoJSON rather than FlatGeobuf on purpose: this image carries shapely but no
geopandas, and the staged text is the same shape the NLDI and ArcGIS responses
arrived in when the container fetched them itself, so the parsing below is the
parsing that was already here.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Basenames the server's manifest stages the reach geometry under.
STAGED_CENTERLINE_FILENAME: str = "river_centerline.geojson"
STAGED_BANKS_FILENAME: str = "river_banks.geojson"


class StagedReachMissingError(RuntimeError):
    """The run directory was not staged with geometry this build has to have."""


def _load(data_dir: str, filename: str, what: str) -> dict[str, Any]:
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        raise StagedReachMissingError(
            f"no staged {what} at {path}; the run directory was not staged with "
            "the river geometry this reach is meshed on. The worker fetches "
            "nothing of its own, so there is nothing to fall back to.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def staged_flowlines(data_dir: str) -> list[dict[str, Any]]:
    """The navigated NLDI flowline features the centerline is stitched from."""
    fc = _load(data_dir, STAGED_CENTERLINE_FILENAME, "river centerline")
    feats = [f for f in (fc.get("features") or []) if isinstance(f, dict)]
    if not feats:
        raise StagedReachMissingError(
            f"the staged river centerline in {data_dir} carries no features; "
            "there is no reach to mesh.")
    return feats


def staged_bank_polygons(data_dir: str) -> list[tuple[Any, list[Any]]] | None:
    """NHDArea water polygons as ``(exterior_ring, [hole_rings])`` lonlat arrays.

    ``None`` when the staged collection is EMPTY, which is the honest answer that
    no NHDArea polygon covers this reach - the caller raises its typed
    banks-unavailable gate on it. A MISSING file is a different thing and raises,
    because a staging failure must not read as a coverage hole.
    """
    import numpy as np

    fc = _load(data_dir, STAGED_BANKS_FILENAME, "river bank polygons")
    polys: list[tuple[Any, list[Any]]] = []
    for feat in fc.get("features") or []:
        geom = (feat or {}).get("geometry") or {}
        if geom.get("type") == "Polygon":
            rings = geom.get("coordinates") or []
            if rings:
                polys.append((np.asarray(rings[0], dtype=float),
                              [np.asarray(r, dtype=float) for r in rings[1:]]))
        elif geom.get("type") == "MultiPolygon":
            for rings in geom.get("coordinates") or []:
                if rings:
                    polys.append((np.asarray(rings[0], dtype=float),
                                  [np.asarray(r, dtype=float) for r in rings[1:]]))
    return polys or None
