"""The TELEMAC WRITER of the shared mesh front: a neutral mesh -> a BOTTOM SELAFIN.

The front's contract is a NEUTRAL artifact plus thin per-solver writers (the
``hecras_build`` pattern). This is TELEMAC's: nodes, triangles and a bed, written
as the single-variable geometry file the solver reads its domain from. It knows
nothing about catchments, coasts or corridors - any generation strategy that
produces the neutral triple can be written through it, which is what keeps the
strategies and the writers independent.

It lives in the mesh front rather than in the TELEMAC step tier because BOTH
callers are outside that tier's question: the standalone ``generate_mesh`` tool
writes a TELEMAC-compatible mesh artifact with it, and the rain-on-grid step
writes its solve geometry with it. A writer only one of them could reach would
be a placement leak wearing a convenience's clothes.
"""

from __future__ import annotations

import struct
from typing import Any

__all__ = ["write_bottom_selafin"]


def write_bottom_selafin(path: str, points_m: Any, cells: Any, z: Any) -> str:
    """Write the single-variable (BOTTOM) SELAFIN geometry the solver reads.

    The thin per-solver writer of the shared mesh front: the neutral artifact is
    nodes, triangles and a bed, and this is what TELEMAC wants that to look like.
    Byte-for-byte the ``selafin_io.write_selafin`` layout, so the mesh round-trips
    through MDAL and through the solver identically.
    """
    import numpy as np

    pts = np.asarray(points_m, dtype=float)
    cel = np.asarray(cells, dtype=np.int64)
    zz = np.asarray(z, dtype=float)
    n_points, n_elem = pts.shape[0], cel.shape[0]

    def _rec(fh, payload: bytes) -> None:
        n = len(payload)
        fh.write(struct.pack(">i", n))
        fh.write(payload)
        fh.write(struct.pack(">i", n))

    ipobo = _ipobo_from_cells(n_points, cel)
    with open(path, "wb") as fh:
        _rec(fh, "TRID3NT WATERSHED RAIN-ON-GRID TIN".ljust(80)[:80].encode("ascii"))
        _rec(fh, struct.pack(">2i", 1, 0))
        _rec(fh, ("BOTTOM".ljust(16)[:16] + "M".ljust(16)[:16]).encode("ascii"))
        iparam = [0] * 10
        iparam[0] = 1
        _rec(fh, struct.pack(">10i", *iparam))
        _rec(fh, struct.pack(">4i", n_elem, n_points, 3, 1))
        _rec(fh, (cel + 1).astype(">i4").ravel().tobytes())
        _rec(fh, ipobo.astype(">i4").tobytes())
        _rec(fh, pts[:, 0].astype(">f4").tobytes())
        _rec(fh, pts[:, 1].astype(">f4").tobytes())
        _rec(fh, struct.pack(">f", 0.0))
        _rec(fh, zz.astype(">f4").tobytes())
    return path


def _ipobo_from_cells(n_points: int, cells: Any) -> Any:
    """TELEMAC IPOBO: boundary nodes numbered 1..NPTFR, 0 interior.

    Boundary edges are those shared by exactly one triangle; nodes on them are
    numbered in first-seen order, which is a valid IPOBO for a single-body TIN.
    """
    import numpy as np

    cel = np.asarray(cells, dtype=np.int64)
    edges = np.vstack([cel[:, [0, 1]], cel[:, [1, 2]], cel[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    ipobo = np.zeros(n_points, dtype=np.int32)
    order = 1
    for a, b in uniq[counts == 1]:
        for node in (int(a), int(b)):
            if ipobo[node] == 0:
                ipobo[node] = order
                order += 1
    return ipobo
