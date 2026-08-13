"""Coastal-TIN -> solver-mesh format writers for the ADR 0192 mesh-front sandbox.

SANDBOX ONLY (nothing landed): given the oceanmesh worker's raw output
(``points`` (N,2) lon/lat + ``cells`` (M,3) 0-indexed triangles), emit the two
cheapest-to-prove unstructured formats:

  * SCHISM ``hgrid.gr3`` -- delegated to the ALREADY-PROVEN in-repo bridge
    ``services/workers/schism/schism_gr3.tin_to_hgrid`` (pure numpy). This is
    the format SCHISM's own grid check (AQUIRE_HGRID / ipre) validates.
  * ADCIRC ``fort.14`` -- the classic node/element/boundary ASCII that SWAN
    (unstructured) and ADCIRC read, and the format ADCIRC-family tooling round-
    trips. Written here by REUSING the same schism_gr3 boundary helpers
    (pinch-point cleaning, CCW normalization, Eulerian boundary-loop
    extraction) so gr3 and fort.14 come from ONE topology pass.

No trid3nt/server imports; numpy only.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

# schism_gr3 is added to sys.path by the driver (services/workers/schism on path).
from schism_gr3 import (  # type: ignore
    extract_boundary_loops,
    remove_boundary_pinch_points,
    signed_area_ccw,
)


def _clean_and_orient(
    points: np.ndarray, cells: np.ndarray, depths: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pinch-clean, re-index, and CCW-normalize -- the shared topology pass."""
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    depths = np.asarray(depths, dtype=float)
    n_nodes = points.shape[0]

    cells = remove_boundary_pinch_points(points, cells)
    used = np.unique(cells)
    if used.shape[0] != n_nodes:
        remap = np.full(n_nodes, -1, dtype=np.int64)
        remap[used] = np.arange(used.shape[0])
        points = points[used]
        depths = depths[used]
        cells = remap[cells]

    area = signed_area_ccw(points, cells)
    cw = area < 0
    if cw.any():
        cells = cells.copy()
        cells[cw] = cells[cw][:, [0, 2, 1]]
    return points, cells, depths


def _open_nodes_on_side(
    points: np.ndarray, ext_loop: list[int], side: str | None
) -> list[int]:
    if not side or not ext_loop:
        return []
    lon = points[ext_loop, 0]
    lat = points[ext_loop, 1]
    if side == "south":
        thr = lat <= np.percentile(lat, 15)
    elif side == "north":
        thr = lat >= np.percentile(lat, 85)
    elif side == "west":
        thr = lon <= np.percentile(lon, 15)
    elif side == "east":
        thr = lon >= np.percentile(lon, 85)
    else:
        raise ValueError(f"bad open_boundary_side {side!r}")
    return [ext_loop[i] for i in np.where(thr)[0]]


def write_fort14(
    points: np.ndarray,
    cells: np.ndarray,
    *,
    depths: float | Sequence[float] | np.ndarray = 10.0,
    grid_name: str = "trid3nt_coastal_tin",
    open_boundary_side: str | None = None,
) -> str:
    """Emit ADCIRC ``fort.14`` text from a coastal TIN.

    ``points`` (N,2) lon/lat (EPSG:4326); ``cells`` (M,3) 0-indexed triangles.
    ``depths`` positive-down bathymetry (scalar or per-node). Open boundary =
    the exterior-loop nodes on ``open_boundary_side`` (IBTYPEE 0 elevation-
    specified); every other boundary loop is a mainland/island flux boundary
    (IBTYPE 0 mainland exterior, IBTYPE 1 island). Returns the fort.14 string.
    """
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    n0 = points.shape[0]
    dep = np.full(n0, float(depths)) if np.isscalar(depths) else np.asarray(depths, float)
    points, cells, dep = _clean_and_orient(points, cells, dep)
    n_nodes = points.shape[0]
    n_elem = cells.shape[0]

    loops = extract_boundary_loops(cells)
    # Orient exterior CCW, islands CW (ADCIRC / SCHISM water-on-the-left).

    def _loop_area(loop: list[int]) -> float:
        xy = points[loop]
        x, y = xy[:, 0], xy[:, 1]
        return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    oriented: list[list[int]] = []
    for li, loop in enumerate(loops):
        a = _loop_area(loop)
        want_ccw = li == 0
        if (a > 0) != want_ccw:
            loop = [loop[0]] + loop[1:][::-1]
        oriented.append(loop)
    loops = oriented

    open_nodes = _open_nodes_on_side(points, loops[0] if loops else [], open_boundary_side)
    open_set = set(open_nodes)

    L: list[str] = []
    L.append(grid_name)
    L.append(f"{n_elem} {n_nodes}")
    for i in range(n_nodes):
        # ADCIRC depth convention is positive-DOWN (bathymetric depth).
        L.append(f"{i + 1} {points[i, 0]:.9f} {points[i, 1]:.9f} {dep[i]:.6f}")
    for e in range(n_elem):
        a, b, c = cells[e] + 1
        L.append(f"{e + 1} 3 {a} {b} {c}")

    # Open boundary block (elevation-specified forcing boundary).
    if open_nodes:
        L.append("1 = Number of open boundaries")
        L.append(f"{len(open_nodes)} = Total number of open boundary nodes")
        L.append(f"{len(open_nodes)} = Number of nodes for open boundary 1")
        L.extend(str(n + 1) for n in open_nodes)
    else:
        L.append("0 = Number of open boundaries")
        L.append("0 = Total number of open boundary nodes")

    # Land/flux boundary block.
    land: list[tuple[list[int], int]] = []
    for li, loop in enumerate(loops):
        seg = [n for n in loop if n not in open_set]
        if len(seg) >= 2:
            land.append((seg, 0 if li == 0 else 1))
    total_land = sum(len(s) for s, _ in land)
    L.append(f"{len(land)} = Number of land boundaries")
    L.append(f"{total_land} = Total number of land boundary nodes")
    for si, (seg, ibtype) in enumerate(land, start=1):
        L.append(f"{len(seg)} {ibtype} = # nodes for land boundary {si}")
        L.extend(str(n + 1) for n in seg)
    return "\n".join(L) + "\n"


