"""In-container writer for the TELEMAC geometry pair.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place ``telapy`` and
``pretel`` are installed. The host mounts this file and a rundir and shells it;
nothing here imports trid3nt code.

  python selafin_cli_driver.py /data/config.json /data

Config keys: ``mesh_npz`` (``x``, ``y`` npoin; ``ikle`` (nelem,3) 0-based;
``bottom`` npoin positive up, empty for a bed-less mesh), ``geo_slf``, ``cli``,
``title``, ``roles`` (``{role: [node index, ...]}`` - every boundary node not
named is a wall). Emits the two files plus ``/data/selafin_cli_stats.json``.

The stats carry the MEASURED liquid-boundary order: TELEMAC numbers its liquid
boundaries by walking the contours, and a deck states PRESCRIBED FLOWRATES and
PRESCRIBED ELEVATIONS in that numbering. Reading the numbering off the boundary
file that was just written is what lets a deck be authored ONCE, against the
order the solver will actually use, instead of being probed for by a throwaway
solve.

The IPOBO written into the geometry and the contours the ``.cli`` is numbered
from come from ONE walk of ONE connectivity, which is what makes the two files
one artifact.

``Conlim.put_content`` does not round-trip a ``.cli`` written by ``set_bnd`` - it
drops rows and the NUMLIQ column - so Conlim is used for its numbering and the
file itself is hermes's.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

import numpy as np  # noqa: E402

from pretel.meshes import get_ipobo  # noqa: E402

#: Boundary role -> the TELEMAC ``(LIHBOR, LIUBOR, LIVBOR, LITBOR)`` quad that
#: states it. An inflow prescribes velocity and tracer and leaves the depth free;
#: an outflow and an open sea boundary make the SAME statement to the solver - a
#: prescribed water level, free velocity - and are named apart because the
#: measured liquid-boundary order is what a deck author reads to decide which
#: boundary carries a flowrate and which a level.
_ROLE_CODES = {
    "wall": (2, 2, 2, 2),
    "inflow": (4, 5, 5, 5),
    "outflow": (5, 4, 4, 4),
    "open": (5, 4, 4, 4),
}


def _load(path: str):
    npz = np.load(path)
    x = np.asarray(npz["x"], dtype=float)
    y = np.asarray(npz["y"], dtype=float)
    ikle = np.asarray(npz["ikle"], dtype=np.int64)
    bottom = np.asarray(npz["bottom"], dtype=float)
    return x, y, ikle, (bottom if bottom.shape[0] == x.shape[0] else None)


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


def _roles_by_node(roles: dict) -> dict:
    """``{role: [node, ...]}`` -> ``{node: role}``, refusing a node claimed twice.

    A node named by two roles has no boundary condition anyone can write, and
    picking one silently would put a flowrate on a stretch the caller meant to
    hold at a level.
    """
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


def _numliq_roles(bnd, role_of: dict) -> list:
    """The role of each liquid boundary, in TELEMAC's own numbering.

    ``set_numliq`` wrote a liquid-boundary number onto every liquid row of the
    ``.cli``; joining that column to the roles the caller named is the MEASURED
    order a deck is authored against. A boundary whose rows carry more than one
    role is reported as the joined name rather than resolved - it means one
    contiguous liquid stretch was asked to be two things.
    """
    import numpy as np

    numliq = np.asarray(bnd.por["lq"], dtype=int)
    nodes = np.asarray(bnd.bor["n"], dtype=int) - 1
    order = []
    for index in range(1, int(bnd.nfrliq) + 1):
        here = sorted({role_of.get(int(n), "wall")
                       for n in nodes[numliq == index]})
        order.append("+".join(here) if here else "wall")
    return order


def write_pair(cfg: dict) -> dict:
    from data_manip.formats.conlim import Conlim
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

    # Conlim.set_numliq walks a contour looking for its first solid node and
    # indexes past the end when there is none, so a fully-liquid contour is named
    # here rather than surfacing as an upstream IndexError.
    for index, contour in enumerate(contours):
        if all(role_of.get(int(n), "wall") != "wall" for n in contour):
            raise ValueError(
                f"boundary contour {index} ({len(contour)} nodes) carries no wall "
                "node; a TELEMAC domain needs at least one solid node per contour "
                "to number its liquid boundaries")
    bnd = Conlim(cfg["cli"])
    bnd.set_numliq(contours)
    return {"npoin": npoin, "nelem": int(ikle.shape[0]), "nptfr": nptfr,
            "liquid_nodes": len(role_of), "n_liquid_boundaries": int(bnd.nfrliq),
            "liquid_boundary_roles": _numliq_roles(bnd, role_of),
            "n_contours": len(contours),
            "ipobo_distinct": distinct, "ipobo_max": int(ipobo.max()),
            "ipobo_is_permutation": bool(distinct == nptfr
                                         and int(ipobo.max()) == nptfr),
            "contour_lengths": [len(c) for c in contours],
            "cli_contour_runs": _contour_runs(bnodes, contours)}


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    stats = write_pair(cfg)
    json.dump(stats, open(out + "/selafin_cli_stats.json", "w"), indent=2)
    print("SELAFIN_CLI_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
