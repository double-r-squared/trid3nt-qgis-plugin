"""SELAFIN in the daemon: the TELEMAC geometry pair out, a result in.

The geometry and its ``.cli`` are one artifact from one walk: boundary rows are ordered by the
geometry's IPOBO, so any other numbering classifies the wrong nodes. Nothing here may shell into the image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np

from trid3nt_server.tools.mesh.meshers import MeshToolError
from trid3nt_server.tools.mesh.shared.formats.tin_topology import (
    BoundaryPinched,
    boundary_numbering,
)
from .topology import FREE_EXIT_ROLE, RATING_CURVE_ROLE

__all__ = ["BOUNDARY_CONDITIONS_FILE", "GEOMETRY_FILE", "telemac_boundary",
           "write_telemac_pair", "read_selafin", "SelafinReadError"]

# The steering keywords TELEMAC reads the pair under; a mesh's file map is keyed by them.
GEOMETRY_FILE: str = "GEOMETRY FILE"


BOUNDARY_CONDITIONS_FILE: str = "BOUNDARY CONDITIONS FILE"

# TELEMAC's boundary-condition type codes (``declarations_telemac.f``): prescribed value, free exit, solid wall.
KENT, KSORT, KLOG = 5, 4, 2

# Boundary role -> the ``(LIHBOR, LIUBOR, LIVBOR, LITBOR)`` quad. An inflow prescribes velocity and
# tracer, leaving depth free; outflow, open sea and rating curve prescribe a level with free velocity,
# named apart because the liquid-boundary order tells the steering author which carries a flowrate,
# a level, or a curve. A free exit prescribes nothing (``bord.f`` overrides depth only under
# ``LIHBOR = KENT``, velocity only under ``LIUBOR = KENT``), well-posed only while the velocity leaves
# (``propin_telemac2d.f`` refuses an entering free velocity). The quad lands in the ``.cli`` and
# ``_prescribes`` derives the steering keyword from it, so an entry here moves both files.
_ROLE_CODES = {
    "wall": (KLOG, KLOG, KLOG, KLOG),
    "inflow": (KSORT, KENT, KENT, KENT),
    "outflow": (KENT, KSORT, KSORT, KSORT),
    "open": (KENT, KSORT, KSORT, KSORT),
    RATING_CURVE_ROLE: (KENT, KSORT, KSORT, KSORT),
    FREE_EXIT_ROLE: (KSORT, KSORT, KSORT, KSORT),
}

# One ``.cli`` row in ``bief``'s column widths: three type codes, four velocity-side values, tracer code and its three, global node number, boundary rank.
_CLI_ROW = ("{0:3d}{1:2d}{2:2d}{3:25.12f}{4:25.12f}{5:25.12f}{6:26.12f}"
            "{7:4d}{8:25.12f}{9:25.12f}{10:25.12f}{11:10d}{12:10d}")


class SelafinReadError(RuntimeError):
    """The result file could not be opened."""

    error_code = "TELEMAC_RESULT_READ_FAILED"


def _prescribes(codes) -> str:
    """What a code quad makes the engine read from the steering file.

    ``"nothing"`` is a free exit: an answer, never a gap.
    """
    # ``bord.f`` consumes elevations only where ``LIHBOR`` is KENT and flowrate only where ``LIUBOR`` is; reading the quad rather than the role keeps the steering file consistent.
    lihbor, liubor = int(codes[0]), int(codes[1])
    if lihbor == KENT:
        return "elevation"
    if liubor == KENT:
        return "flowrate"
    return "nothing"


def _roles_by_node(roles: Mapping[str, Any]) -> dict:
    out: dict = {}
    for role, nodes in roles.items():
        if role not in _ROLE_CODES or role == "wall":
            raise ValueError(
                f"boundary role {role!r} states no TELEMAC condition; the roles a "
                f"mesh can carry are {sorted(set(_ROLE_CODES) - {'wall'})}")
        for node in nodes:
            node = int(node)
            if out.setdefault(node, role) != role:
                raise ValueError(
                    f"boundary node {node} is named both {out[node]!r} and "
                    f"{role!r}; a node carries one boundary condition")
    return out


def _successors(contour_lengths) -> list:
    """``kp1bor`` over the written row order: the next row on the same contour.

    A run straddling a contour's first row is one boundary to the engine.
    """
    kp1: list = []
    at = 0
    for length in contour_lengths:
        kp1 += [at + (i + 1) % length for i in range(length)]
        at += length
    return kp1


def _south_west(keys, unvisited) -> int:
    """The row FRONT2 starts a contour at: south-westernmost, then southernmost (off the geometry, never the file)."""
    sums = [keys[k][0] for k in unvisited]
    lowest, highest = min(sums), max(sums)
    eps = (highest - lowest) * 1.0e-4
    corner = min(unvisited, key=lambda k: keys[k][0])
    ties = [k for k in unvisited if abs(keys[k][0] - lowest) < eps]
    return min(ties, key=lambda k: keys[k][1]) if ties else corner


def _liquid_boundaries(x, y, bnodes, codes, contour_lengths) -> list:
    """TELEMAC's own liquid-boundary numbering -> one ``[first, last]`` row pair; a port of ``bief/front2.f``."""
    kp1 = _successors(contour_lengths)
    quads = [(int(quad[0]), int(quad[1])) for quad in codes]
    solid = [quad[0] == KLOG for quad in quads]
    keys = [(float(x[n]) + float(y[n]), float(y[n])) for n in bnodes]
    seen = [False] * len(kp1)
    runs: list = []
    # FRONT2 starts each contour at the south-westernmost point, walks the successor, calls a segment
    # solid when either end is solid, and folds the run straddling the start into one boundary. Row-order
    # numbering disagrees when the inflow holds the south-west corner: level and flowrate land on the
    # wrong numbers, each into a code that never reads it, and the outflow clamps to elevation zero.
    while not all(seen):
        start = _south_west(keys, [k for k in range(len(seen)) if not seen[k]])
        opened_first = not (solid[start] or solid[kp1[start]])
        first_run = len(runs) + 1 if opened_first else 0
        if opened_first:
            runs.append([start, start])
        ends_liquid = False
        ends_solid = False
        seen[start] = True
        previous, here = start, kp1[start]
        while True:
            back, at, ahead = solid[previous], solid[here], solid[kp1[here]]
            if back and not at and not ahead:
                runs.append([here, here])
                ends_liquid, ends_solid = True, False
            elif not back and not at and ahead:
                runs[-1][1] = here
                ends_liquid, ends_solid = False, True
            elif not back and not at and not ahead:
                ends_liquid, ends_solid = True, False
                # A liquid-liquid seam where the codes change is a boundary break: a level and a flowrate touching are two boundaries.
                if quads[here] != quads[kp1[here]]:
                    runs[-1][1] = here
                    runs.append([kp1[here], kp1[here]])
            elif back and not at and ahead:
                raise ValueError(
                    f"boundary node {int(bnodes[here])} is a lone liquid point "
                    "between two solid ones; TELEMAC (front2.f) refuses it")
            elif not back and at and not ahead:
                raise ValueError(
                    f"boundary node {int(bnodes[here])} is a lone solid point "
                    "between two liquid ones; TELEMAC (front2.f) refuses it")
            seen[here] = True
            previous, here = here, kp1[here]
            if here == start:
                break
        if ends_solid:
            if opened_first:
                runs[first_run - 1][0] = start
        elif ends_liquid:
            if opened_first:
                if first_run != len(runs):
                    runs[first_run - 1][0] = runs[-1][0]
                    runs.pop()
            else:
                runs[-1][1] = start
        elif opened_first:
            # A contour of one type: an all-liquid ring is one circular boundary from the start point.
            runs[first_run - 1] = [start, start]
    return runs


