"""In-container writer for the TELEMAC geometry pair.

Runs INSIDE the image where ``telapy`` and ``pretel`` live, importing nothing
from trid3nt. The geometry's IPOBO and the contours the ``.cli`` is numbered
from come from ONE walk of ONE connectivity: the two files are one artifact."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

import numpy as np  # noqa: E402

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
    "rating_curve": (KENT, KSORT, KSORT, KSORT),
    "free_exit": (KSORT, KSORT, KSORT, KSORT),
}

#: The role whose quad prescribes NOTHING by design. A steering author reads it
#: to tell a face that states no condition from a face whose two files disagree,
#: which are the same string in :func:`_prescribes` and opposite intentions.
FREE_EXIT_ROLE = "free_exit"

#: The role whose prescribed level comes from a STAGE-DISCHARGE CURVE. The quad
#: is the outflow's, because ``bord.f`` reads the curve only where
#: ``LIHBOR = KENT``; what the role adds is WHERE that level comes from, which
#: the quad cannot say and the steering author has to know: a face carrying this
#: role owes ``STAGE-DISCHARGE CURVES = 1`` at its number and a curve file, and a
#: face carrying ``outflow`` owes a constant in ``PRESCRIBED ELEVATIONS``.
RATING_CURVE_ROLE = "rating_curve"


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


def _load(path: str):
    npz = np.load(path)
    x = np.asarray(npz["x"], dtype=float)
    y = np.asarray(npz["y"], dtype=float)
    ikle = np.asarray(npz["ikle"], dtype=np.int64)
    bottom = np.asarray(npz["bottom"], dtype=float)
    return x, y, ikle, (bottom if bottom.shape[0] == x.shape[0] else None)


class _Refusal(Exception):
    """A typed refusal the HOST re-raises under its own code.

    What the engine's own boundary walk returned is knowable only here."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.document = {"code": code, "message": message}


#: How many shared nodes the refusal spells out before it counts the rest.
_NAMED_PINCH_NODES = 12


def _refuse_shared_ring_nodes(contours) -> None:
    """Refuse a walk whose rings share nodes, naming them and how many there are.

    IPOBO is a permutation of 1..NPTFR: one position per boundary node."""
    # A node two rings both walk through would need two positions, gets the
    # second, and the successor array built from the contour lengths is then
    # longer than the boundary itself - an index error several functions later,
    # and a silently misnumbered boundary if it were not. The walk that decides
    # this is the engine's own: a host-side walk of the same cells returns
    # different rings.
    seen: dict = {}
    for ring in contours:
        for node in ring:
            seen[int(node)] = seen.get(int(node), 0) + 1
    shared = sorted(node for node, count in seen.items() if count > 1)
    if not shared:
        return
    rows = sum(len(ring) for ring in contours)
    named = ", ".join(str(n) for n in shared[:_NAMED_PINCH_NODES])
    more = ("" if len(shared) <= _NAMED_PINCH_NODES
            else f" (and {len(shared) - _NAMED_PINCH_NODES} more)")
    raise _Refusal(
        "MESH_BOUNDARY_PINCHED",
        f"the boundary walk returned {len(contours)} rings over {rows} rows but "
        f"only {len(seen)} distinct nodes: {len(shared)} node(s) - {named}{more} "
        "- lie on more than one ring, so the domain is pinched to a point there "
        "and has no IPOBO permutation to be numbered by. It is a domain the "
        "extent cut into pieces that touch rather than a mesh defect: cut it "
        "from a shoreline that resolves the ask, or narrow the extent to the "
        "water body the question is about.")


def _boundary(x, y, ikle):
    """pretel's own boundary walk -> ``(ipobo, contours)``, which agree by construction.

    Every node on the walk is numbered once, in walk order."""
    # Imported inside the walk: pretel exists only inside the image.
    from pretel.meshes import get_ipobo

    _, pbounds = get_ipobo(x, y, np.asarray(ikle, dtype=np.int32), debug=False)
    contours = [[int(n) for n in ring] for ring in pbounds]
    _refuse_shared_ring_nodes(contours)
    # ``get_ipobo`` returns the closed contours it walked (each already stripped
    # of its repeated closing node) and an IPOBO numbered along them with ONE
    # count continuing across contours - which is what TELEMAC's permutation of
    # 1..NPTFR means, and what a per-contour count would break. Its own array
    # numbers ``contour[1:]``, leaving the first node of every contour at 0
    # where TELEMAC would read a boundary node as interior, so the walk here is
    # the authority.
    ipobo = np.zeros(len(x), dtype=np.int32)
    position = 0
    for ring in contours:
        for node in ring:
            position += 1
            ipobo[node] = position
    return ipobo, contours


def _contour_runs(bnodes, contours) -> int:
    """How many CONTIGUOUS stretches the written row order breaks the contours into.

    One run per contour: interleaved rows describe a boundary no walk produces."""
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


def _roles_by_node(roles: dict) -> dict:
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

    run straddling a contour's first row is ONE boundary to the engine."""
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


def write_pair(cfg: dict) -> dict:
    from telapy.api.hermes import BND_POINT, TRIANGLE, HermesFile

    x, y, ikle, bottom = _load(cfg["mesh_npz"])
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
    role_of = _roles_by_node(cfg.get("roles") or {})
    codes = np.array([_ROLE_CODES[role_of.get(int(n), "wall")] for n in bnodes],
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

    lengths = [len(c) for c in contours]
    runs = _liquid_boundaries(x, y, bnodes, codes, lengths)
    numliq = _numliq(runs, _successors(lengths), nptfr)
    rows = [[k for k in range(nptfr) if numliq[k] == number]
            for number in range(1, len(runs) + 1)]
    return {"npoin": npoin, "nelem": int(ikle.shape[0]), "nptfr": nptfr,
            "liquid_nodes": len(role_of), "n_liquid_boundaries": len(runs),
            "liquid_boundary_roles": [
                _joined([role_of.get(int(bnodes[k]), "wall") for k in here])
                for here in rows],
            # What each numbered boundary PRESCRIBES, read off the quad written
            # for it - the half of the cross-file contract the steering
            # author reads.
            "liquid_boundary_prescribes": [
                _joined([_prescribes(codes[k]) for k in here]) for here in rows],
            "n_contours": len(contours),
            "ipobo_distinct": distinct, "ipobo_max": int(ipobo.max()),
            "ipobo_is_permutation": bool(distinct == nptfr
                                         and int(ipobo.max()) == nptfr),
            "contour_lengths": [len(c) for c in contours],
            "cli_contour_runs": _contour_runs(bnodes, contours)}


def main() -> int:
    # Contract (host <-> container over the mounted /data dir):
    #   python selafin_cli_driver.py /data/config.json /data
    #
    #   config: mesh_npz (x, y npoin; ikle (nelem,3) 0-based; bottom npoin
    #           positive up, empty for a bed-less mesh), geo_slf, cli, title,
    #           roles ({role: [node index, ...]} - every boundary node not named
    #           is a wall). Emits the two files plus /data/selafin_cli_stats.json,
    #           whose stats carry the MEASURED liquid-boundary order and what
    #           each numbered boundary prescribes.
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    try:
        stats = write_pair(cfg)
    except _Refusal as refusal:
        json.dump(refusal.document,
                  open(out + "/selafin_cli_refusal.json", "w"), indent=2)
        print("SELAFIN_CLI_REFUSED", json.dumps(refusal.document))
        return 3
    json.dump(stats, open(out + "/selafin_cli_stats.json", "w"), indent=2)
    print("SELAFIN_CLI_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
