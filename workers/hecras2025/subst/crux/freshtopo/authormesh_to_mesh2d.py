#!/usr/bin/env python3
"""Adapt an AuthorMesh full-topology + subgrid-tables dump into Mesh2D/SubgridTables.

The C# ``AuthorMesh`` (OI-B, c1) dumps a fresh mesh's raw topology (Faces
cellA/cellB/fpA/fpB, FacePoint coords, per-cell face lists, normals, perimeter) and
-- over a real terrain -- the ``MeshPropertyTables.ComputeFrom`` subgrid curves.
This adapter reconstructs a ``MuncieGeom``-shaped structure from that dump and runs
the SAME proven ``carve_muncie.carve`` convention rebuild (ghost synthesis on the
374 ``cellB == -1`` external faces, Cells Face + Orientation, FacePoints CCW
adjacency, the walked perimeter) so the C# AuthorMesh path feeds the identical
``hecras_geometry_writer`` inputs the carve path does.

The C# dump was verified to already carry the HEC conventions (ApiProbe + the c1
validator: interior normals point cellA->cellB, boundary normals outward,
rot_-90(fpB-fpA)==normal for 100% of faces), so this adapter is a faithful lift,
not a re-derivation. It is the LAST piece of the C# authoring path before the
fresh-C#-topology SOLVE (c2) -- the flagged risk the ADR chain named.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from carve_muncie import MuncieGeom, carve, CarveResult  # noqa: E402


def _load(d: Path):
    g = lambda n, dt, cols: np.fromfile(d / n, dt).reshape(-1, cols)  # noqa: E731
    faces = g("faces.i32", np.int32, 4)          # cellA, cellB, fpA, fpB
    fp = g("facepoints.f64", np.float64, 2)
    cc = g("cellcenters.f64", np.float64, 2)
    cfi = g("cell_face_info.i32", np.int32, 2)
    cfv = np.fromfile(d / "cell_face_vals.i32", np.int32)
    cell_info = g("cell_info.i32", np.int32, 2)
    cell_elev = np.fromfile(d / "cell_elev.f32", np.float32)
    cell_vol = np.fromfile(d / "cell_vol.f32", np.float32)
    face_info = g("face_info.i32", np.int32, 2)
    face_elev = np.fromfile(d / "face_elev.f32", np.float32)
    face_area = np.fromfile(d / "face_area.f32", np.float32)
    face_wp = np.fromfile(d / "face_wp.f32", np.float32)
    face_mann = np.fromfile(d / "face_mann.f32", np.float32)
    return dict(faces=faces, fp=fp, cc=cc, cfi=cfi, cfv=cfv, cell_info=cell_info,
                cell_elev=cell_elev, cell_vol=cell_vol, face_info=face_info,
                face_elev=face_elev, face_area=face_area, face_wp=face_wp,
                face_mann=face_mann)


def _cell_facepoints(nc: int, cc: np.ndarray, faces: np.ndarray,
                     cfi: np.ndarray, cfv: np.ndarray, fp: np.ndarray):
    """Per-cell facepoint indices, ordered CCW around the cell center, -1 padded.

    Derived from the cell's face list (dump) + each face's two facepoints.
    """
    per_cell: list[list[int]] = []
    S = 0
    for c in range(nc):
        s, n = cfi[c]
        fps: set[int] = set()
        for fj in cfv[s:s + n]:
            fps.add(int(faces[fj, 2])); fps.add(int(faces[fj, 3]))
        pts = list(fps)
        ang = [np.arctan2(*(fp[p] - cc[c])[::-1]) for p in pts]
        ordered = [pts[i] for i in np.argsort(ang)]
        per_cell.append(ordered)
        S = max(S, len(ordered))
    out = np.full((nc, S), -1, np.int32)
    for c, lst in enumerate(per_cell):
        out[c, :len(lst)] = lst
    return out


def _cell_polygon_areas(cell_fp_idx: np.ndarray, fp: np.ndarray) -> np.ndarray:
    """Shoelace area of each cell's ordered facepoint polygon (2D footprint)."""
    nc = cell_fp_idx.shape[0]
    area = np.zeros(nc, np.float32)
    for c in range(nc):
        idx = cell_fp_idx[c]
        idx = idx[idx >= 0]
        if idx.size < 3:
            continue
        xy = fp[idx]
        a = 0.5 * abs(np.sum(xy[:, 0] * np.roll(xy[:, 1], -1)
                             - np.roll(xy[:, 0], -1) * xy[:, 1]))
        area[c] = a
    return area