def _numliq(runs, kp1, nptfr) -> list:
    numliq = [0] * nptfr
    for number, (first, last) in enumerate(runs, start=1):
        row = first
        numliq[row] = number
        while True:
            row = kp1[row]
            numliq[row] = number
            if row == last:
                break
    return numliq


def _joined(values) -> str:
    """One statement per numbered boundary, or the joined names when it is two.

    A boundary whose rows disagree is reported joined, never resolved here.
    """
    return "+".join(sorted(set(values))) if values else "wall"


def _write_cli(path: Path, bnodes, codes) -> None:
    rows = []
    for rank, (node, quad) in enumerate(zip(bnodes, codes), start=1):
        rows.append(_CLI_ROW.format(
            int(quad[0]), int(quad[1]), int(quad[2]),
            0.0, 0.0, 0.0, 0.0, int(quad[3]), 0.0, 0.0, 0.0,
            int(node) + 1, rank))
    path.write_text("\n".join(rows) + "\n")


def telemac_boundary(*, x: Any, y: Any, cells: Any,
                     roles: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The engine's own boundary numbering over this geometry -> the walk.

    The IPOBO order, the quad on every row, and what each liquid boundary prescribes; the ``.cli``
    and the topology are both read off this one walk.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    try:
        ipobo, contours = boundary_numbering(cells, x.shape[0])
    except BoundaryPinched as pinched:
        raise MeshToolError("MESH_BOUNDARY_PINCHED", str(pinched)) from pinched
    npoin = int(x.shape[0])
    bnodes = np.where(ipobo > 0)[0][np.argsort(ipobo[ipobo > 0])]
    nptfr = int(bnodes.shape[0])
    role_of = _roles_by_node(roles or {})
    codes = np.array([_ROLE_CODES[role_of.get(int(n), "wall")] for n in bnodes],
                     dtype=np.int32)
    lengths = [len(c) for c in contours]
    runs = _liquid_boundaries(x, y, bnodes, codes, lengths)
    numliq = _numliq(runs, _successors(lengths), nptfr)
    rows = [[k for k in range(nptfr) if numliq[k] == number]
            for number in range(1, len(runs) + 1)]
    stats = {
        "npoin": npoin, "nelem": int(cells.shape[0]), "nptfr": nptfr,
        "liquid_nodes": len(role_of), "n_liquid_boundaries": len(runs),
        "liquid_boundary_roles": [
            _joined([role_of.get(int(bnodes[k]), "wall") for k in here])
            for here in rows],
        "liquid_boundary_prescribes": [
            _joined([_prescribes(codes[k]) for k in here]) for here in rows],
        "n_contours": len(contours),
        "ipobo_distinct": nptfr, "ipobo_max": int(ipobo.max()),
        "ipobo_is_permutation": True,
        "contour_lengths": lengths,
        "cli_contour_runs": len(contours)}
    return {"ipobo": ipobo, "bnodes": bnodes, "codes": codes, "stats": stats}


def write_telemac_pair(rundir: Path | str, *, x: Any, y: Any, cells: Any,
                       bed: Any, roles: Mapping[str, Any] | None = None,
                       title: str = "TRID3NT MESH") -> dict[str, Any]:
    """Write the SELAFIN geometry and its ``.cli`` -> the two paths and the walk.

    ``roles`` maps a boundary role to its node indices; the rest are wall.
    """
    from serafin import SerafinHeader, SerafinWriter

    rundir = Path(rundir)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    walk = telemac_boundary(x=x, y=y, cells=cells, roles=roles)
    ipobo, bnodes, codes = walk["ipobo"], walk["bnodes"], walk["codes"]
    npoin = int(x.shape[0])

    geo_slf, cli = rundir / "mesh.slf", rundir / "mesh.cli"
    header = SerafinHeader(title=str(title)[:72])
    # ``from_triangulation`` takes 1-based connectivity and the walk's IPOBO; left alone it would rebuild IPOBO and the ``.cli`` rows would follow a numbering no other file shares.
    header.from_triangulation(np.column_stack([x, y]), cells + 1, ipobo)
    header.add_variable_str("BOTTOM", "BOTTOM", "M")
    bottom = (np.asarray(bed, dtype=float) if bed is not None
              else np.zeros(npoin, dtype=float))
    # The pair is one artifact; either half failing refuses by name.
    try:
        with SerafinWriter(str(geo_slf), "en", overwrite=True) as writer:
            writer.write_header(header)
            writer.write_entire_frame(header, 0.0, bottom.reshape(1, -1))
        _write_cli(cli, bnodes, codes)
    except Exception as failure:
        raise MeshToolError(
            "MESH_PAIR_WRITE_FAILED",
            f"the SELAFIN writer could not write {geo_slf.name} and "
            f"{cli.name} into {rundir}: {failure}") from failure

    return {"geo_slf": geo_slf, "cli": cli, "stats": walk["stats"]}


def read_selafin(path: str | Path) -> dict[str, Any]:
    """A result file -> its mesh and per-variable time series.

    ``varnames`` carry no unit (``varunits`` does), ``ikle`` is 0-based, origins are not applied.
    """
    # ``x``/``y`` stay as the file stores them: a reader placing a local-coordinate mesh adds the origin itself, and applying it here would double the offset.
    from serafin import SerafinReader

    slf = Path(path).resolve()
    try:
        reader = SerafinReader(str(slf), "en")
        reader.__enter__()
        reader.read_header()
        reader.get_time()
    except Exception as failure:
        raise SelafinReadError(
            f"the SELAFIN reader could not open {slf.name}: {failure}") from failure
    try:
        header = reader.header
        varnames = [name.decode().strip() for name in header.var_names]
        nplan = int(header.nb_planes)
        times = np.asarray(reader.time, dtype="float64")
        # Read by position, one seek per frame: the reader's lookup keys on the dictionary id, and two variables sharing an id (appended tracers) would return one twice.
        frames = (np.stack([reader.read_vars_in_frame(index)
                            for index in range(times.shape[0])])
                  if times.shape[0]
                  else np.empty((0, len(varnames), int(header.nb_nodes))))
        data = {name: np.asarray(frames[:, index], dtype="float64")
                for index, name in enumerate(varnames)}
        return {
            "varnames": varnames,
            "varunits": [unit.decode().strip() for unit in header.var_units],
            "npoin": int(header.nb_nodes),
            "nelem": int(header.nb_elements),
            # A 3D field is flat over NPOIN3 and is NPLAN planes stacked over the 2D mesh, bottom first; a 2D file reports one plane and the same mesh twice.
            "nplan": nplan,
            "npoin2": int(header.nb_nodes_2d),
            "nelem2": int(header.nb_elements_2d),
            "x": np.asarray(header.x_stored, dtype="float64"),
            "y": np.asarray(header.y_stored, dtype="float64"),
            "ikle": np.asarray(header.ikle_2d if nplan <= 1 else header.ikle,
                               dtype="int64").reshape(
                                   int(header.nb_elements), -1) - 1,
            "ikle2": np.asarray(header.ikle_2d, dtype="int64") - 1,
            "x_origin": int(header.mesh_origin[0]),
            "y_origin": int(header.mesh_origin[1]),
            "times": times,
            "data": data,
        }
    # A file whose header opens and whose frames do not is unreadable too, by the same name.
    except Exception as failure:
        raise SelafinReadError(
            f"the SELAFIN reader could not read {slf.name}: {failure}") from failure
    finally:
        reader.__exit__(None, None, None)
