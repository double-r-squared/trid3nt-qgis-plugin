"""The settled release point: where on the accepted mesh a source enters water.

A point is held where it was stated, never moved: it must lie inside the domain and the node the
engine reads it at - its nearest - must hold water at t0 off the rim, because the solver reads a
source on the rim as outside the domain. A point that misses refuses with the nearest node it would not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_server.inputs.run_notes import journal_note
from trid3nt_server.tools.mesh.shared.nodes import accepted_mesh_nodes
from trid3nt_server.inputs.point import Point, as_utm, publish_point, require_viable
from ..errors import TelemacError
from .accepted_mesh import mesh_artifact, mesh_zone, slug_of
from .initial_state import initial_state_of

__all__ = ["settle_release"]

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.release_point")


def _domain_m(artifact: Any, utm_epsg: int) -> Any:
    """The polygon the accepted mesh was cut from, in the zone's metres; a mesh cut from a bbox refuses."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.inputs.geometry import flatten_geometries, read_geometry_doc

    # The mesh records its build recipe, so containment runs against the mesh's own domain.
    recipe = ((getattr(artifact, "provenance", None) or {}).get("recipe") or {})
    extent = recipe.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        raise TelemacError(
            f"the accepted mesh for this run was cut from {extent!r} rather than "
            "from a domain polygon, so there is no mapped shape a source point "
            "could be inside of. A reach is meshed from the sectioned water "
            "polygon; solve on a mesh built that way.",
            error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    shapes = [_shape(g) for g in flatten_geometries(read_geometry_doc(extent))
              if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if not shapes:
        raise TelemacError(
            "the domain carries no polygon, so there is no shape a source point "
            "could be inside of.", error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    forward = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    return _transform(forward.transform, unary_union(shapes).buffer(0))


def _inside_water(cells: Any, wet: Any) -> Any:
    import numpy as np

    from trid3nt_server.tools.mesh.shared.nodes import interior_nodes

    held = np.asarray(wet, dtype=bool)
    return held & interior_nodes(cells, held.shape[0])


def _nearest(node_xy: Any) -> Any:
    import numpy as np

    nodes = np.asarray(node_xy, dtype=float)

    def resolve(xy: tuple[float, float]) -> int:
        return int(np.argmin(np.hypot(nodes[:, 0] - xy[0], nodes[:, 1] - xy[1])))

    return resolve


async def settle_release(
    *,
    point: Point,
    mesh: dict[str, Any],
    label: str,
    domain: Any = None,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Where a source enters the water: the stated point, checked against the accepted mesh, unmoved.

    The engine solves a source at its nearest node, so that node must be wet at t0 and off the rim."""
    from trid3nt_server.render.pipeline_emitter import current_emitter

    import numpy as np

    utm_epsg = mesh_zone(mesh)
    node_xy, cells, _bed, _lonlat = await asyncio.to_thread(accepted_mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(initial_state_of, continue_from, len(node_xy))
    # A source may enter where wet at t0 and off the edge; boundary conditions are imposed on the rim.
    wet = _inside_water(cells, initial_state["wet"])
    region = await asyncio.to_thread(_domain_m, mesh_artifact(mesh), utm_epsg)
    node = await asyncio.to_thread(
        require_viable, point, label=label, utm_epsg=utm_epsg, sites_xy=node_xy,
        viable=wet, resolve=_nearest(node_xy), region_m=region,
        missed=("where the engine reads it - its nearest mesh node - holds no "
                f"water at t0 off the rim ({initial_state['note']})"))
    at = as_utm(point, utm_epsg)
    away = float(np.hypot(float(node_xy[node][0]) - at[0], float(node_xy[node][1]) - at[1]))
    settled = (f"stated at ({point.lon:.5f}, {point.lat:.5f}); the engine reads it at "
               f"mesh node {node}, {away:.1f} m away, which holds water at t0 "
               f"({initial_state['note']})")
    logger.info("%s settled: %s", label, settled)
    journal_note(f"{label}: {settled}.")
    # The marker is emitted before the solve and carries the stated point.
    await publish_point(current_emitter(), point, label=label, basis="user",
                        context=slug_of(getattr(domain, "name", None)
                                      or str(mesh.get("mesh_id") or "domain")))
    return {"at": [round(at[0], 3), round(at[1], 3)],
            "lon": round(point.lon, 6), "lat": round(point.lat, 6),
            "name": point.name, "user_supplied": True, "note": settled}
