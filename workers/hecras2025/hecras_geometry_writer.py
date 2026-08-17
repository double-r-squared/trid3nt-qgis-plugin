"""HEC-RAS 6.x 2D-flow-area geometry WRITER -- the net-new OI-2 component.

Serialize a 2025-authored ``Mesh`` (topology arrays from
``Geospatial.Vectors.Mesh`` -- Cells / Faces / FacePoints / CellCenters /
Perimeter) plus ``MeshPropertyTables`` subgrid curves (cell volume-elevation,
face area-elevation-WP-Manning, computed headless on Linux) into the
exact ``/Geometry/2D Flow Areas/<name>/`` schema the PRODUCTION 6.x solver
consumes.

This closes OI-2. It proved every OTHER link end-to-end on
Muncie: the 2025 path authors an arbitrary real-AOI mesh (Q1, bit-identical
centers), computes the subgrid tables headless on Linux, those VALUES
match the 6.x GUI ground truth (Q2, cell-volume corr 0.99988), and the
production 6.x ``RasGeomPreprocess`` + ``RasUnsteady`` consume externally-authored
2D tables and solve to the baseline (Q3, dWSE 0.008 ft). What did NOT
build is a writer that serializes a mesh into a FRESH geometry HDF group (its
faithful-transplant step edited Muncie's OWN topology in place). This module is
that writer.

WHY IT SUPERSEDES THE WRITE-BLOCK. ``mesh/hecras_geometry.py`` recorded
 that the WRITE direction was blocked because "nothing on the Linux
stack computes the subgrid property tables". removed exactly that
premise (``MeshPropertyTables.ComputeFrom`` runs headless on Linux under the
substituted open-source natives), so the writer is no longer dead code: its
input tables are now computable, and-Q3 proved the 6.x solver accepts
externally-authored tables.

The layout is the classic HEC ragged ``Info(start, count)`` + flat ``Values``
pattern, mapped 1:1 to HEC's shipped Muncie geometry (schema extracted, never
guessed -- see ``MUNCIE_2D_SCHEMA`` in the sibling test). Pure ``h5py`` / ``numpy``;
no .NET, no server code, offline-suite-safe (h5py/numpy are worker-image deps and
imported at module top only where the worker runs).

``write_boundary_condition_lines`` (link c3) authors the companion
``/Geometry/Boundary Condition Lines/`` group -- the forcing-entry polylines on the
mesh perimeter that a pure-2D ``.bNN`` inflow maps to positionally; schema from the
shipped pure-2D ``BaldEagleDamBrk.g09.hdf`` (``pure2d_reference/g09_hdf_schema.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

__all__ = [
    "Mesh2D",
    "SubgridTables",
    "PropertyTableOptions",
    "BoundaryConditionLine",
    "write_2d_flow_area",
    "write_boundary_condition_lines",
    "perimeter_face_run",
    "AREA_GROUP",
    "BC_LINES_GROUP",
]

#: Parent group for 2D flow areas in a 6.x geometry / plan HDF.
AREA_GROUP = "Geometry/2D Flow Areas"

#: Boundary Condition Lines group -- the forcing-entry polylines on the mesh
#: perimeter (schema from the pure-2D BaldEagle ``g09.hdf``).
BC_LINES_GROUP = "Geometry/Boundary Condition Lines"

#: Per-cell facepoint-index padding (a cell with fewer than max-sides facepoints).
FACEPOINT_PAD = -1

# The Column/Row/Units/Can-Plot string attributes HEC stamps on each dataset.
# Solver ingest does not depend on them (Q3 solved with the transplant),
# but reproducing them keeps a writer-authored HDF indistinguishable from a
# RASMapper-authored one for downstream readers (ras-commander, RASMapper preview).
_ATTRS: dict[str, dict[str, object]] = {
    "Cells Center Coordinate": {"Column": [b"X", b"Y"], "Row": b"Cell"},
    "Cells Center Manning's n": {"Can Plot": b"False", "Column": [b"Index"], "Row": b"Cell"},
    "Cells Face and Orientation Info": {"Column": [b"Starting Index", b"Count"], "Row": b"Cell"},
    "Cells Face and Orientation Values": {"Column": [b"Face Index", b"Orientation"], "Row": b"row"},
    "Cells FacePoint Indexes": {"Column": [b"Face Point Indexes"], "Row": b"Cell"},
    "Cells Minimum Elevation": {"Can Plot": b"False", "Column": [b"Minimum Elevation"], "Row": b"Cell"},
    "Cells Surface Area": {"Can Plot": b"False", "Column": [b"Surface Area"], "Row": b"Cell"},
    "Cells Volume Elevation Info": {"Can Plot": b"True", "Column": [b"Starting Index", b"Count"], "Row": b"Cell"},
    "Cells Volume Elevation Values": {"Can Plot": b"True", "Column": [b"Elevation", b"Volume"], "Row": b"row", "Units": [b"ft", b"ft^3"]},
    "FacePoints Cell Index Values": {"Column": [b"Cell Index"], "Row": b"row"},
    "FacePoints Cell Info": {"Column": [b"Starting Index", b"Count"], "Row": b"Face Point"},
    "FacePoints Coordinate": {"Column": [b"X", b"Y"], "Row": b"Face Point"},
    "FacePoints Face and Orientation Info": {"Column": [b"Start Index", b"Count"], "Row": b"Face Point"},
    "FacePoints Face and Orientation Values": {"Column": [b"Face Index", b"Orientation"], "Row": b"row"},
    "FacePoints Is Perimeter": {"Column": [b"Is On Perimeter"], "Row": b"Face Point"},
    "Faces Area Elevation Info": {"Can Plot": b"True", "Column": [b"Starting Index", b"Count"], "Row": b"Face"},
    "Faces Area Elevation Values": {"Can Plot": b"True", "Column": [b"Z", b"Area", b"Wetted Perimeter", b"Manning's n"], "Row": b"row", "Units": [b"ft", b"ft^2", b"ft", b"s/m^(1/3)"]},
    "Faces Cell Indexes": {"Column": [b"Cell 0", b"Cell 1"], "Row": b"Face"},
    "Faces FacePoint Indexes": {"Column": [b"Face Point A", b"Face Point B"], "Row": b"Face"},
    "Faces Low Elevation Centroid": {"Can Plot": b"False", "Row": b"Face"},
    "Faces Minimum Elevation": {"Can Plot": b"False", "Column": [b"Minimum Elevation"], "Row": b"Face"},
    "Faces NormalUnitVector and Length": {"Row": b"Face"},
    "Faces Perimeter Info": {"Column": [b"Start Index", b"Count"], "Row": b"Face"},
    "Faces Perimeter Values": {"Column": [b"X", b"Y"], "Row": b"row"},
    "Perimeter": {"Column": [b"X", b"Y"], "Row": b"Points"},
}


@dataclass
class PropertyTableOptions:
    """The ``PropertyTableOptions`` a 2025 ``ComputeFrom`` ran under.

    Values default to HEC's Muncie set (Cell Vol Tol 0.01, Face Conv Ratio 0.02,
    Face Profile/Area Tol 0.01, Cell Min Area Fraction 0.01, Laminar Depth 0.2,
    50 ft spacing). Written into the group ``Attributes`` compound + the parent
    ``2D Flow Areas/Attributes`` row so the deck is self-describing and a
    re-preprocess reads the same tolerances.
    """

    manning: float = 0.06
    cell_vol_tol: float = 0.01
    cell_min_area_fraction: float = 0.01
    face_profile_tol: float = 0.01
    face_area_tol: float = 0.01
    face_conv_ratio: float = 0.02
    laminar_depth: float = 0.2
    spacing_dx: float = 50.0
    spacing_dy: float = 50.0


@dataclass
class Mesh2D:
    """A 2025-authored mesh's topology arrays (model CRS, US ft), incl ghost cells.

    Field names mirror the HEC datasets 1:1. ``cell_count`` counts REAL cells
    (excludes virtual/ghost boundary cells); the arrays are sized to the full
    cell count (real + ghost) exactly as the 6.x HDF stores them.
    """

    perimeter: np.ndarray                       # (P, 2) f8, CCW + OPEN (no closing dup)
    cell_center_coord: np.ndarray               # (Nc, 2) f8
    cell_facepoint_indexes: np.ndarray          # (Nc, S) i4, FACEPOINT_PAD filled
    cell_face_orientation_info: np.ndarray      # (Nc, 2) i4 [start, count]
    cell_face_orientation_values: np.ndarray    # (M, 2) i4 [face index, orientation]
    cell_center_manning: np.ndarray             # (Nc,) f4
    facepoints_coord: np.ndarray                # (Nfp, 2) f8
    facepoints_cell_info: np.ndarray            # (Nfp, 2) i4 [start, count]
    facepoints_cell_index_values: np.ndarray    # (Q,) i4
    facepoints_face_orientation_info: np.ndarray   # (Nfp, 2) i4
    facepoints_face_orientation_values: np.ndarray # (M, 2) i4
    facepoints_is_perimeter: np.ndarray         # (Nfp,) i4
    faces_cell_indexes: np.ndarray              # (Nf, 2) i4
    faces_facepoint_indexes: np.ndarray         # (Nf, 2) i4
    faces_normal_unit_vector_length: np.ndarray # (Nf, 3) f4 [nx, ny, length]
    faces_perimeter_info: np.ndarray            # (Nf, 2) i4 [start, count]
    faces_perimeter_values: np.ndarray          # (R, 2) f8
    cell_count: int                             # REAL cell count (excludes ghosts)


@dataclass
class SubgridTables:
    """The ``MeshPropertyTables.ComputeFrom`` output, per cell/face.

    Ragged: one variable-length curve per cell/face. The writer splits these into
    HEC's ``Info(start, count)`` + flat ``Values`` layout. Scalar per-cell/face
    minimums are the curve bottoms (kept explicit -- the solver reads them
    directly).
    """

    cell_vol_elev: Sequence[np.ndarray]   # per real+ghost cell: (k, 2) f4 [elev, volume]
    cell_min_elevation: np.ndarray        # (Nc,) f4
    cell_surface_area: np.ndarray         # (Nc,) f4
    face_area_elev: Sequence[np.ndarray]  # per face: (k, 4) f4 [Z, area, wetted_perim, manning]
    face_min_elevation: np.ndarray        # (Nf,) f4
    faces_low_elev_centroid: np.ndarray   # (Nf,) f4


def _ragged(curves: Sequence[np.ndarray], ncols: int) -> tuple[np.ndarray, np.ndarray]:
    """Split a per-row list of variable-length curves into HEC Info + Values.

    Returns ``(info, values)`` where ``info`` is (n, 2) int32 ``[start, count]``
    and ``values`` is the vertically stacked (sum(count), ncols) float32 -- the
    exact ragged layout the 6.x solver reads (``sum(count) == len(values)``).
    """
    info = np.zeros((len(curves), 2), dtype=np.int32)
    chunks: list[np.ndarray] = []
    start = 0
    for i, curve in enumerate(curves):
        seg = np.asarray(curve, dtype=np.float32).reshape(-1, ncols)
        info[i] = (start, seg.shape[0])
        chunks.append(seg)
        start += seg.shape[0]
    values = (
        np.concatenate(chunks, axis=0) if chunks
        else np.zeros((0, ncols), dtype=np.float32)
    )
    return info, values


def _ds(group, name: str, data: np.ndarray) -> None:
    """Create a dataset and stamp its HEC Column/Row/Units/Can-Plot attrs."""
    d = group.create_dataset(name, data=data)
    for k, v in _ATTRS.get(name, {}).items():
        d.attrs[k] = (
            np.array(v, dtype="S") if isinstance(v, list) else v
        )


def write_2d_flow_area(
    f,
    area_name: str,
    mesh: Mesh2D,
    tables: SubgridTables,
    opts: PropertyTableOptions,
    *,
    projection_wkt: str,
    terrain_filename: str = ".\\Terrain\\Terrain.hdf",
    authored_by: str = "trid3nt hecras_geometry_writer (OI-2)",
) -> dict:
    """Serialize ``mesh`` + ``tables`` into ``f`` under ``2D Flow Areas/<area_name>``.

    ``f`` is an open ``h5py.File`` (or Group root) in ``r+`` / ``w`` mode. Writes:
    the 25 topology + subgrid datasets, the group ``Attributes`` compound, the
    parent ``2D Flow Areas/Attributes`` row, and the root ``Projection`` attr.
    Returns a small provenance dict (counts + row totals) for logging.

    The writer performs the genuine SCHEMA ASSEMBLY (ragged Info/Values splitting,
    dtype casting, the two Attributes compounds, the attr stamping) -- the topology
    arrays and subgrid curves are produced upstream by the 2025 ``Mesh`` +
    ``ComputeFrom``, which this writer does not recompute.
    """
    import h5py  # local import keeps numpy-only importers light

    if AREA_GROUP not in f:
        parent = f.create_group(AREA_GROUP)
    else:
        parent = f[AREA_GROUP]
    if area_name in parent:
        del parent[area_name]
    g = parent.create_group(area_name)

    nc = int(mesh.cell_center_coord.shape[0])
    nfp = int(mesh.facepoints_coord.shape[0])
    nf = int(mesh.faces_cell_indexes.shape[0])

    # --- topology (straight passthrough of the 2025 Mesh arrays, cast to HEC dtypes) ---
    _ds(g, "Cells Center Coordinate", np.asarray(mesh.cell_center_coord, np.float64))
    _ds(g, "Cells Center Manning's n", np.asarray(mesh.cell_center_manning, np.float32))
    _ds(g, "Cells Face and Orientation Info", np.asarray(mesh.cell_face_orientation_info, np.int32))
    _ds(g, "Cells Face and Orientation Values", np.asarray(mesh.cell_face_orientation_values, np.int32))
    _ds(g, "Cells FacePoint Indexes", np.asarray(mesh.cell_facepoint_indexes, np.int32))
    _ds(g, "Cells Surface Area", np.asarray(tables.cell_surface_area, np.float32))
    _ds(g, "FacePoints Cell Index Values", np.asarray(mesh.facepoints_cell_index_values, np.int32))
    _ds(g, "FacePoints Cell Info", np.asarray(mesh.facepoints_cell_info, np.int32))
    _ds(g, "FacePoints Coordinate", np.asarray(mesh.facepoints_coord, np.float64))
    _ds(g, "FacePoints Face and Orientation Info", np.asarray(mesh.facepoints_face_orientation_info, np.int32))
    _ds(g, "FacePoints Face and Orientation Values", np.asarray(mesh.facepoints_face_orientation_values, np.int32))
    _ds(g, "FacePoints Is Perimeter", np.asarray(mesh.facepoints_is_perimeter, np.int32))
    _ds(g, "Faces Cell Indexes", np.asarray(mesh.faces_cell_indexes, np.int32))
    _ds(g, "Faces FacePoint Indexes", np.asarray(mesh.faces_facepoint_indexes, np.int32))
    _ds(g, "Faces NormalUnitVector and Length", np.asarray(mesh.faces_normal_unit_vector_length, np.float32))
    _ds(g, "Faces Perimeter Info", np.asarray(mesh.faces_perimeter_info, np.int32))
    _ds(g, "Faces Perimeter Values", np.asarray(mesh.faces_perimeter_values, np.float64))
    _ds(g, "Perimeter", np.asarray(mesh.perimeter, np.float64))

    # --- subgrid property tables (the genuine ragged assembly) ---
    cvi, cvv = _ragged(tables.cell_vol_elev, 2)
    _ds(g, "Cells Volume Elevation Info", cvi)
    _ds(g, "Cells Volume Elevation Values", cvv)
    _ds(g, "Cells Minimum Elevation", np.asarray(tables.cell_min_elevation, np.float32))
    fai, fav = _ragged(tables.face_area_elev, 4)
    _ds(g, "Faces Area Elevation Info", fai)
    _ds(g, "Faces Area Elevation Values", fav)
    _ds(g, "Faces Minimum Elevation", np.asarray(tables.face_min_elevation, np.float32))
    _ds(g, "Faces Low Elevation Centroid", np.asarray(tables.faces_low_elev_centroid, np.float32))

    # --- group Attributes (the PropertyTableOptions + extents + provenance) ---
    xy = np.asarray(mesh.cell_center_coord, np.float64)
    extents = np.array(
        [xy[:, 0].min(), xy[:, 0].max(), xy[:, 1].min(), xy[:, 1].max()], np.float64
    )
    for k, v in {
        "Cell Volume Tolerance": np.float32(opts.cell_vol_tol),
        "Cell Minimum Area Fraction": np.float32(opts.cell_min_area_fraction),
        "Face Profile Tolerance": np.float32(opts.face_profile_tol),
        "Face Area Elevation Tolerance": np.float32(opts.face_area_tol),
        "Face Area Conveyance Ratio": np.float32(opts.face_conv_ratio),
        "Laminar Depth": np.float32(opts.laminar_depth),
        "Manning's n": np.float32(opts.manning),
        "Multiple Face Mann n": np.uint8(0),
        "Composite LC": np.uint8(0),
        "Extents": extents,
        "Terrain Filename": np.bytes_(terrain_filename),
        "Property Tables Authored By": np.bytes_(authored_by),
        "Version": np.bytes_(b"1.0"),
    }.items():
        g.attrs[k] = v

    # --- parent 2D Flow Areas/Attributes compound (one row per area) ---
    attr_dt = np.dtype([
        ("Name", "S16"), ("Mann", "<f4"), ("Multiple Face Mann n", "u1"),
        ("Composite LC", "u1"), ("Cell Vol Tol", "<f4"),
        ("Cell Min Area Fraction", "<f4"), ("Face Profile Tol", "<f4"),
        ("Face Area Tol", "<f4"), ("Face Conv Ratio", "<f4"),
        ("Laminar Depth", "<f4"), ("Spacing dx", "<f4"), ("Spacing dy", "<f4"),
        ("Shift dx", "<f4"), ("Shift dy", "<f4"), ("Cell Count", "<i4"),
    ])
    row = np.array([(
        area_name.encode()[:16], opts.manning, 0, 0, opts.cell_vol_tol,
        opts.cell_min_area_fraction, opts.face_profile_tol, opts.face_area_tol,
        opts.face_conv_ratio, opts.laminar_depth, opts.spacing_dx, opts.spacing_dy,
        np.nan, np.nan, int(mesh.cell_count),
    )], dtype=attr_dt)
    if "Attributes" in parent:
        del parent["Attributes"]
    parent.create_dataset("Attributes", data=row)

    # --- root projection (a 2D HEC-RAS model is always projected) ---
    f.attrs["Projection"] = np.bytes_(projection_wkt.encode() if isinstance(projection_wkt, str) else projection_wkt)

    return {
        "area_name": area_name,
        "cells_total": nc,
        "cells_real": int(mesh.cell_count),
        "facepoints": nfp,
        "faces": nf,
        "cell_vol_values_rows": int(cvv.shape[0]),
        "face_area_values_rows": int(fav.shape[0]),
    }


# =====================================================================
# Boundary Condition Lines (link c3) -- the forcing-entry polyline
# =====================================================================
#
# A pure-2D deck forces the flow through a named 2D BC line on the mesh
# PERIMETER (the ``.bNN`` bare ``Upstream Flow Hydrograph`` maps to it
# positionally). HEC stores the BC lines in ``/Geometry/Boundary Condition
# Lines/`` as five datasets whose schema was extracted from the shipped pure-2D
# ``BaldEagleDamBrk.g09.hdf`` (``pure2d_reference/g09_hdf_schema.json``):
#
#   Attributes      compound[Name S32, SA-2D S16, Type S8, Length f4]   (one row per line)
#   External Faces  compound[BC Line ID i4, Face Index i4, FP Start Index i4,
#                            FP End Index i4, Station Start f4, Station End f4]
#                   (each BC line -> the ordered perimeter faces it spans)
#   Polyline Info   (n, 4) i4   [pt start, pt count, part start, part count]
#   Polyline Parts  (n, 2) i4   [pt start, pt count]   (one part per line)
#   Polyline Points (P, 2) f8   (flat -- the along-line facepoint coordinates)
#
# The ``External Faces`` FP Start/End Index + Station Start/End follow the exact
# convention Muncie's ``Reference Lines/Internal Faces`` uses (verified): per
# face, the two facepoint indices in along-line order + the cumulative station
# (ft) at each. This is the same "each line -> its faces + facepoints" mapping.

_BC_ATTR_DT = np.dtype([
    ("Name", "S32"), ("SA-2D", "S16"), ("Type", "S8"), ("Length", "<f4"),
])
_BC_EXTFACE_DT = np.dtype([
    ("BC Line ID", "<i4"), ("Face Index", "<i4"),
    ("FP Start Index", "<i4"), ("FP End Index", "<i4"),
    ("Station Start", "<f4"), ("Station End", "<f4"),
])


@dataclass
class BoundaryConditionLine:
    """One 2D BC line, defined by the ORDERED perimeter faces it spans.

    ``face_indices`` is the sequence of GLOBAL external (perimeter) face indices
    the line covers, in along-line order (see ``perimeter_face_run`` for the
    default lowest-edge selection). The writer derives the facepoint ordering,
    the polyline geometry, and the cumulative stations from the mesh topology --
    the caller only chooses WHICH perimeter faces carry the forcing.
    """

    name: str
    sa_2d: str
    face_indices: Sequence[int]
    line_type: str = "External"


def _order_facepoints_along_run(
    face_indices: Sequence[int], faces_fp: np.ndarray
) -> np.ndarray:
    """Return the (k+1,) ordered facepoint indices for a run of k external faces.

    Consecutive perimeter faces share one facepoint; the run is walked so the
    shared facepoint is the seam. Raises if the given faces are not a simple
    connected chain (the honest failure -- a BC line must lie on contiguous
    perimeter faces).
    """
    fis = [int(i) for i in face_indices]
    if not fis:
        raise ValueError("a BC line needs at least one external face")
    if len(fis) == 1:
        a, b = faces_fp[fis[0]]
        return np.array([int(a), int(b)], dtype=np.int64)

    # Orient the first face against the second (the free end leads).
    f0, f1 = set(faces_fp[fis[0]].tolist()), set(faces_fp[fis[1]].tolist())
    shared01 = f0 & f1
    if len(shared01) != 1:
        raise ValueError(f"faces {fis[0]},{fis[1]} do not share exactly one facepoint")
    seam = shared01.pop()
    lead = (f0 - {seam}).pop()
    order = [lead, seam]
    for fj in fis[1:]:
        a, b = int(faces_fp[fj][0]), int(faces_fp[fj][1])
        prev = order[-1]
        if prev == a:
            nxt = b
        elif prev == b:
            nxt = a
        else:
            raise ValueError(
                f"face {fj} (fps {a},{b}) is not contiguous with the run at fp {prev}"
            )
        order.append(nxt)
    return np.asarray(order, dtype=np.int64)


def perimeter_face_run(
    mesh: "Mesh2D",
    *,
    min_elevation: np.ndarray | None = None,
    n_faces: int = 12,
    edge: str | None = None,
) -> list[int]:
    """Pick a contiguous run of external (perimeter) faces for a BC line.

    External faces are those whose Faces Cell Indexes reference a ghost cell
    (cell index >= ``cell_count``) -- HEC's boundary-face marker. The run is
    ordered around the perimeter ring and, by default, centred on the LOWEST
    ``min_elevation`` external facepoint (where inflow naturally enters); pass
    ``edge`` in {"n","s","e","w"} to force a compass side instead.

    Returns up to ``n_faces`` contiguous global face indices. Pure topology +
    (optional) elevation; no terrain, no solver.
    """
    nc = int(mesh.cell_count)
    fc = np.asarray(mesh.faces_cell_indexes)
    ext_mask = (fc[:, 0] >= nc) | (fc[:, 1] >= nc) | (fc[:, 0] < 0) | (fc[:, 1] < 0)
    ext_faces = np.where(ext_mask)[0]
    if ext_faces.size == 0:
        # Fallback: faces whose BOTH facepoints are perimeter facepoints.
        isper = np.asarray(mesh.facepoints_is_perimeter).astype(bool)
        ffp = np.asarray(mesh.faces_facepoint_indexes)
        ext_faces = np.where(isper[ffp[:, 0]] & isper[ffp[:, 1]])[0]
    if ext_faces.size == 0:
        raise ValueError("no external faces found on the mesh perimeter")

    # Order the external faces into a ring by shared facepoints.
    ffp = np.asarray(mesh.faces_facepoint_indexes)
    remaining = list(int(i) for i in ext_faces)
    ring = [remaining.pop(0)]
    a0, b0 = int(ffp[ring[0]][0]), int(ffp[ring[0]][1])
    tail = b0
    changed = True
    while remaining and changed:
        changed = False
        for j, fj in enumerate(remaining):
            a, b = int(ffp[fj][0]), int(ffp[fj][1])
            if a == tail:
                ring.append(fj); tail = b; remaining.pop(j); changed = True; break
            if b == tail:
                ring.append(fj); tail = a; remaining.pop(j); changed = True; break
    ring_arr = np.asarray(ring, dtype=np.int64)

    n = min(int(n_faces), ring_arr.size)
    coord = np.asarray(mesh.facepoints_coord)

    def _face_key_coord(fi: int) -> np.ndarray:
        a, b = ffp[fi]
        return 0.5 * (coord[a] + coord[b])

    if edge is not None:
        keys = np.array([_face_key_coord(fi) for fi in ring_arr])
        if edge in ("s", "n"):
            order_val = keys[:, 1]
            center = int(np.argmin(order_val) if edge == "s" else np.argmax(order_val))
        else:
            order_val = keys[:, 0]
            center = int(np.argmin(order_val) if edge == "w" else np.argmax(order_val))
    elif min_elevation is not None:
        fmin = np.asarray(min_elevation, dtype=np.float64)
        vals = np.array([
            np.nanmin([fmin[a] if a < fmin.size else np.inf,
                       fmin[b] if b < fmin.size else np.inf])
            for a, b in ffp[ring_arr]
        ])
        center = int(np.nanargmin(vals))
    else:
        center = 0

    half = n // 2
    start = center - half
    idx = [(start + k) % ring_arr.size for k in range(n)]
    return [int(ring_arr[i]) for i in idx]


def write_boundary_condition_lines(
    f,
    lines: Sequence[BoundaryConditionLine],
    mesh: "Mesh2D",
    *,
    replace: bool = True,
) -> dict:
    """Author ``/Geometry/Boundary Condition Lines/`` for ``lines`` on ``mesh``.

    ``f`` is an open ``h5py.File``/root in ``r+``/``w``. Writes the five datasets
    (Attributes, External Faces, Polyline Info/Parts/Points) per the g09 schema.
    The per-face facepoint ordering + cumulative stations are derived from the
    mesh's ``faces_facepoint_indexes`` + ``facepoints_coord``. Returns a
    provenance dict (per-line face counts + lengths).
    """
    import h5py  # noqa: F401  local import keeps numpy-only importers light

    faces_fp = np.asarray(mesh.faces_facepoint_indexes)
    coord = np.asarray(mesh.facepoints_coord, dtype=np.float64)

    attr_rows = []
    extface_rows = []
    poly_pts: list[np.ndarray] = []
    poly_info = []
    poly_parts = []
    prov_lines = []
    pt_cursor = 0

    for line_id, ln in enumerate(lines):
        order = _order_facepoints_along_run(ln.face_indices, faces_fp)
        pts = coord[order]                                   # (k+1, 2)
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)   # (k,)
        station = np.concatenate([[0.0], np.cumsum(seg)])    # (k+1,)
        length = float(station[-1])

        for j, fi in enumerate(ln.face_indices):
            extface_rows.append((
                line_id, int(fi), int(order[j]), int(order[j + 1]),
                float(station[j]), float(station[j + 1]),
            ))
        attr_rows.append((
            ln.name.encode()[:32], ln.sa_2d.encode()[:16],
            ln.line_type.encode()[:8], length,
        ))
        poly_pts.append(pts)
        poly_parts.append((pt_cursor, pts.shape[0]))
        poly_info.append((pt_cursor, pts.shape[0], line_id, 1))
        pt_cursor += pts.shape[0]
        prov_lines.append({"name": ln.name, "faces": len(ln.face_indices),
                           "length_ft": length})

    if BC_LINES_GROUP in f:
        if replace:
            del f[BC_LINES_GROUP]
        else:
            raise ValueError(f"{BC_LINES_GROUP} already present (replace=False)")
    g = f.create_group(BC_LINES_GROUP)
    g.create_dataset("Attributes", data=np.array(attr_rows, dtype=_BC_ATTR_DT))
    g.create_dataset("External Faces", data=np.array(extface_rows, dtype=_BC_EXTFACE_DT))
    g.create_dataset("Polyline Info", data=np.array(poly_info, dtype=np.int32))
    g.create_dataset("Polyline Parts", data=np.array(poly_parts, dtype=np.int32))
    g.create_dataset(
        "Polyline Points",
        data=(np.concatenate(poly_pts, axis=0) if poly_pts
              else np.zeros((0, 2), np.float64)),
    )
    return {"lines": prov_lines, "external_faces_total": len(extface_rows)}
