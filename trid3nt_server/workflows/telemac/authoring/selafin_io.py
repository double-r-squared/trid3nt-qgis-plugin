"""SELAFIN in the daemon: the TELEMAC geometry pair out, a result in.

The geometry and its ``.cli`` are ONE artifact - the boundary rows are ordered by
the geometry's own IPOBO, so a boundary file written against any other numbering
classifies the wrong nodes - and both come from one walk of one connectivity.
Reading and writing happen in this process: the engine image solves and does
nothing else, so nothing here may shell into it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.shared.formats.tin_topology import (
    BoundaryPinched,
    boundary_numbering,
)
from .topology import FREE_EXIT_ROLE, RATING_CURVE_ROLE

__all__ = ["BOUNDARY_CONDITIONS_FILE", "GEOMETRY_FILE", "telemac_boundary",
           "write_telemac_pair", "read_selafin", "SelafinReadError"]

#: The steering keywords TELEMAC reads the pair under. A mesh's file map is
#: keyed by them, so a reader asks for a file by the name the engine uses for it
#: and no other module spells either string.
GEOMETRY_FILE: str = "GEOMETRY FILE"


BOUNDARY_CONDITIONS_FILE: str = "BOUNDARY CONDITIONS FILE"

#: TELEMAC's own boundary-condition type codes, from ``declarations_telemac.f``:
#: a prescribed value, a free exit, a solid wall.
KENT, KSORT, KLOG = 5, 4, 2

#: Boundary role -> the TELEMAC ``(LIHBOR, LIUBOR, LIVBOR, LITBOR)`` quad that
#: states it. An inflow prescribes velocity and tracer and leaves the depth free;
#: an outflow, an open sea boundary and a RATING CURVE make the SAME statement to
#: the solver - a prescribed water level, free velocity - and are named apart
#: because the measured liquid-boundary order is what a steering author reads to
#: decide which boundary carries a flowrate, which a level, and which one's level
#: is read off a stage-discharge curve instead of a constant. A FREE EXIT
#: prescribes nothing at all: ``bord.f`` overrides the depth only under
#: ``LIHBOR = KENT`` and the velocity only under ``LIUBOR = KENT``, so an
#: all-``KSORT`` quad leaves the water leaving at whatever level and velocity the
#: interior brings to the face. It is well-posed only while that velocity leaves:
#: ``propin_telemac2d.f`` refuses a free velocity whose normal component enters.
#:
#: THIS IS THE ONE AUTHORING DECISION for the pair. The quad lands in the
#: ``.cli`` and :func:`_prescribes` derives the steering keyword from the same
#: quad, so moving an entry here moves both files together.
_ROLE_CODES = {
    "wall": (KLOG, KLOG, KLOG, KLOG),
    "inflow": (KSORT, KENT, KENT, KENT),
    "outflow": (KENT, KSORT, KSORT, KSORT),
    "open": (KENT, KSORT, KSORT, KSORT),
    RATING_CURVE_ROLE: (KENT, KSORT, KSORT, KSORT),
    FREE_EXIT_ROLE: (KSORT, KSORT, KSORT, KSORT),
}

#: One ``.cli`` row, in the column widths ``bief``'s own reader is written
#: against: the three type codes, the four velocity-side values, the tracer code
#: and its three, then the global node number and the boundary rank.
_CLI_ROW = ("{0:3d}{1:2d}{2:2d}{3:25.12f}{4:25.12f}{5:25.12f}{6:26.12f}"
            "{7:4d}{8:25.12f}{9:25.12f}{10:25.12f}{11:10d}{12:10d}")


class SelafinReadError(RuntimeError):
    """The result file could not be opened."""

    error_code = "TELEMAC_RESULT_READ_FAILED"


def _prescribes(codes) -> str:
    """What a code quad makes the engine READ from the steering file.

    ``"nothing"`` is what a FREE EXIT reads as: an answer, never a gap."""
    # ``bord.f`` consumes PRESCRIBED ELEVATIONS only where ``LIHBOR`` is KENT
    # and the prescribed flowrate only where ``LIUBOR`` is; a value written
    # against any other code is a number the engine never looks at. Reading the
    # quad rather than the role name is what leaves the steering file unable to
    # disagree with it.
    lihbor, liubor = int(codes[0]), int(codes[1])
    if lihbor == KENT:
        return "elevation"
    if liubor == KENT:
        return "flowrate"
    return "nothing"


def _roles_by_node(roles: Mapping[str, Any]) -> dict:
    """``{role: [node, ...]}`` -> ``{node: role}``, refusing a node claimed twice."""
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
    """``kp1bor`` over the written row order: the next row on the SAME contour.

    A run straddling a contour's first row is ONE boundary to the engine."""
    kp1: list = []
    at = 0
    for length in contour_lengths:
        kp1 += [at + (i + 1) % length for i in range(length)]
        at += length
    return kp1


def _south_west(keys, unvisited) -> int:
    """The row FRONT2 starts a contour at: south-westernmost, then southernmost.

    The engine picks its start point off the GEOMETRY, never off the file."""
    sums = [keys[k][0] for k in unvisited]
    lowest, highest = min(sums), max(sums)
    eps = (highest - lowest) * 1.0e-4
    corner = min(unvisited, key=lambda k: keys[k][0])
    ties = [k for k in unvisited if abs(keys[k][0] - lowest) < eps]
    return min(ties, key=lambda k: keys[k][1]) if ties else corner