def write_2dm(
    points: np.ndarray,
    cells: np.ndarray,
    *,
    z: float | Sequence[float] | np.ndarray = 0.0,
    material_id: int = 1,
) -> str:
    """Emit an SMS ``2dm`` mesh (the simplest QGIS/MDAL-readable unstructured
    format). ``points`` (N,2) X/Y; ``cells`` (M,3) 0-indexed triangles; ``z``
    per-node elevation (positive-up), written as the node Z so MDAL surfaces the
    bathymetry as the mesh dataset. Returns the 2dm text.

    Layout (MDAL 2DM reader): ``MESH2D`` header, ``E3T id n1 n2 n3 mat`` element
    lines (1-indexed CCW), ``ND id x y z`` node lines.
    """
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    n = points.shape[0]
    zz = np.full(n, float(z)) if np.isscalar(z) else np.asarray(z, dtype=float)
    L = ["MESH2D"]
    for e in range(cells.shape[0]):
        a, b, c = cells[e] + 1
        L.append(f"E3T {e + 1} {a} {b} {c} {material_id}")
    for i in range(n):
        L.append(f"ND {i + 1} {points[i, 0]:.9f} {points[i, 1]:.9f} {zz[i]:.6f}")
    return "\n".join(L) + "\n"


def mesh_quality_report(points: np.ndarray, cells: np.ndarray) -> dict:
    """Independent V&V of the TIN in a LOCAL metric frame (not the worker's own
    stats): equilateral quality q_E per triangle, inverted-element count (signed
    area sign disagreement), and boundary closure (every boundary node even
    boundary-degree == closed loops). Metres via the lat/lon degree scaling."""
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    mid_lat = float(np.mean(points[:, 1]))
    m_lat = 111_320.0
    m_lon = m_lat * max(0.05, float(np.cos(np.radians(mid_lat))))
    px = (points[:, 0] - points[:, 0].mean()) * m_lon
    py = (points[:, 1] - points[:, 1].mean()) * m_lat
    pm = np.column_stack([px, py])
    tri = pm[cells]
    e0 = tri[:, 1] - tri[:, 0]
    e1 = tri[:, 2] - tri[:, 1]
    e2 = tri[:, 0] - tri[:, 2]
    a2 = (e0 ** 2).sum(1) + (e1 ** 2).sum(1) + (e2 ** 2).sum(1)
    signed = 0.5 * (e0[:, 0] * (-e2[:, 1]) - (-e2[:, 0]) * e0[:, 1])
    area = np.abs(signed)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = 4.0 * np.sqrt(3.0) * area / a2
    q = q[np.isfinite(q)]
    seg = np.sqrt(np.concatenate([(e0 ** 2).sum(1), (e1 ** 2).sum(1), (e2 ** 2).sum(1)]))

    # inverted == triangles whose signed area sign disagrees with the majority.
    pos = int((signed > 0).sum())
    neg = int((signed < 0).sum())
    inverted = min(pos, neg)

    # boundary closure: boundary edges used once; every boundary node even degree.
    ec: dict[tuple[int, int], int] = {}
    for t in cells:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            k = (int(a), int(b)) if a < b else (int(b), int(a))
            ec[k] = ec.get(k, 0) + 1
    bnd_deg: dict[int, int] = {}
    for (a, b), n in ec.items():
        if n == 1:
            bnd_deg[a] = bnd_deg.get(a, 0) + 1
            bnd_deg[b] = bnd_deg.get(b, 0) + 1
    odd = sum(1 for d in bnd_deg.values() if d % 2 == 1)
    loops = extract_boundary_loops(cells)
    return {
        "n_vertices": int(points.shape[0]),
        "n_elements": int(cells.shape[0]),
        "min_quality_qE": round(float(q.min()), 4),
        "median_quality_qE": round(float(np.median(q)), 4),
        "mean_quality_qE": round(float(q.mean()), 4),
        "frac_below_0p3": round(float((q < 0.3).mean()), 4),
        "inverted_elements": inverted,
        "edge_min_m": round(float(seg.min()), 1),
        "edge_median_m": round(float(np.median(seg)), 1),
        "edge_max_m": round(float(seg.max()), 1),
        "boundary_nodes": len(bnd_deg),
        "boundary_odd_degree_nodes": odd,
        "boundary_closed": bool(odd == 0),
        "n_boundary_loops": len(loops),
        "exterior_loop_nodes": len(loops[0]) if loops else 0,
    }
