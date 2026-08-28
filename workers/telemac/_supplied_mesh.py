"""The TELEMAC geometry a run directory was staged with, when one was.

A mesh authored outside the worker arrives as the PAIR its authoring wrote
together: a SELAFIN carrying the nodes, the connectivity and the BOTTOM every node
was sampled at, and the boundary-conditions file numbered from that same
geometry's IPOBO. The two are read as a pair or not at all - a boundary file
numbered from any other walk classifies the wrong nodes, and the classification is
the half of the hand-off geometry cannot carry.

What comes back is the plain dict the rest of this worker's mesh code already
reads, in the LOCAL frame the AOI's south-west corner defines, so a supplied mesh
and a built grid are the same thing to every writer and reader downstream.

Absent staging there is nothing here to read and the caller meshes as it always
has; a pair whose two halves disagree about the node count refuses rather than
solving on a boundary that belongs to another mesh.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

#: Basenames the manifest stages the supplied geometry pair under.
STAGED_MESH_SLF: str = "supplied_mesh.slf"
STAGED_MESH_CLI: str = "supplied_mesh.cli"

#: The node elevation variable a TELEMAC geometry carries.
_BOTTOM = "BOTTOM"

#: LIHBOR 2 is a solid wall; every other code in the first column is liquid.
_SOLID_LIHBOR = 2

#: The fraction of the median element area below which an element has collapsed.
_COLLAPSED_AREA_FRAC = 1e-9


class SuppliedMeshUnusableError(RuntimeError):
    """A mesh was staged and it cannot be read as one."""


def staged_pair(data_dir: str, slf_name: str | None,
                cli_name: str | None) -> tuple[str, str] | None:
    """The staged geometry pair's paths, or ``None`` when none was staged."""
    if not slf_name:
        return None
    slf = os.path.join(data_dir, str(slf_name))
    cli = os.path.join(data_dir, str(cli_name or STAGED_MESH_CLI))
    for path in (slf, cli):
        if not os.path.exists(path):
            raise SuppliedMeshUnusableError(
                f"the run directory names a supplied mesh but {path} is not in it; "
                "the geometry and its boundary file are staged together or not at "
                "all.")
    return slf, cli


def read_supplied_mesh(data_dir: str, slf_name: str, cli_name: str, *,
                       x0m: float = 0.0, y0m: float = 0.0) -> dict[str, Any]:
    """The staged geometry pair as this worker's mesh dict, in the local frame.

    ``x0m`` / ``y0m`` are the projected coordinates of the AOI corner every other
    domain in this worker lays node 0 at, subtracted here so a supplied mesh
    reaches the readers on the same origin they already add back.
    """
    slf, cli = staged_pair(data_dir, slf_name, cli_name)
    x, y, ikle, bed = _read_geometry(slf)
    ring, ipob, liquid = _read_boundary(cli, npoin=int(x.shape[0]))

    x = x - float(x0m)
    y = y - float(y0m)
    edges = _edge_lengths(x, y, ikle)
    return {
        "X": x, "Y": y, "ikle": ikle, "Z": bed,
        "ipob": ipob, "ring": ring, "nptfr": int(ring.shape[0]),
        "npoin": int(x.shape[0]),
        "open_mask": liquid,
        "dx": float(np.median(edges)),
        "edge_min_m": float(edges.min()),
        "edge_median_m": float(np.median(edges)),
        "edge_max_m": float(edges.max()),
    }


def _read_geometry(path: str) -> tuple[Any, Any, Any, Any]:
    """Nodes, connectivity and the bed, through TELEMAC's own SELAFIN reader."""
    from data_manip.extraction.telemac_file import TelemacFile

    tf = TelemacFile(path)
    try:
        x = np.asarray(tf.meshx, dtype=float)
        y = np.asarray(tf.meshy, dtype=float)
        ikle = np.asarray(tf.ikle2, dtype=np.int32)
        names = [str(v).strip().upper() for v in tf.varnames]
        if _BOTTOM not in names:
            raise SuppliedMeshUnusableError(
                f"the supplied geometry {os.path.basename(path)} carries "
                f"{names} and no {_BOTTOM}; a wave solve reads the bed off the "
                "mesh it is given.")
        bed = np.asarray(tf.get_data_value(tf.varnames[names.index(_BOTTOM)], 0),
                         dtype=float)
    finally:
        del tf
    if ikle.size == 0 or x.shape[0] == 0:
        raise SuppliedMeshUnusableError(
            f"the supplied geometry {os.path.basename(path)} holds "
            f"{x.shape[0]} nodes and {ikle.shape[0]} elements.")
    # The reader's connectivity base is not fixed across writers, so it is
    # MEASURED: a 1-based array is rebased rather than read as an off-by-one mesh.
    if int(ikle.min()) == 1 and int(ikle.max()) == x.shape[0]:
        ikle = ikle - 1
    if int(ikle.min()) != 0 or int(ikle.max()) != x.shape[0] - 1:
        raise SuppliedMeshUnusableError(
            f"the supplied geometry indexes nodes {int(ikle.min())}..."
            f"{int(ikle.max())} against {x.shape[0]} nodes; the connectivity and "
            "the node list describe different meshes.")
    if bed.shape[0] != x.shape[0]:
        raise SuppliedMeshUnusableError(
            f"the supplied geometry carries {bed.shape[0]} bed values for "
            f"{x.shape[0]} nodes.")
    _refuse_degenerate(x, y, ikle, path)
    return x, y, ikle, bed


