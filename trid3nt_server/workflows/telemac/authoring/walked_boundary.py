"""The walked boundary of a harbour: which stretch of the outline is open water.

The walk over the mesh's connectivity, with the structure's segments and the basin's deepest
column measured off it, so the incident wave prescribes only where the sea reaches.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.runtime import journal_note
from ..errors import TelemacError
from .accepted_mesh import (
    HARBOUR_GEOMETRY,
    boundary_uri,
    geometry_uri,
    mesh_facts,
    mesh_nodes,
    refuse_a_sealed_domain,
    slug_of,
    topology_of,
)

__all__ = ["settle_harbour"]

def _harbour_mesh_missing(message: str) -> Exception:
    return TelemacError(message, error_code="ARTEMIS_MESH_NOT_ACCEPTED")


def _boundary_file(files: Mapping[str, Any], *,
                   missing: Callable[[str], Exception]) -> tuple[str, list[int]]:
    """The pair's ``.cli`` text and boundary nodes in rank order; the file from this IPOBO is the one record of the walk."""
    from trid3nt_server.tools.cache import read_object_bytes_s3

    uri = boundary_uri(files, missing=missing)
    text = (read_object_bytes_s3(uri).decode("utf-8") if uri.startswith("s3://")
            else Path(uri).read_text(encoding="utf-8"))
    rows: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append((int(parts[-1]), int(parts[-2])))
    if not rows:
        raise missing(f"the accepted mesh's boundary file {uri} holds no rows.")
    rows.sort()
    return text, [node - 1 for _rank, node in rows]


def _nodes_near(segments: Any, points_utm: Any, candidates: Sequence[int],
                tolerance_m: float) -> list[int]:
    import numpy as np

    segs = np.asarray(segments, dtype=float).reshape(-1, 4)
    picked = np.asarray(candidates, dtype=np.int64)
    if segs.shape[0] == 0 or picked.size == 0:
        return []
    px = points_utm[picked, 0][:, None]
    py = points_utm[picked, 1][:, None]
    x0, y0, x1, y1 = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    dx, dy = x1 - x0, y1 - y0
    length2 = np.maximum(dx * dx + dy * dy, 1e-12)
    t = np.clip(((px - x0) * dx + (py - y0) * dy) / length2, 0.0, 1.0)
    distance = np.hypot(px - (x0 + t * dx), py - (y0 + t * dy)).min(axis=1)
    return [int(n) for n in picked[distance <= float(tolerance_m)]]


def _settled_walk(walk: Sequence[int], structure: set[int], liquid: set[int]
                  ) -> tuple[list[int], list[int]]:
    """The two roles as runs of the boundary walk; the structure wins where they overlap."""
    order = [int(n) for n in walk]
    kind = ["structure" if n in structure else
            ("liquid" if n in liquid else "shore") for n in order]
    # A lone node between two of another kind is not a face (front2.f refuses a solid point between two liquid points and the reverse), so it takes its neighbours' role; iterated until stable.
    for _pass in range(len(order)):
        moved = False
        for i in range(1, len(kind) - 1):
            if kind[i - 1] == kind[i + 1] != kind[i]:
                kind[i] = kind[i - 1]
                moved = True
        if not moved:
            break
    return ([n for n, k in zip(order, kind) if k == "structure"],
            [n for n, k in zip(order, kind) if k == "liquid"])


def _segments_utm(polylines: Sequence[Any], utm_epsg: int) -> list[list[float]]:
    from pyproj import Transformer

    forward = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    out: list[list[float]] = []
    for line in polylines:
        points = [forward.transform(float(lon), float(lat)) for lon, lat in line]
        out += [[points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]]
                for i in range(len(points) - 1)]
    return out


