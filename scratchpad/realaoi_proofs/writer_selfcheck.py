#!/usr/bin/env python3
"""Muncie product-path self-check for the geometry WRITER (ADR 0132 OI-2).

Author a FRESH /Geometry/2D Flow Areas/<area> group via write_2d_flow_area (from
Muncie's own mesh arrays -- the stand-in for a 2025 Mesh dump), splice it into a
copy of the Muncie plan HDF (replacing the RASMapper-authored group), and stage
the deck for the production 6.x solve. If the writer-authored group solves to the
ADR 0109 baseline, the writer's output is solver-valid end-to-end.

Host-side (writes the spliced deck); the solve runs in trid3nt-local/hecras:latest.
"""
import shutil
import sys
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, "/home/nate/Documents/trid3nt-local/services/workers/hecras2025")
from hecras_geometry_writer import (
    AREA_GROUP, Mesh2D, PropertyTableOptions, SubgridTables, write_2d_flow_area,
)

SRC = Path("/home/nate/Documents/trid3nt-local/services/workers/hecras/fixtures/muncie_smoke/wrk_source")
AREA = "2D Interior Area"
RUN = Path(sys.argv[1])
RUN.mkdir(parents=True, exist_ok=True)
for fp in SRC.glob("*.*"):
    shutil.copy2(fp, RUN / fp.name)

plan = RUN / "Muncie.p04.tmp.hdf"


def ragged_to_curves(info, values):
    return [values[s:s + c] for s, c in info]


with h5py.File(plan, "r") as f:
    g = f[f"{AREA_GROUP}/{AREA}"]
    rd = lambda n: g[n][()]
    cvi, cvv = rd("Cells Volume Elevation Info"), rd("Cells Volume Elevation Values")
    fai, fav = rd("Faces Area Elevation Info"), rd("Faces Area Elevation Values")
    cell_count = int(f[f"{AREA_GROUP}/Attributes"][()]["Cell Count"][0])
    proj = f.attrs["Projection"]
    proj = proj.decode() if isinstance(proj, bytes) else str(proj)
    mesh = Mesh2D(
        perimeter=rd("Perimeter"),
        cell_center_coord=rd("Cells Center Coordinate"),
        cell_facepoint_indexes=rd("Cells FacePoint Indexes"),
        cell_face_orientation_info=rd("Cells Face and Orientation Info"),
        cell_face_orientation_values=rd("Cells Face and Orientation Values"),
        cell_center_manning=rd("Cells Center Manning's n"),
        facepoints_coord=rd("FacePoints Coordinate"),
        facepoints_cell_info=rd("FacePoints Cell Info"),
        facepoints_cell_index_values=rd("FacePoints Cell Index Values"),
        facepoints_face_orientation_info=rd("FacePoints Face and Orientation Info"),
        facepoints_face_orientation_values=rd("FacePoints Face and Orientation Values"),
        facepoints_is_perimeter=rd("FacePoints Is Perimeter"),
        faces_cell_indexes=rd("Faces Cell Indexes"),
        faces_facepoint_indexes=rd("Faces FacePoint Indexes"),
        faces_normal_unit_vector_length=rd("Faces NormalUnitVector and Length"),
        faces_perimeter_info=rd("Faces Perimeter Info"),
        faces_perimeter_values=rd("Faces Perimeter Values"),
        cell_count=cell_count,
    )
    tables = SubgridTables(
        cell_vol_elev=ragged_to_curves(cvi, cvv),
        cell_min_elevation=rd("Cells Minimum Elevation"),
        cell_surface_area=rd("Cells Surface Area"),
        face_area_elev=ragged_to_curves(fai, fav),
        face_min_elevation=rd("Faces Minimum Elevation"),
        faces_low_elev_centroid=rd("Faces Low Elevation Centroid"),
    )
    # preserve Muncie's RASMapper group attrs so the self-check isolates the
    # DATASET-authoring question (solver-consumes writer datasets) from unrelated
    # provenance-attr archaeology; the fresh-group attr set is exercised separately.
    orig_attrs = dict(g.attrs)

with h5py.File(plan, "r+") as f:
    del f[f"{AREA_GROUP}/{AREA}"]
    prov = write_2d_flow_area(f, AREA, mesh, tables, PropertyTableOptions(), projection_wkt=proj)
    gw = f[f"{AREA_GROUP}/{AREA}"]
    for k, v in orig_attrs.items():
        if k not in gw.attrs:
            gw.attrs[k] = v

# M3-gate manifest shape (plan_hdf + geom_suffix; no archetype).
(RUN / "manifest.json").write_text(
    '{"plan_hdf": "Muncie.p04.tmp.hdf", "geom_suffix": "x04", "run_geompre": true}'
)
print("WRITER PROVENANCE:", prov)
print("SPLICED DECK ->", RUN)