def load_authormesh(dump_dir: Path) -> CarveResult:
    """Build a fresh-topology CarveResult (Mesh2D + SubgridTables) from a dump."""
    d = Path(dump_dir)
    r = _load(d)
    faces, fp, cc = r["faces"], r["fp"], r["cc"]
    nc = cc.shape[0]
    # AuthorMesh marks external faces with cellB == -1 (no virtual cells); carve's
    # boundary detector expects a ghost sentinel >= nc_real (the Muncie convention,
    # and -1 would negative-index keep_real[-1] -> True -> mis-classified interior).
    # Remap every -1 to nc_real so carve synthesizes a fresh ghost per external face.
    faces[faces[:, 1] < 0, 1] = nc

    cell_fp_idx = _cell_facepoints(nc, cc, faces, r["cfi"], r["cfv"], fp)
    cell_surface_area = _cell_polygon_areas(cell_fp_idx, fp)

    # ragged subgrid -> per-cell/face curves + scalar minima (curve bottoms)
    cell_vol_values = np.column_stack([r["cell_elev"], r["cell_vol"]]).astype(np.float32)
    cell_min = np.array(
        [r["cell_elev"][s] if n > 0 else np.float32("nan")
         for s, n in r["cell_info"]], np.float32)
    faces_area_values = np.column_stack(
        [r["face_elev"], r["face_area"], r["face_wp"], r["face_mann"]]).astype(np.float32)
    faces_min = np.array(
        [r["face_elev"][s] if n > 0 else np.float32("nan")
         for s, n in r["face_info"]], np.float32)

    # Faces Perimeter Values: for a straight fresh face, the ground profile is the
    # two endpoints (the writer stores per-face breakpoints; the subgrid curve is
    # the hydraulic table, this is only the plan geometry).
    nf = faces.shape[0]
    fperim_info = np.zeros((nf, 2), np.int32)
    fperim_vals = np.empty((nf * 2, 2), np.float64)
    for i in range(nf):
        fperim_info[i] = (i * 2, 2)
        fperim_vals[i * 2] = fp[faces[i, 2]]
        fperim_vals[i * 2 + 1] = fp[faces[i, 3]]

    m = MuncieGeom(
        cell_center=cc,
        cell_manning=np.full(nc, 0.06, np.float32),
        cell_fp_idx=cell_fp_idx,
        cell_min_elev=cell_min,
        cell_surface_area=cell_surface_area,
        cell_vol_info=r["cell_info"].astype(np.int32),
        cell_vol_values=cell_vol_values,
        fp_coord=fp,
        faces_cell=faces[:, :2].astype(np.int32),          # cellA, cellB (-1 boundary)
        faces_fp=faces[:, 2:].astype(np.int32),            # fpA, fpB
        faces_min_elev=faces_min,
        faces_low_centroid=faces_min.copy(),
        faces_area_info=r["face_info"].astype(np.int32),
        faces_area_values=faces_area_values,
        faces_perim_info=fperim_info,
        faces_perim_values=fperim_vals,
        nc_real=nc,
    )
    # keep=all -> carve rebuilds ghosts on the cellB==-1 faces + all conventions
    return carve(m, np.ones(nc, bool))


if __name__ == "__main__":
    res = load_authormesh(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    print(f"[adapter] real={res.n_real} ghost={res.n_ghost} faces={res.n_faces} "
          f"fp={res.n_fp} perimeter={res.mesh.perimeter.shape[0]}")
