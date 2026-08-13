"""Minimal SELAFIN (.slf) geometry writer for the ADR 0192 mesh-front sandbox.

SANDBOX ONLY (nothing landed). Writes a single-variable 2D SELAFIN geometry
file -- the native TELEMAC-2D geometry format (GEO / SELAFIN / SERAFIN) -- from
a coastal TIN so the mesh can be handed to TELEMAC and opened via QGIS/MDAL.

Format reference: TELEMAC "SERAFIN" (single-precision, big-endian) unformatted
Fortran record layout (openTELEMAC data_manip serafin spec):
  1  TITLE                80 char
  2  NBV1, NBV2           2 int   (n_vars, n_quasi_bubble_vars=0)
  3  per-var name+unit    32 char each
  4  IPARAM[10]           10 int  (IPARAM[9]=0 -> no date record)
  5  NELEM,NPOIN,NDP,1    4 int
  6  IKLE                 NELEM*NDP int (1-indexed connectivity)
  7  IPOBO                NPOIN int (0 interior, boundary order otherwise)
  8  X                    NPOIN real
  9  Y                    NPOIN real
  10 TIME                 1 real  (t=0)
  11 per-var values       NPOIN real  (BOTTOM elevation / bathymetry)

Every record is bracketed by a 4-byte big-endian length marker (Fortran
unformatted convention). numpy only; no TELEMAC/product imports -- the file is
proved TELEMAC-valid afterwards by reading it back with the telemac worker's own
``data_manip`` SERAFIN reader.
"""

from __future__ import annotations

import struct

import numpy as np

from schism_gr3 import extract_boundary_loops  # type: ignore


def _rec(fh, payload: bytes) -> None:
    n = len(payload)
    fh.write(struct.pack(">i", n))
    fh.write(payload)
    fh.write(struct.pack(">i", n))


def _ipobo(n_points: int, cells: np.ndarray) -> np.ndarray:
    """TELEMAC IPOBO: boundary nodes numbered 1..NPTFR around each loop, 0 else."""
    ipobo = np.zeros(n_points, dtype=np.int32)
    order = 1
    for loop in extract_boundary_loops(cells):
        for node in loop:
            if ipobo[node] == 0:
                ipobo[node] = order
                order += 1
    return ipobo


def write_selafin(
    path,
    points: np.ndarray,
    cells: np.ndarray,
    z: np.ndarray,
    *,
    title: str = "TRID3NT OCEANMESH2D COASTAL TIN",
    varname: str = "BOTTOM",
    varunit: str = "M",
) -> str:
    """Write a single-variable 2D SERAFIN geometry file.

    ``points`` (N,2) X/Y (lon/lat degrees or projected metres); ``cells`` (M,3)
    0-indexed triangles; ``z`` (N,) the node field written as BOTTOM (positive-up
    elevation or bathymetry). Returns the path written.
    """
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    z = np.asarray(z, dtype=float)
    n_points = points.shape[0]
    n_elem = cells.shape[0]

    with open(path, "wb") as fh:
        _rec(fh, title.ljust(80)[:80].encode("ascii"))
        _rec(fh, struct.pack(">2i", 1, 0))
        name = (varname.ljust(16)[:16] + varunit.ljust(16)[:16]).encode("ascii")
        _rec(fh, name)
        iparam = [0] * 10
        iparam[0] = 1
        _rec(fh, struct.pack(">10i", *iparam))
        _rec(fh, struct.pack(">4i", n_elem, n_points, 3, 1))
        ikle = (cells + 1).astype(">i4").ravel()
        _rec(fh, ikle.tobytes())
        _rec(fh, _ipobo(n_points, cells).astype(">i4").tobytes())
        _rec(fh, points[:, 0].astype(">f4").tobytes())
        _rec(fh, points[:, 1].astype(">f4").tobytes())
        _rec(fh, struct.pack(">f", 0.0))
        _rec(fh, z.astype(">f4").tobytes())
    return str(path)
