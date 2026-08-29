"""In-container writer for the TELEMAC geometry pair.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place ``telapy`` and
``pretel`` are installed. The host mounts this file and a rundir and shells it;
nothing here imports trid3nt code.

  python selafin_cli_driver.py /data/config.json /data

Config keys: ``mesh_npz`` (``x``, ``y`` npoin; ``ikle`` (nelem,3) 0-based;
``bottom`` npoin positive up, empty for a bed-less mesh), ``geo_slf``, ``cli``,
``title``, ``open_nodes`` (node indices a solve forces at). Emits the two files
plus ``/data/selafin_cli_stats.json``.

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

#: TELEMAC boundary-condition codes for the two classes a mesh can state: a
#: prescribed-elevation open boundary with free velocity, and a solid wall.
_OPEN = (5, 4, 4, 4)
_LAND = (2, 2, 2, 2)


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


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    stats = write_pair(cfg)
    json.dump(stats, open(out + "/selafin_cli_stats.json", "w"), indent=2)
    print("SELAFIN_CLI_OK", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