async def settle_harbour(
    *,
    mesh: dict[str, Any],
    files: dict[str, Any],
    structure: Any = None,
    structure_width_m: float,
    wave_period_s: float,
    wave_height_m: float,
    wave_direction_deg: float,
    reflection_coef: float,
    result_basename: str,
    deck: str,
    open_depth_threshold_m: float,
) -> dict[str, Any]:
    """What the accepted harbour mesh measures -> what the harbour sheet reads.

    A mesh naming no liquid boundary refuses.
    """
    from trid3nt_server.inputs.shape import polylines as _lines, shape

    facts = mesh_facts(mesh, missing=_harbour_mesh_missing)
    utm_epsg = int(facts["utm_epsg"])
    topology = topology_of(files, missing=_harbour_mesh_missing)
    refuse_a_sealed_domain(topology, deck=deck,
                           open_depth_threshold_m=open_depth_threshold_m)
    open_nodes = [int(n) for n in topology["roles"]["open"]]
    cli_text, boundary_nodes = _boundary_file(files,
                                              missing=_harbour_mesh_missing)
    points_utm, node_bed = await asyncio.to_thread(
        mesh_nodes, mesh, missing=_harbour_mesh_missing)

    drawn = await asyncio.to_thread(
        shape, structure, label="structure", code="ARTEMIS_STRUCTURE_INVALID")
    polylines = (_lines(drawn, label="structure", code="ARTEMIS_STRUCTURE_INVALID")
                 if drawn is not None else [])
    segments = await asyncio.to_thread(
        _segments_utm, polylines, utm_epsg) if polylines else []
    # The structure footprint is the centreline buffered by half the declared width, the outline the
    # mesher punched with. Boundary nodes are measured against that outline plus the mesh's own edge:
    # relaxation places nodes on a locked outline only to within the edge (e.g. one population at 9-11 m
    # off a 20 m structure), so an exact-equality cut would split it. A structure face wins a contested
    # node: an incident-wave barrier would radiate the sheltering away from the lee.
    band_m = float(structure_width_m) / 2.0 + float(facts["mesh_size_m"])
    on_structure = set(_nodes_near(segments, points_utm, boundary_nodes, band_m))
    structure_nodes, open_nodes = _settled_walk(
        boundary_nodes, on_structure, set(open_nodes) - on_structure)

    import numpy as np

    journal_note(
        f"harbour boundary: {len(boundary_nodes)} boundary nodes, "
        f"{len(open_nodes)} of them the designated liquid edge the "
        f"{wave_height_m:g} m incident wave enters through, "
        f"{len(structure_nodes)} standing on the {structure_width_m:g} m "
        f"structure footprint and reflecting "
        f"{reflection_coef:g} of it; every other face is the absorbing shore. "
        f"{topology['states']}.")
    return {
        **facts,
        "name": slug_of(facts["mesh_name"]),
        "keywords": {"TITLE": f"ARTEMIS {facts['mesh_name']}"},
        "cli_text": cli_text,
        "open_nodes": open_nodes,
        "structure_nodes": structure_nodes,
        "structure_segments": [[float(v) for v in seg] for seg in segments],
        "boundary_nodes": len(boundary_nodes),
        "open_boundary_nodes": len(open_nodes),
        "structure_boundary_nodes": len(structure_nodes),
        "boundary_states": topology["states"],
        "wave_period_s": float(wave_period_s),
        "wave_height_m": float(wave_height_m),
        "wave_direction_deg": float(wave_direction_deg),
        "reflection_coef": float(reflection_coef),
        "max_depth_m": round(float(-np.nanmin(node_bed)), 2),
        "mesh_inputs": [
            {"gs_uri": geometry_uri(files, missing=_harbour_mesh_missing),
             "dest": HARBOUR_GEOMETRY}],
        "server_facts": {
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6) for v in facts["lonlat_bounds"]],
            "npoin": facts["mesh_node_count"],
            "nelem": facts["mesh_element_count"],
            "mesh_size_m": facts["mesh_size_m"],
            "name": facts["mesh_name"],
            "result_slf": result_basename,
            "bed_source": facts["bed_source"]},
    }