def _refuse_degenerate(x: Any, y: Any, ikle: Any, path: str) -> None:
    """Refuse a geometry carrying an element a solver cannot invert.

    A collapsed or inverted triangle stops the solve with a negative determinant
    and a stack trace naming an element number, several seconds in and with
    nothing that points back at the mesh. One cell out of tens of thousands does
    it, so the count and the worst element are named HERE, before the solve.
    """
    a, b, c = (np.column_stack([x, y])[ikle[:, i]] for i in range(3))
    twice = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
             - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    median = float(np.median(np.abs(twice)))
    bad = np.nonzero(np.abs(twice) <= _COLLAPSED_AREA_FRAC * median)[0]
    if bad.size:
        raise SuppliedMeshUnusableError(
            f"the supplied geometry {os.path.basename(path)} carries {bad.size} "
            f"element(s) with no area to invert (worst: element {int(bad[0]) + 1}, "
            f"twice-area {float(twice[bad[0]]):.3e} against a median of "
            f"{median:.3e}); a solver stops on the first one it reaches.")


def _read_boundary(path: str, *, npoin: int) -> tuple[Any, Any, Any]:
    """The boundary walk the ``.cli`` numbers -> ``(ring, ipob, lihbor)``.

    The file's own last two columns ARE the hand-off: the node each row classifies
    and the boundary rank it holds. Read in rank order they rebuild the walk the
    geometry's IPOBO was written from, which is the only order a rewritten
    boundary file may use.
    """
    rows: list[tuple[int, int, int]] = []
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 3:
                continue
            rows.append((int(parts[-1]), int(parts[-2]), int(parts[0])))
    if not rows:
        raise SuppliedMeshUnusableError(
            f"the supplied boundary file {os.path.basename(path)} holds no rows.")
    rows.sort()
    ranks = [r for r, _, _ in rows]
    if ranks != list(range(1, len(rows) + 1)):
        raise SuppliedMeshUnusableError(
            f"the supplied boundary file ranks {ranks[:5]}... are not a "
            f"permutation of 1..{len(rows)}; TELEMAC numbers its boundary once.")
    ring = np.asarray([n - 1 for _, n, _ in rows], dtype=np.int32)
    if int(ring.min()) < 0 or int(ring.max()) >= npoin:
        raise SuppliedMeshUnusableError(
            f"the supplied boundary file names nodes outside 1..{npoin}.")
    if np.unique(ring).shape[0] != ring.shape[0]:
        raise SuppliedMeshUnusableError(
            "the supplied boundary file names a node twice; a boundary walk "
            "visits each of its nodes once.")
    ipob = np.zeros(npoin, dtype=np.int32)
    ipob[ring] = np.arange(1, ring.shape[0] + 1, dtype=np.int32)
    # A node the boundary file never names is INTERIOR, not liquid: the mask is
    # seeded closed and opened only where a row said so, or every interior node
    # would read as a boundary the incident wave enters through.
    liquid = np.zeros(npoin, dtype=bool)
    liquid[ring] = np.asarray([c for _, _, c in rows],
                              dtype=np.int32) != _SOLID_LIHBOR
    return ring, ipob, liquid


def _edge_lengths(x: Any, y: Any, ikle: Any) -> Any:
    """Every element edge's length, so the mesh reports its own spacing."""
    tri = np.column_stack([x, y])[ikle]
    return np.sqrt(np.concatenate([
        ((tri[:, 1] - tri[:, 0]) ** 2).sum(1),
        ((tri[:, 2] - tri[:, 1]) ** 2).sum(1),
        ((tri[:, 0] - tri[:, 2]) ** 2).sum(1)]))
