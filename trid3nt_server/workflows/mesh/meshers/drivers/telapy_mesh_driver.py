"""In-container telapy mesh driver for the ``telapy_mesh`` mesher.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place ``telapy`` and
``pretel`` are installed. The host mesher mounts this file and a rundir and
shells it once per operation; nothing here imports trid3nt code.

  python telapy_mesh_driver.py <op> /data/config.json /data

Ops, and the official surface each one delegates to:

  read    HermesFile geometry accessors -> nodes, connectivity, BOTTOM
  write   HermesFile.set_mesh + set_bnd -> the SELAFIN geometry and its .cli,
          then Conlim.set_numliq for the liquid-boundary numbering
  punch   element removal inside a polygon + pretel remove_extra_nodes and
          get_ipobo to re-derive the boundary and its IPOBO
  refine  node insertion at a requested spacing inside a polygon, re-triangulated
          through the Delaunay pretel itself meshes with, filtered back to the
          domain the boundary contours describe

Every op reads and writes the same ``.npz`` shape: ``x``, ``y`` (npoin), ``ikle``
(nelem,3 0-based), ``bottom`` (npoin, positive up), plus the boundary
``contours`` as a flat node list with per-contour lengths.

Two constraints this file works around, both verified in the image:
``Conlim.put_content`` does not round-trip a .cli written by ``set_bnd`` (it
drops rows and the NUMLIQ column), so Conlim is used for its numbering and the
file itself is hermes's; and ``pretel.meshes.cleave_max_7_nodes`` - the only
local refinement primitive in pretel - raises ``Implementation not finished``,
so a region refine inserts nodes and re-triangulates instead.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

import numpy as np  # noqa: E402
from scipy.spatial import Delaunay, cKDTree  # noqa: E402
from shapely import contains_xy  # noqa: E402
from shapely.geometry import Polygon, shape as _shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from pretel.meshes import get_ipobo, remove_extra_nodes  # noqa: E402

#: TELEMAC boundary-condition codes for the two classes a mesh can state: a
#: prescribed-elevation open boundary with free velocity, and a solid wall.
_OPEN = (5, 4, 4, 4)
_LAND = (2, 2, 2, 2)


def _load_geoms(path: str) -> list:
    doc = json.load(open(path))
    feats = doc.get("features") if isinstance(doc, dict) else None
    if feats is not None:
        return [_shape(f["geometry"]) for f in feats if f.get("geometry")]
    if isinstance(doc, dict) and doc.get("type") == "GeometryCollection":
        return [_shape(g) for g in doc["geometries"]]
    return [_shape(doc)]


def _boundary(x, y, ikle):
    """pretel's own boundary walk -> ``(ipobo, contours)``, which agree by construction.

    ``get_ipobo`` returns the closed contours it walked (each already stripped of
    its repeated closing node) and an IPOBO numbered along them with ONE count
    continuing across contours - which is what TELEMAC's permutation of 1..NPTFR
    means, and what a per-contour count would break. Its own array numbers
    ``contour[1:]``, leaving the first node of every contour at 0 where TELEMAC
    would read a boundary node as interior, so the walk is the authority here and
    every node on it is numbered once, in walk order.
    """
    _, pbounds = get_ipobo(x, y, np.asarray(ikle, dtype=np.int32), debug=False)
    contours = [[int(n) for n in ring] for ring in pbounds]
    ipobo = np.zeros(len(x), dtype=np.int32)
    position = 0
    for ring in contours:
        for node in ring:
            position += 1
            ipobo[node] = position
    return ipobo, contours


def _contours(x, y, ikle) -> list[list[int]]:
    return _boundary(x, y, ikle)[1]


def _domain_polygon(x, y, contours):
    """The meshed domain the contours describe: the widest ring, minus the rest."""
    rings = [Polygon([(float(x[n]), float(y[n])) for n in c])
             for c in contours if len(c) >= 3]
    if not rings:
        return None
    rings.sort(key=lambda p: p.area, reverse=True)
    domain = rings[0]
    for hole in rings[1:]:
        domain = domain.difference(hole)
    return domain


def _save(path: str, x, y, ikle, bottom, contours) -> dict:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ikle = np.asarray(ikle, dtype=np.int64)
    flat = np.array([n for c in contours for n in c], dtype=np.int64)
    lens = np.array([len(c) for c in contours], dtype=np.int64)
    np.savez(path, x=x, y=y, ikle=ikle,
             bottom=(np.asarray(bottom, dtype=float)
                     if bottom is not None else np.empty(0)),
             contour_nodes=flat, contour_lengths=lens)
    return {"npoin": int(x.shape[0]), "nelem": int(ikle.shape[0]),
            "n_contours": len(contours),
            "boundary_nodes": int(flat.shape[0])}


def _load(path: str):
    npz = np.load(path)
    x = np.asarray(npz["x"], dtype=float)
    y = np.asarray(npz["y"], dtype=float)
    ikle = np.asarray(npz["ikle"], dtype=np.int64)
    bottom = np.asarray(npz["bottom"], dtype=float)
    bottom = bottom if bottom.shape[0] == x.shape[0] else None
    contours: list[list[int]] = []
    if "contour_lengths" in npz.files:
        flat = np.asarray(npz["contour_nodes"], dtype=np.int64)
        at = 0
        for n in np.asarray(npz["contour_lengths"], dtype=np.int64):
            contours.append([int(v) for v in flat[at:at + int(n)]])
            at += int(n)
    return x, y, ikle, bottom, contours


def op_read(cfg: dict) -> dict:
    from telapy.api.hermes import HermesFile

    geo = HermesFile(cfg["geometry"], cfg.get("fformat", "SERAFIN"), access="r")
    try:
        npoin = geo.get_mesh_npoin()
        nelem = geo.get_mesh_nelem()
        ndp = geo.get_mesh_npoin_per_element()
        if int(ndp) != 3:
            raise ValueError(
                f"the geometry has {int(ndp)} nodes per element; the mesh tool "
                "carries triangles")
        x = np.asarray(geo.get_mesh_coord(1), dtype=float)
        y = np.asarray(geo.get_mesh_coord(2), dtype=float)
        # get_mesh_connectivity already returns 0-based (nelem, ndp).
        ikle = np.asarray(geo.get_mesh_connectivity(), dtype=np.int64)
        ikle = ikle.reshape((int(nelem), 3))
        # get_data_var_list returns (names, units), not a flat name list.
        names = geo.get_data_var_list() or ([], [])
        variables = [str(v).strip() for v in names[0]]
        bottom = None
        records = int(geo.get_data_ntimestep() or 0)
        for name in variables:
            if records and (name.upper().startswith("BOTTOM")
                            or name.upper().startswith("FOND")):
                bottom = np.asarray(geo.get_data_value(name, 0), dtype=float)
                break
        title = str(geo.get_mesh_title()).strip()
    finally:
        geo.close()

    contours = _contours(x, y, ikle)
    stats = _save(cfg["out_npz"], x, y, ikle, bottom, contours)
    stats.update({"title": title, "variables": variables,
                  "has_bottom": bottom is not None,
                  "npoin_declared": int(npoin)})
    return stats


def op_write(cfg: dict) -> dict:
    from data_manip.formats.conlim import Conlim
    from telapy.api.hermes import BND_POINT, TRIANGLE, HermesFile

    x, y, ikle, bottom, _ = _load(cfg["mesh_npz"])
    # The IPOBO written into the geometry and the contours the .cli is numbered
    # from come from ONE walk of THIS connectivity: a boundary file numbered from
    # any other walk classifies the wrong nodes.
    ipobo, contours = _boundary(x, y, ikle)
    npoin = int(x.shape[0])
    order = np.argsort(ipobo[ipobo > 0])
    bnodes = np.where(ipobo > 0)[0][order]
    nptfr = int(bnodes.shape[0])
    distinct = int(np.unique(ipobo[ipobo > 0]).shape[0])
    if distinct != nptfr:
        raise ValueError(
            f"IPOBO holds {nptfr} nonzero entries but only {distinct} distinct "
            "values; TELEMAC requires a permutation of 1..NPTFR")
    open_nodes = set(int(n) for n in (cfg.get("open_nodes") or []))
    codes = np.array([_OPEN if int(n) in open_nodes else _LAND for n in bnodes],
                     dtype=np.int32)

    for path in (cfg["geo_slf"], cfg["cli"]):
        if os.path.exists(path):
            os.remove(path)
    geo = HermesFile(cfg["geo_slf"], "SERAFIN", access="w",
                     boundary_file=cfg["cli"])
    try:
        geo.set_header(cfg.get("title", "TRID3NT MESH")[:72],
                       1, ["BOTTOM          "], ["M               "])
        # hermes derives NDP from the element type, so a wrong type writes a
        # header that disagrees with the connectivity beside it; and set_mesh
        # transposes and 1-bases the connectivity itself, so it takes the
        # (nelem, ndp) 0-based array as it stands.
        geo.set_mesh(2, TRIANGLE, 3, nptfr, 0, int(ikle.shape[0]), npoin,
                     np.asarray(ikle, dtype=np.int32), ipobo,
                     np.arange(1, npoin + 1, dtype=np.int32), x, y, 1,
                     [2024, 1, 1], [0, 0, 0], 0.0, 0.0)
        zeros = np.zeros(nptfr, dtype=float)
        geo.set_bnd(BND_POINT, nptfr, bnodes.reshape((-1, 1)).astype(np.int32),
                    codes[:, 0], codes[:, 1], codes[:, 2],
                    zeros, zeros.copy(), zeros.copy(), zeros.copy(),
                    codes[:, 3], zeros.copy(), zeros.copy(), zeros.copy(),
                    np.arange(1, nptfr + 1, dtype=np.int32))
        bed = (bottom if bottom is not None else np.zeros(npoin, dtype=float))
        geo.add_data("BOTTOM          ", "M               ", 0.0, 0, True,
                     np.asarray(bed, dtype=float))
    finally:
        geo.close()

    # Conlim.set_numliq walks a contour looking for its first solid node and
    # indexes past the end when there is none, so a fully-liquid contour is named
    # here rather than surfacing as an upstream IndexError.
    for index, contour in enumerate(contours):
        if all(int(n) in open_nodes for n in contour):
            raise ValueError(
                f"boundary contour {index} ({len(contour)} nodes) is entirely "
                "open; a TELEMAC domain needs at least one solid node per contour "
                "to number its liquid boundaries")
    bnd = Conlim(cfg["cli"])
    bnd.set_numliq(contours)
    return {"npoin": npoin, "nelem": int(ikle.shape[0]), "nptfr": nptfr,
            "open_nodes": len(open_nodes), "n_liquid_boundaries": int(bnd.nfrliq),
            "n_contours": len(contours),
            "ipobo_distinct": distinct, "ipobo_max": int(ipobo.max()),
            "ipobo_is_permutation": bool(distinct == nptfr
                                         and int(ipobo.max()) == nptfr),
            "contour_lengths": [len(c) for c in contours],
            "cli_contour_runs": _contour_runs(bnodes, contours)}


def _contour_runs(bnodes, contours) -> int:
    """How many CONTIGUOUS stretches the written row order breaks the contours into.

    One run per contour is the whole point: a .cli whose rows interleave two
    contours describes a boundary no walk of the geometry produces.
    """
    where = {}
    for index, contour in enumerate(contours):
        for node in contour:
            where[int(node)] = index
    runs = 0
    previous = None
    for node in bnodes:
        current = where.get(int(node), -1)
        if current != previous:
            runs += 1
            previous = current
    return runs


def _drop_orphans(x, y, cells, bottom):
    """pretel's node removal, with the per-node bed carried through its numbering.

    ``remove_extra_nodes`` renumbers to the sorted unique used nodes and mutates
    the coordinate arrays it is handed, so the bed is indexed by that same list and
    copies go in.
    """
    kept = np.sort(np.unique(np.ravel(cells)))
    x2, y2, likle = remove_extra_nodes(
        np.array(x, dtype=float), np.array(y, dtype=float),
        np.asarray(cells, dtype=np.int32), debug=False)
    bed = None if bottom is None else np.asarray(bottom, dtype=float)[kept]
    return (np.asarray(x2, dtype=float), np.asarray(y2, dtype=float),
            np.asarray(likle, dtype=np.int64), bed)


def op_punch(cfg: dict) -> dict:
    x, y, ikle, bottom, _ = _load(cfg["mesh_npz"])
    hole = unary_union(_load_geoms(cfg["geometry"]))
    tri = np.column_stack([x[ikle].mean(axis=1), y[ikle].mean(axis=1)])
    keep = ~contains_xy(hole, tri[:, 0], tri[:, 1])
    if not keep.any():
        raise ValueError("the obstacle covers the whole mesh; nothing is left")
    removed = int((~keep).sum())
    x, y, ikle, bottom = _drop_orphans(x, y, ikle[keep], bottom)
    contours = _contours(x, y, ikle)
    stats = _save(cfg["out_npz"], x, y, ikle, bottom, contours)
    stats["elements_removed"] = removed
    return stats


def op_refine(cfg: dict) -> dict:
    x, y, ikle, bottom, contours = _load(cfg["mesh_npz"])
    if not contours:
        contours = _contours(x, y, ikle)
    domain = _domain_polygon(x, y, contours)
    if domain is None:
        raise ValueError("the mesh has no boundary contour to refine inside")
    region = unary_union(_load_geoms(cfg["geometry"])).intersection(domain)
    if region.is_empty:
        raise ValueError("the refine region does not overlap the mesh domain")

    step = float(cfg["edge_length"])
    xmin, ymin, xmax, ymax = region.bounds
    gx = np.arange(xmin, xmax + step, step)
    gy = np.arange(ymin, ymax + step, step)
    lattice = np.column_stack([g.ravel() for g in np.meshgrid(gx, gy)])
    inside = contains_xy(region, lattice[:, 0], lattice[:, 1])
    lattice = lattice[inside]
    if lattice.shape[0]:
        far = cKDTree(np.column_stack([x, y])).query(lattice, k=1)[0] > 0.5 * step
        lattice = lattice[far]

    points = np.vstack([np.column_stack([x, y]), lattice])
    cells = Delaunay(points).simplices.astype(np.int64)
    centroid = points[cells].mean(axis=1)
    cells = cells[contains_xy(domain, centroid[:, 0], centroid[:, 1])]
    if cells.shape[0] == 0:
        raise ValueError("re-triangulation left no element inside the domain")

    if bottom is not None and lattice.shape[0]:
        # A node the refine inserted has no sampled bed; the nearest existing node
        # is the only elevation this process actually measured.
        near = cKDTree(np.column_stack([x, y])).query(lattice, k=1)[1]
        bottom = np.concatenate([bottom, bottom[near]])

    x2, y2, cells, bottom = _drop_orphans(
        points[:, 0], points[:, 1], cells, bottom)
    contours = _contours(x2, y2, cells)
    stats = _save(cfg["out_npz"], x2, y2, cells, bottom, contours)
    stats["nodes_inserted"] = int(lattice.shape[0])
    return stats


def op_contours(cfg: dict) -> dict:
    x, y, ikle, bottom, _ = _load(cfg["mesh_npz"])
    return _save(cfg["out_npz"], x, y, ikle, bottom, _contours(x, y, ikle))


_OPS = {"read": op_read, "write": op_write, "punch": op_punch,
        "refine": op_refine, "contours": op_contours}


def main() -> int:
    op = sys.argv[1]
    cfg = json.load(open(sys.argv[2]))
    out = sys.argv[3].rstrip("/")
    stats = _OPS[op](cfg)
    stats["op"] = op
    json.dump(stats, open(out + "/telapy_stats.json", "w"), indent=2)
    print("TELAPY_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
