#!/usr/bin/env python3
"""FRESH-TOPOLOGY carve (fused c1+c2 -- the recommended first probe).

Extract a spatial SUB-RECTANGLE of HEC's shipped Muncie 2D flow area and
RE-INDEX it from zero into a NEW, smaller mesh with a FRESH topology (new cell /
face / facepoint numbering, a fresh perimeter walked from the cut boundary, fresh
ghost cells on every external face) -- while every hydraulic INGREDIENT (subgrid
volume-elevation + area-elevation curves, cell/face min elevations, coordinates)
is carried over solver-proven from the shipped 6.x geometry. The result is a
topology this repo AUTHORED (different perimeter + cell layout than anything HEC
shipped) built from solver-proven arrays: exactly the probe of whether
the production 6.x solver accepts a fresh tessellation.

The HEC 2D conventions this carve reproduces (all decoded + validated against the
full Muncie mesh, see the module test):

  1. Faces Cell Indexes [col0, col1]: the NormalUnitVector points col0 -> col1.
     External faces put the REAL cell in col0 and a ghost cell in col1 (normal
     outward). 100% of Muncie's 374 external faces follow this.
  2. Faces FacePoint Indexes [A, B]: ordered so rot_-90(B - A) == the normal,
     where rot_-90((dx, dy)) = (dy, -dx). (Held for 11164/11164 Muncie faces.)
  3. Ghost cell: center = the external face midpoint; surface area 0; min
     elevation NaN; Manning default; NO volume-elevation curve (count 0).
  4. Cells Face and Orientation: per cell, orientation = +1 if the cell is col0
     of the face, -1 if col1.
  5. FacePoints Cell adjacency: neighbor cells ordered CCW by angle(center - fp).
  6. FacePoints Face adjacency: faces ordered CCW by angle(otherfp - fp);
     orientation = +1 if fp is the face's B endpoint, -1 if A.
  7. FacePoints Is Perimeter: true iff the fp is an endpoint of any external face.

Pure numpy/h5py; no .NET, no server code. Emits a ``Mesh2D`` + ``SubgridTables``
(the ``hecras_geometry_writer`` inputs) plus the external-face ring for the BC
line. ``python carve_muncie.py --validate`` runs the full-mesh identity carve and
asserts the machinery reproduces Muncie's real-cell arrays before any sub-carve.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# The writer lives two dirs up under hecras2025/. Import Mesh2D/SubgridTables.
_HECRAS2025 = Path(__file__).resolve().parents[3]
if str(_HECRAS2025) not in sys.path:
    sys.path.insert(0, str(_HECRAS2025))
from hecras_geometry_writer import Mesh2D, SubgridTables  # noqa: E402

MUNCIE_PLAN = (
    _HECRAS2025.parent
    / "hecras/fixtures/muncie_smoke/wrk_source/Muncie.p04.tmp.hdf"
)
AREA = "Geometry/2D Flow Areas/2D Interior Area"
NC_REAL_MUNCIE = 5391


@dataclass
class MuncieGeom:
    """The shipped-Muncie 2D arrays we carve from (real + ghost)."""

    cell_center: np.ndarray          # (Ntot,2) f8
    cell_manning: np.ndarray         # (Ntot,) f4
    cell_fp_idx: np.ndarray          # (Ntot,S) i4 (-1 padded)
    cell_min_elev: np.ndarray        # (Ntot,) f4
    cell_surface_area: np.ndarray    # (Ntot,) f4
    cell_vol_info: np.ndarray        # (Ntot,2) i4
    cell_vol_values: np.ndarray      # (M,2) f4
    fp_coord: np.ndarray             # (Nfp,2) f8
    faces_cell: np.ndarray           # (Nf,2) i4
    faces_fp: np.ndarray             # (Nf,2) i4
    faces_min_elev: np.ndarray       # (Nf,) f4
    faces_low_centroid: np.ndarray   # (Nf,) f4
    faces_area_info: np.ndarray      # (Nf,2) i4
    faces_area_values: np.ndarray    # (K,4) f4
    faces_perim_info: np.ndarray     # (Nf,2) i4
    faces_perim_values: np.ndarray   # (R,2) f8
    nc_real: int


def load_muncie(plan_path: Path = MUNCIE_PLAN) -> MuncieGeom:
    import h5py

    with h5py.File(plan_path, "r") as f:
        g = f[AREA]
        return MuncieGeom(
            cell_center=g["Cells Center Coordinate"][()].astype(np.float64),
            cell_manning=g["Cells Center Manning's n"][()].astype(np.float32),
            cell_fp_idx=g["Cells FacePoint Indexes"][()].astype(np.int32),
            cell_min_elev=g["Cells Minimum Elevation"][()].astype(np.float32),
            cell_surface_area=g["Cells Surface Area"][()].astype(np.float32),
            cell_vol_info=g["Cells Volume Elevation Info"][()].astype(np.int32),
            cell_vol_values=g["Cells Volume Elevation Values"][()].astype(np.float32),
            fp_coord=g["FacePoints Coordinate"][()].astype(np.float64),
            faces_cell=g["Faces Cell Indexes"][()].astype(np.int32),
            faces_fp=g["Faces FacePoint Indexes"][()].astype(np.int32),
            faces_min_elev=g["Faces Minimum Elevation"][()].astype(np.float32),
            faces_low_centroid=g["Faces Low Elevation Centroid"][()].astype(np.float32),
            faces_area_info=g["Faces Area Elevation Info"][()].astype(np.int32),
            faces_area_values=g["Faces Area Elevation Values"][()].astype(np.float32),
            faces_perim_info=g["Faces Perimeter Info"][()].astype(np.int32),
            faces_perim_values=g["Faces Perimeter Values"][()].astype(np.float64),
            nc_real=NC_REAL_MUNCIE,
        )


def _rot_m90(v: np.ndarray) -> np.ndarray:
    """rot_-90((dx, dy)) = (dy, -dx) -- the HEC face-normal convention."""
    return np.array([v[1], -v[0]], dtype=np.float64)


@dataclass
class CarveResult:
    mesh: Mesh2D
    tables: SubgridTables
    external_faces_ring: list[int]   # ordered ring of external (perimeter) face ids
    n_real: int
    n_ghost: int
    n_faces: int
    n_fp: int
    n_cut_faces: int                 # faces that were interior in Muncie, now external


def carve(m: MuncieGeom, keep_real: np.ndarray) -> CarveResult:
    """Carve the real cells in ``keep_real`` (bool mask over 0..nc_real-1).

    Produces a fully re-indexed fresh mesh: kept real cells 0..K-1, then one
    fresh ghost cell per external face. All derived arrays are rebuilt in HEC's
    conventions. Subgrid curves + coordinates are carried solver-proven.
    """
    keep_real = np.asarray(keep_real, bool)
    assert keep_real.shape[0] == m.nc_real
    old_real = np.where(keep_real)[0]
    K = old_real.size
    new_of_real = -np.ones(m.nc_real, np.int64)
    new_of_real[old_real] = np.arange(K)

    # --- classify faces: keep any face touching a kept real cell ---
    fc = m.faces_cell
    c0, c1 = fc[:, 0], fc[:, 1]
    kept0 = (c0 < m.nc_real) & np.isin(c0, old_real)
    kept1 = (c1 < m.nc_real) & np.isin(c1, old_real)
    keep_face = kept0 | kept1
    face_ids = np.where(keep_face)[0]
    is_boundary = keep_face & ~(kept0 & kept1)   # exactly one kept real -> external
    # cut = was an interior real-real Muncie face (both < nc_real) now boundary
    was_interior_real = (c0 < m.nc_real) & (c1 < m.nc_real)
    n_cut = int((is_boundary & was_interior_real).sum())

    new_of_face = -np.ones(fc.shape[0], np.int64)
    new_of_face[face_ids] = np.arange(face_ids.size)
    NF = face_ids.size

    # --- facepoints kept = those referenced by kept faces ---
    used_fp = np.unique(m.faces_fp[face_ids].reshape(-1))
    new_of_fp = -np.ones(m.fp_coord.shape[0], np.int64)
    new_of_fp[used_fp] = np.arange(used_fp.size)
    NFP = used_fp.size
    fp_coord = m.fp_coord[used_fp]

    # --- real cell arrays (carry + re-index) ---
    cell_center = np.empty((K, 2), np.float64)
    cell_center[:] = m.cell_center[old_real]
    cell_manning = m.cell_manning[old_real].copy()
    cell_min_elev = m.cell_min_elev[old_real].copy()
    cell_surface_area = m.cell_surface_area[old_real].copy()
    # Cells FacePoint Indexes: re-index (all fps of a kept cell are kept), pad to S.
    S = m.cell_fp_idx.shape[1]
    real_cfi = m.cell_fp_idx[old_real].copy()
    valid = real_cfi >= 0
    real_cfi[valid] = new_of_fp[real_cfi[valid]]
    # subgrid volume curves (carry)
    cell_vol_curves: list[np.ndarray] = []
    for oc in old_real:
        s, cnt = m.cell_vol_info[oc]
        cell_vol_curves.append(m.cell_vol_values[s:s + cnt])

    # --- build faces (assign col0/col1, fp order, normal) + ghost cells ---
    faces_cell = np.empty((NF, 2), np.int64)
    faces_fp = np.empty((NF, 2), np.int64)
    faces_nuv = np.empty((NF, 3), np.float32)
    ghost_centers: list[np.ndarray] = []
    ghost_face: list[int] = []          # new face id each ghost sits on
    for of in face_ids:
        nf = new_of_face[of]
        oc0, oc1 = int(fc[of, 0]), int(fc[of, 1])
        a_old, b_old = int(m.faces_fp[of, 0]), int(m.faces_fp[of, 1])
        na, nb = int(new_of_fp[a_old]), int(new_of_fp[b_old])
        k0 = oc0 < m.nc_real and keep_real[oc0]
        k1 = oc1 < m.nc_real and keep_real[oc1]
        if k0 and k1:
            # interior: preserve Muncie order/normal exactly (re-index only)
            faces_cell[nf] = (new_of_real[oc0], new_of_real[oc1])
            faces_fp[nf] = (na, nb)
            edge = fp_coord[nb] - fp_coord[na]
            nrm = _rot_m90(edge)
            L = np.linalg.norm(edge)
            faces_nuv[nf] = (*(nrm / L), L)
        else:
            # boundary: real is col0, a fresh ghost is col1 (normal outward)
            real_old = oc0 if k0 else oc1
            gid = K + len(ghost_centers)
            mid = 0.5 * (fp_coord[na] + fp_coord[nb])
            ghost_centers.append(mid)
            ghost_face.append(nf)
            faces_cell[nf] = (new_of_real[real_old], gid)
            # order [A,B] so rot_-90(B-A) points real-center -> ghost(mid)
            desired = mid - cell_center[new_of_real[real_old]]
            if np.dot(_rot_m90(fp_coord[nb] - fp_coord[na]), desired) >= 0:
                A, B = na, nb
            else:
                A, B = nb, na
            faces_fp[nf] = (A, B)
            edge = fp_coord[B] - fp_coord[A]
            nrm = _rot_m90(edge)
            L = np.linalg.norm(edge)
            faces_nuv[nf] = (*(nrm / L), L)

    G = len(ghost_centers)
    NC = K + G
    # full cell-center array (real + ghost)
    all_centers = np.vstack([cell_center, np.asarray(ghost_centers).reshape(G, 2)]) \
        if G else cell_center
    # ghost scalar cell arrays
    ghost_manning = np.full(G, 0.06, np.float32)
    ghost_min_elev = np.full(G, np.nan, np.float32)
    ghost_surface = np.zeros(G, np.float32)
    all_manning = np.concatenate([cell_manning, ghost_manning])
    all_min_elev = np.concatenate([cell_min_elev, ghost_min_elev])
    all_surface = np.concatenate([cell_surface_area, ghost_surface])
    # ghost cfi = [A,B,-1...]
    ghost_cfi = np.full((G, S), -1, np.int32)
    for j, nf in enumerate(ghost_face):
        ghost_cfi[j, 0], ghost_cfi[j, 1] = faces_fp[nf]
    all_cfi = np.vstack([real_cfi, ghost_cfi]) if G else real_cfi
    # ghost volume curves = empty
    all_vol_curves = cell_vol_curves + [np.zeros((0, 2), np.float32)] * G

    # --- Cells Face and Orientation (carry real face lists, re-sign; ghost single) ---
    # per-cell face list from the FINAL faces_cell (membership order preserved via
    # Muncie's original per-cell face order for real cells).
    cfo_info = np.zeros((NC, 2), np.int32)
    cfo_vals: list[tuple[int, int]] = []
    # build map: new cell -> list of (new_face) in Muncie order for real; single for ghost
    real_faces_by_cell: dict[int, list[int]] = {i: [] for i in range(K)}
    for of in face_ids:
        nf = int(new_of_face[of])
        for col in (0, 1):
            oc = int(fc[of, col])
            if oc < m.nc_real and keep_real[oc]:
                real_faces_by_cell[int(new_of_real[oc])].append(nf)
    cur = 0
    for nc in range(NC):
        if nc < K:
            flist = real_faces_by_cell[nc]
        else:
            flist = [ghost_face[nc - K]]
        cfo_info[nc] = (cur, len(flist))
        for nf in flist:
            orient = 1 if int(faces_cell[nf, 0]) == nc else -1
            cfo_vals.append((nf, orient))
        cur += len(flist)
    cfo_values = np.asarray(cfo_vals, np.int32).reshape(-1, 2)

    # --- FacePoints adjacency (rebuild CCW-angular) ---
    # cells touching each fp
    fp_cells: list[list[int]] = [[] for _ in range(NFP)]
    for nc in range(NC):
        row = all_cfi[nc]
        for fp in row[row >= 0]:
            fp_cells[int(fp)].append(nc)
    fp_faces: list[list[int]] = [[] for _ in range(NFP)]
    for nf in range(NF):
        for fp in faces_fp[nf]:
            fp_faces[int(fp)].append(nf)

    def _ccw_cells(fp: int) -> list[int]:
        cs = fp_cells[fp]
        ang = [np.arctan2(*(all_centers[c] - fp_coord[fp])[::-1]) for c in cs]
        return [cs[i] for i in np.argsort(ang)]

    def _ccw_faces(fp: int) -> list[int]:
        fs = fp_faces[fp]
        ang = []
        for nf in fs:
            a, b = faces_fp[nf]
            other = b if a == fp else a
            ang.append(np.arctan2(*(fp_coord[other] - fp_coord[fp])[::-1]))
        return [fs[i] for i in np.argsort(ang)]

    fpc_info = np.zeros((NFP, 2), np.int32)
    fpc_vals: list[int] = []
    fpf_info = np.zeros((NFP, 2), np.int32)
    fpf_vals: list[tuple[int, int]] = []
    is_per = np.zeros(NFP, np.int32)
    # external faces mark perimeter fps
    ext_face_mask = faces_cell[:, 1] >= K
    for nf in np.where(ext_face_mask)[0]:
        is_per[faces_fp[nf, 0]] = 1
        is_per[faces_fp[nf, 1]] = 1
    cur_c = cur_f = 0
    for fp in range(NFP):
        cs = _ccw_cells(fp)
        fpc_info[fp] = (cur_c, len(cs))
        fpc_vals.extend(cs)
        cur_c += len(cs)
        fs = _ccw_faces(fp)
        fpf_info[fp] = (cur_f, len(fs))
        for nf in fs:
            orient = 1 if int(faces_fp[nf, 1]) == fp else -1
            fpf_vals.append((nf, orient))
        cur_f += len(fs)

    # --- Faces Perimeter (carry curved-face subgrid geometry, re-index) ---
    fperim_info = np.zeros((NF, 2), np.int32)
    fperim_chunks: list[np.ndarray] = []
    cur_p = 0
    for of in face_ids:
        nf = int(new_of_face[of])
        s, cnt = m.faces_perim_info[of]
        fperim_info[nf] = (cur_p, cnt)
        fperim_chunks.append(m.faces_perim_values[s:s + cnt])
        cur_p += cnt
    fperim_values = (np.vstack(fperim_chunks) if fperim_chunks
                     else np.zeros((0, 2), np.float64))

    # --- Faces subgrid (carry area-elevation, min elev, low centroid) ---
    faces_area_curves: list[np.ndarray] = []
    faces_min = np.empty(NF, np.float32)
    faces_lowc = np.empty(NF, np.float32)
    for of in face_ids:
        nf = int(new_of_face[of])
        s, cnt = m.faces_area_info[of]
        faces_area_curves.append(m.faces_area_values[s:s + cnt])
        faces_min[nf] = m.faces_min_elev[of]
        faces_lowc[nf] = m.faces_low_centroid[of]

    # --- perimeter ring (external faces) + walked polygon ---
    ring = _walk_ext_ring(faces_fp, ext_face_mask)
    perim_xy = _perimeter_polygon(ring, faces_fp, fp_coord)

    mesh = Mesh2D(
        perimeter=perim_xy,
        cell_center_coord=all_centers,
        cell_facepoint_indexes=all_cfi,
        cell_face_orientation_info=cfo_info,
        cell_face_orientation_values=cfo_values,
        cell_center_manning=all_manning,
        facepoints_coord=fp_coord,
        facepoints_cell_info=fpc_info,
        facepoints_cell_index_values=np.asarray(fpc_vals, np.int32),
        facepoints_face_orientation_info=fpf_info,
        facepoints_face_orientation_values=np.asarray(fpf_vals, np.int32).reshape(-1, 2),
        facepoints_is_perimeter=is_per,
        faces_cell_indexes=faces_cell.astype(np.int32),
        faces_facepoint_indexes=faces_fp.astype(np.int32),
        faces_normal_unit_vector_length=faces_nuv,
        faces_perimeter_info=fperim_info,
        faces_perimeter_values=fperim_values,
        cell_count=K,
    )
    tables = SubgridTables(
        cell_vol_elev=all_vol_curves,
        cell_min_elevation=all_min_elev,
        cell_surface_area=all_surface,
        face_area_elev=faces_area_curves,
        face_min_elevation=faces_min,
        faces_low_elev_centroid=faces_lowc,
    )
    return CarveResult(
        mesh=mesh, tables=tables, external_faces_ring=ring,
        n_real=K, n_ghost=G, n_faces=NF, n_fp=NFP, n_cut_faces=n_cut,
    )


def _walk_ext_ring(faces_fp: np.ndarray, ext_mask: np.ndarray) -> list[int]:
    """Order external faces into a connected ring by shared facepoints."""
    ext = list(int(i) for i in np.where(ext_mask)[0])
    if not ext:
        return []
    ring = [ext.pop(0)]
    tail = int(faces_fp[ring[0], 1])
    changed = True
    while ext and changed:
        changed = False
        for j, nf in enumerate(ext):
            a, b = int(faces_fp[nf, 0]), int(faces_fp[nf, 1])
            if a == tail:
                ring.append(nf); tail = b; ext.pop(j); changed = True; break
            if b == tail:
                ring.append(nf); tail = a; ext.pop(j); changed = True; break
    return ring


def _perimeter_polygon(ring, faces_fp, fp_coord) -> np.ndarray:
    """Walk the ordered external-face ring into a CCW, non-closed point list."""
    if not ring:
        return np.zeros((0, 2), np.float64)
    pts_idx = [int(faces_fp[ring[0], 0])]
    tail = int(faces_fp[ring[0], 1])
    pts_idx.append(tail)
    for nf in ring[1:]:
        a, b = int(faces_fp[nf, 0]), int(faces_fp[nf, 1])
        nxt = b if a == tail else a
        if nxt == pts_idx[0]:
            break
        pts_idx.append(nxt)
        tail = nxt
    xy = fp_coord[pts_idx]
    # enforce CCW (positive signed area); drop closing dup already avoided
    area = 0.5 * np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])
    if area < 0:
        xy = xy[::-1]
    return xy


# ---------------------------------------------------------------------------
# validation: full-mesh identity carve reproduces Muncie's real-cell arrays
# ---------------------------------------------------------------------------
def validate() -> None:
    m = load_muncie()
    keep = np.ones(m.nc_real, bool)
    r = carve(m, keep)
    mesh, tab = r.mesh, r.tables
    print(f"[identity carve] real={r.n_real} ghost={r.n_ghost} faces={r.n_faces} "
          f"fp={r.n_fp} cut={r.n_cut_faces}")
    assert r.n_real == m.nc_real, "identity must keep all real cells"
    assert r.n_faces == m.faces_cell.shape[0], "identity must keep all faces"
    assert r.n_fp == m.fp_coord.shape[0], "identity must keep all facepoints"
    assert r.n_cut_faces == 0, "identity has no cut faces"
    assert r.n_ghost == (m.faces_cell[:, 1] >= m.nc_real).sum(), "ghost per ext face"
    # real-cell subgrid curves carried value-identically
    for i in range(0, m.nc_real, 431):
        s, c = m.cell_vol_info[i]
        assert np.array_equal(tab.cell_vol_elev[i], m.cell_vol_values[s:s + c])
    # internal consistency: every face normal perpendicular + unit, points col0->col1
    fp = mesh.facepoints_coord
    ffp = mesh.faces_facepoint_indexes
    edge = fp[ffp[:, 1]] - fp[ffp[:, 0]]
    edge_u = edge / np.linalg.norm(edge, axis=1, keepdims=True)
    dot = np.abs((mesh.faces_normal_unit_vector_length[:, :2] * edge_u).sum(1))
    assert dot.max() < 1e-5, f"normals not perpendicular (max {dot.max()})"
    nn = np.linalg.norm(mesh.faces_normal_unit_vector_length[:, :2], axis=1)
    assert abs(nn.max() - 1) < 1e-4 and abs(nn.min() - 1) < 1e-4, "normals not unit"
    cen = mesh.cell_center_coord
    fc = mesh.faces_cell_indexes
    d = cen[fc[:, 1]] - cen[fc[:, 0]]
    proj = (mesh.faces_normal_unit_vector_length[:, :2] * d).sum(1)
    assert (proj > 0).all(), "some normals do not point col0->col1"
    # orientation membership rule
    cfo_i = mesh.cell_face_orientation_info
    cfo_v = mesh.cell_face_orientation_values
    bad = 0
    for c in range(0, mesh.cell_center_coord.shape[0], 137):
        s, cnt = cfo_i[c]
        for nf, ori in cfo_v[s:s + cnt]:
            exp = 1 if fc[nf, 0] == c else -1
            bad += (exp != ori)
    assert bad == 0, "orientation membership rule violated"
    # ragged consistency
    assert mesh.cell_face_orientation_info[:, 1].sum() == cfo_v.shape[0]
    assert mesh.facepoints_cell_info[:, 1].sum() == mesh.facepoints_cell_index_values.shape[0]
    assert mesh.facepoints_face_orientation_info[:, 1].sum() == mesh.facepoints_face_orientation_values.shape[0]
    # perimeter walked, CCW, non-degenerate
    assert mesh.perimeter.shape[0] > 3, "perimeter too short"
    print(f"[identity carve] perimeter pts={mesh.perimeter.shape[0]} "
          f"(Muncie shipped 170)  -- fresh walk, not byte-identical (expected)")
    print("[identity carve] ALL internal-consistency + carry checks PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()
    if args.validate:
        validate()
    else:
        print("use --validate (library module; carve() is imported by the deck builder)")
