#!/usr/bin/env python3
"""Extract the Muncie 6.x 2D flow-area mesh + GUI-computed subgrid property
tables from the shipped plan HDF (the ground truth for the transplant
experiment, ADR-transplant). Pure h5py; writes a portable .npz + a text
summary. No server code.

The 2D area "2D Interior Area" is the Muncie White River flow area:
constant Manning n=0.06, 50x50 ft nominal spacing, tolerances recorded in
/Geometry/2D Flow Areas/Attributes (Cell Vol Tol 0.01, Face Conv Ratio 0.02,
Face Profile/Area Tol 0.01, Cell Min Area Fraction 0.01, Laminar Depth 0.2).
"""
import sys, json
import h5py, numpy as np

SRC = sys.argv[1] if len(sys.argv) > 1 else \
    "workers/hecras/fixtures/muncie_smoke/wrk_source/Muncie.p04.tmp.hdf"
OUT = sys.argv[2] if len(sys.argv) > 2 else \
    "scratchpad/transplant_proofs/muncie_mesh.npz"

AREA = "/Geometry/2D Flow Areas/2D Interior Area"

def main():
    f = h5py.File(SRC, "r")
    g = f[AREA]
    d = {}
    # mesh definition
    d["cells_center"]       = g["Cells Center Coordinate"][()]            # (Ncell,2)
    d["cells_manning"]      = g["Cells Center Manning's n"][()]           # (Ncell,)
    d["cells_facepoint_idx"]= g["Cells FacePoint Indexes"][()]           # (Ncell,7) -1 padded
    d["cells_min_elev"]     = g["Cells Minimum Elevation"][()]           # (Ncell,)
    d["cells_surface_area"] = g["Cells Surface Area"][()]                # (Ncell,)
    d["facepoints_coord"]   = g["FacePoints Coordinate"][()]             # (Nfp,2)
    d["facepoints_is_perim"]= g["FacePoints Is Perimeter"][()]           # (Nfp,)
    d["faces_facepoint_idx"]= g["Faces FacePoint Indexes"][()]           # (Nface,2)
    d["faces_cell_idx"]     = g["Faces Cell Indexes"][()]                # (Nface,2)
    d["faces_min_elev"]     = g["Faces Minimum Elevation"][()]           # (Nface,)
    d["faces_perim_info"]   = g["Faces Perimeter Info"][()]              # (Nface,2)
    d["faces_perim_values"] = g["Faces Perimeter Values"][()]            # (?,2)
    d["perimeter"]          = g["Perimeter"][()]                         # (Nper,2)
    # GUI-computed subgrid property tables (GROUND TRUTH)
    d["cell_vol_info"]      = g["Cells Volume Elevation Info"][()]       # (Ncell,2) start,count
    d["cell_vol_values"]    = g["Cells Volume Elevation Values"][()]     # (M,2)
    d["face_area_info"]     = g["Faces Area Elevation Info"][()]         # (Nface,2)
    d["face_area_values"]   = g["Faces Area Elevation Values"][()]       # (K,4)

    # projection + attributes
    attrs = g.parent["Attributes"][()]
    proj_wkt = f.attrs.get("Projection", b"")
    d["proj_wkt"] = np.frombuffer(proj_wkt if isinstance(proj_wkt, bytes) else bytes(proj_wkt), dtype=np.uint8)

    np.savez(OUT, **{k: v for k, v in d.items() if isinstance(v, np.ndarray)})

    # summary
    ncell = d["cells_center"].shape[0]
    nface = d["faces_facepoint_idx"].shape[0]
    print(f"[extract] source: {SRC}")
    print(f"[extract] cells={ncell} facepoints={d['facepoints_coord'].shape[0]} faces={nface} perim_pts={d['perimeter'].shape[0]}")
    print(f"[extract] Attributes: {attrs}")
    print(f"[extract] cell min elev: [{d['cells_min_elev'].min():.3f}, {d['cells_min_elev'].max():.3f}]")
    print(f"[extract] mesh extent X: [{d['facepoints_coord'][:,0].min():.1f}, {d['facepoints_coord'][:,0].max():.1f}]")
    print(f"[extract] mesh extent Y: [{d['facepoints_coord'][:,1].min():.1f}, {d['facepoints_coord'][:,1].max():.1f}]")
    # inspect table column ordering for cell 0
    s0, c0 = d["cell_vol_info"][0]
    print(f"[extract] cell0 vol/elev info start={s0} count={c0}")
    print(f"[extract]   first rows (col0,col1):\n{d['cell_vol_values'][s0:s0+min(c0,5)]}")
    fs0, fc0 = d["face_area_info"][0]
    print(f"[extract] face0 area/elev info start={fs0} count={fc0}")
    print(f"[extract]   first rows (elev,area,wp,mann):\n{d['face_area_values'][fs0:fs0+min(fc0,5)]}")
    print(f"[extract] wrote {OUT}")

if __name__ == "__main__":
    main()
