"""Where a DERIVED release is settled: a station along the reach, inside the mesh.

A run with no release point placed puts the source a declared fraction along the
centerline and walks it downstream to the first station the accepted mesh holds;
the domain a supplied point is tested against is the mesh's own record of what
it was cut from."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.release_point")

__all__ = ["derive_release_on_mesh", "domain_polygon_of"]


def domain_polygon_of(artifact: Any) -> Any:
    """The polygon the accepted mesh was cut from, or a typed refusal.

    A mesh cut from a bbox carries no polygon and is refused, never approximated."""
    # The mesh records the RECIPE it was built from, so the domain a containment
    # test runs against is the mesh's own statement of it rather than a second
    # resolution of the same question. A release point tested against four numbers
    # is a release point nobody tested.
    recipe = ((getattr(artifact, "provenance", None) or {}).get("recipe") or {})
    extent = recipe.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the accepted mesh for this run was cut from {extent!r} rather than "
            "from a domain polygon, so there is no mapped shape a release point "
            "could be inside of. A reach is meshed from the sectioned water "
            "polygon; solve on a mesh built that way.")
    return extent


def derive_release_on_mesh(*, centerline_utm: Any, mesh: Any,
                           fraction: float) -> tuple[tuple[float, float], str | None]:
    """A DERIVED release: ``fraction`` along the centerline, inside the mesh.

    Returns ``((lon, lat), note)`` in EPSG:4326, the note ``None`` if unmoved."""
    # The centerline is the whole navigated stretch; the accepted mesh is only the
    # part of it the mapped banks and the cleanup left, so a station on the line is
    # not a station in the domain. A source the solver cannot find an element for
    # stops the run at startup with nothing but "SOURCE POINT OUTSIDE DOMAIN", so
    # the station is walked DOWNSTREAM to the first one the triangulation holds and
    # the distance it travelled is said out loud.
    import numpy as np
    import shapely
    from pyproj import Transformer
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    display_uri = str(mesh.get("display_uri") or "")
    line = LineString(centerline_utm)
    frac = min(max(float(fraction), 0.0), 1.0)
    start = frac * line.length
    back = Transformer.from_crs(int(utm_epsg or 4326), 4326, always_xy=True)

    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(
        display_uri, utm_epsg=utm_epsg)
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    tree = shapely.STRtree(
        shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1)))
    # A cell-length stride: finer than that resolves nothing the mesh can hold,
    # coarser than that could step over a short meshed stretch entirely.
    step = max(float(line.length) / 2000.0, 1.0)
    walked = 0.0
    while start + walked <= line.length:
        here = line.interpolate(start + walked)
        if tree.query(here, predicate="intersects").size:
            lon, lat = back.transform(here.x, here.y)
            note = (None if walked <= 0.0 else
                    f"derived station moved {walked:.0f} m downstream to the "
                    "first point the accepted mesh holds")
            if note:
                logger.info("derived release walked %.0f m downstream into the "
                            "meshed reach", walked)
            return (float(lon), float(lat)), note
        walked += step
    raise TelemacDyeScenarioError(
        "TELEMAC_DYE_SCENARIO_ERROR",
        f"no point on the centerline at or below {frac:.0%} of its length lies "
        "inside the accepted mesh, so there is nowhere in the solved domain to "
        "put the release. Mesh more of the reach (a finer mesh_resolution_m or a "
        "supplied mesh) or place the release explicitly.")