def _liquid_boundaries(x, y, bnodes, codes, contour_lengths) -> list:
    """TELEMAC's OWN liquid-boundary numbering -> one ``[first, last]`` row pair.

    A port of ``bief/front2.f``: the engine's own choice of boundary number 1."""
    kp1 = _successors(contour_lengths)
    quads = [(int(quad[0]), int(quad[1])) for quad in codes]
    solid = [quad[0] == KLOG for quad in quads]
    keys = [(float(x[n]) + float(y[n]), float(y[n])) for n in bnodes]
    seen = [False] * len(kp1)
    runs: list = []
    # FRONT2 does NOT start at the first row of the file: it starts each contour
    # at the south-westernmost boundary point, walks the successor from there,
    # calls a segment solid when EITHER of its ends is solid, and folds the run
    # straddling that start point back into one boundary. Numbering from row
    # order instead agrees only by luck: on a reach whose inflow face holds the
    # domain's south-west corner the two disagree, and a steering file then
    # states its level at the inflow's number and its flowrate at the outflow's
    # - each into a code that never reads it, so the inflow supplies nothing and
    # the outflow is clamped to elevation zero and drains the domain.
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
                # A liquid-liquid seam where the CODES change is a boundary
                # break to the engine: one prescribed level and one prescribed
                # flowrate touching are two boundaries, not one.
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
            # A contour of ONE type: an all-liquid ring is one circular boundary
            # that begins and ends at the start point.
            runs[first_run - 1] = [start, start]
    return runs


def _numliq(runs, kp1, nptfr) -> list:
    """The liquid-boundary number on every row, 0 where the row is solid."""
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

    A boundary whose rows disagree is reported joined, never resolved here."""
    return "+".join(sorted(set(values))) if values else "wall"


def _write_cli(path: Path, bnodes, codes) -> None:
    """The boundary file, one row per boundary node in IPOBO order."""
    rows = []
    for rank, (node, quad) in enumerate(zip(bnodes, codes), start=1):
        rows.append(_CLI_ROW.format(
            int(quad[0]), int(quad[1]), int(quad[2]),
            0.0, 0.0, 0.0, 0.0, int(quad[3]), 0.0, 0.0, 0.0,
            int(node) + 1, rank))
    path.write_text("\n".join(rows) + "\n")


def telemac_boundary(*, x: Any, y: Any, cells: Any,
                     roles: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The engine's OWN boundary numbering over this geometry -> the walk.

    The IPOBO order, the code quad written on every boundary row, and what each
    numbered liquid boundary prescribes. One walk: the ``.cli`` rows and the
    topology beside them are both read off this, so neither can disagree."""
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
        # What each numbered boundary PRESCRIBES, read off the quad written for
        # it - the half of the cross-file contract the steering author reads.
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

    ``roles`` maps a boundary role to its node indices; the rest are wall."""
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
    # ``from_triangulation`` takes the connectivity 1-based and the IPOBO the
    # walk numbered; left to itself it would rebuild IPOBO from its own walk,
    # and the ``.cli`` rows below would then be ordered by a numbering no other
    # file agrees with.
    header.from_triangulation(np.column_stack([x, y]), cells + 1, ipobo)
    header.add_variable_str("BOTTOM", "BOTTOM", "M")
    bottom = (np.asarray(bed, dtype=float) if bed is not None
              else np.zeros(npoin, dtype=float))
    # The pair is ONE artifact: a geometry without its .cli, or a .cli written
    # against a geometry that never landed, is a numbering nothing agrees with.
    # Either half failing refuses by name here - there is no in-image writer
    # left to fall back to.
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

    ``varnames`` carry no unit (``varunits`` does), ``ikle`` is 0-based, origins
    are not applied."""
    # {"varnames": [str], "varunits": [str], "npoin": int, "nelem": int,
    #  "x": ndarray(npoin), "y": ndarray(npoin), "ikle": ndarray(nelem, ndp),
    #  "nplan": int, "npoin2": int, "nelem2": int, "ikle2": ndarray(nelem2, 3),
    #  "x_origin": int, "y_origin": int, "times": ndarray(nframes),
    #  "data": {varname: ndarray(nframes, npoin)}}
    # ``x``/``y`` stay exactly as the file stores them: a reader that places a
    # local-coordinate mesh adds the origin it recovers from the domain bbox,
    # and applying it here would double the offset.
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
        # Read by POSITION, one seek per frame: the reader's own lookup keys on
        # the dictionary id it resolved the name to, and a result carrying two
        # variables the dictionary gives the same id (appended tracers) would
        # then return one of them twice.
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
            # The vertical shape a 3D result carries. A 3D field is flat over
            # NPOIN3 and is NPLAN planes stacked over the 2D mesh, bottom first;
            # a 2D file reports one plane and the same mesh twice.
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
    # A file whose header opens and whose frames do not is unreadable too, and
    # says so by the same name rather than returning a short series.
    except Exception as failure:
        raise SelafinReadError(
            f"the SELAFIN reader could not read {slf.name}: {failure}") from failure
    finally:
        reader.__exit__(None, None, None)
