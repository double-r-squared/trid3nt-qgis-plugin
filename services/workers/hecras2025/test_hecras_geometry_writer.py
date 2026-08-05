"""Offline round-trip test for the 2D geometry writer (ADR 0132 OI-2).

Reads HEC's shipped Muncie 2D flow-area geometry (the ground truth this whole
HEC-RAS track matches bit-for-bit), rebuilds the writer's ``Mesh2D`` +
``SubgridTables`` inputs from the on-disk ragged layout, serializes them through
``write_2d_flow_area`` into a FRESH HDF, and asserts every one of the 25 topology
+ subgrid datasets reconstructs VALUE-IDENTICALLY -- the offline gate proving the
writer's schema assembly (ragged Info/Values splitting, dtype casting, the two
Attributes compounds) is faithful before any live 6.x solve.

No .NET, no docker, no network: pure h5py/numpy over the in-repo Muncie fixture.
Skips cleanly if the fixture plan HDF is absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from hecras_geometry_writer import (  # noqa: E402  (after importorskip)
    AREA_GROUP,
    BC_LINES_GROUP,
    BoundaryConditionLine,
    Mesh2D,
    PropertyTableOptions,
    SubgridTables,
    perimeter_face_run,
    write_2d_flow_area,
    write_boundary_condition_lines,
)

_MUNCIE = (
    Path(__file__).resolve().parent.parent
    / "hecras" / "fixtures" / "muncie_smoke" / "wrk_source" / "Muncie.p04.tmp.hdf"
)
_AREA = "2D Interior Area"

# The 25 datasets the writer authors (parent Attributes + Projection checked apart).
_DATASETS = [
    "Cells Center Coordinate", "Cells Center Manning's n",
    "Cells Face and Orientation Info", "Cells Face and Orientation Values",
    "Cells FacePoint Indexes", "Cells Minimum Elevation", "Cells Surface Area",
    "Cells Volume Elevation Info", "Cells Volume Elevation Values",
    "FacePoints Cell Index Values", "FacePoints Cell Info", "FacePoints Coordinate",
    "FacePoints Face and Orientation Info", "FacePoints Face and Orientation Values",
    "FacePoints Is Perimeter", "Faces Area Elevation Info",
    "Faces Area Elevation Values", "Faces Cell Indexes", "Faces FacePoint Indexes",
    "Faces Low Elevation Centroid", "Faces Minimum Elevation",
    "Faces NormalUnitVector and Length", "Faces Perimeter Info",
    "Faces Perimeter Values", "Perimeter",
]


def _ragged_to_curves(info: np.ndarray, values: np.ndarray) -> list[np.ndarray]:
    """Invert HEC Info(start, count) + Values into a per-row curve list."""
    return [values[s : s + c] for s, c in info]


def _load_muncie() -> tuple[Mesh2D, SubgridTables, str]:
    with h5py.File(_MUNCIE, "r") as f:
        g = f[f"{AREA_GROUP}/{_AREA}"]
        rd = lambda n: g[n][()]  # noqa: E731
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
            cell_vol_elev=_ragged_to_curves(cvi, cvv),
            cell_min_elevation=rd("Cells Minimum Elevation"),
            cell_surface_area=rd("Cells Surface Area"),
            face_area_elev=_ragged_to_curves(fai, fav),
            face_min_elevation=rd("Faces Minimum Elevation"),
            faces_low_elev_centroid=rd("Faces Low Elevation Centroid"),
        )
    return mesh, tables, proj


@pytest.mark.skipif(not _MUNCIE.is_file(), reason="Muncie fixture plan HDF absent")
def test_writer_reconstructs_muncie_geometry_value_identically(tmp_path):
    mesh, tables, proj = _load_muncie()
    out = tmp_path / "authored_geom.hdf"
    with h5py.File(out, "w") as f:
        prov = write_2d_flow_area(
            f, _AREA, mesh, tables,
            PropertyTableOptions(), projection_wkt=proj,
        )

    assert prov["cells_real"] == 5391
    assert prov["cells_total"] == 5765
    assert prov["faces"] == 11164

    with h5py.File(_MUNCIE, "r") as fo, h5py.File(out, "r") as fw:
        go = fo[f"{AREA_GROUP}/{_AREA}"]
        gw = fw[f"{AREA_GROUP}/{_AREA}"]
        for name in _DATASETS:
            a = np.asarray(go[name][()])
            b = np.asarray(gw[name][()])
            assert a.shape == b.shape, f"{name}: shape {a.shape} != {b.shape}"
            assert a.dtype == b.dtype, f"{name}: dtype {a.dtype} != {b.dtype}"
            assert np.array_equal(a, b, equal_nan=True), f"{name}: values differ"
        # parent Attributes compound + root Projection round-trip
        assert fw[f"{AREA_GROUP}/Attributes"][()]["Cell Count"][0] == 5391
        assert b"StatePlane" in bytes(fw.attrs["Projection"])


def _synthetic_perimeter_mesh() -> Mesh2D:
    """A tiny 5-facepoint / 4-external-face open chain for the BC-line unit test.

    Facepoints on a straight line at x=0,10,20,30,40; four faces link them in
    order; each face's outer cell is a ghost (index >= cell_count) so the
    external-face detector marks all four.
    """
    fp = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0], [30.0, 0.0], [40.0, 0.0]])
    faces_fp = np.array([[0, 1], [1, 2], [2, 3], [3, 4]], dtype=np.int32)
    faces_cell = np.array([[0, 5], [0, 5], [1, 5], [1, 5]], dtype=np.int32)  # 5 = ghost
    z = np.zeros(1)
    return Mesh2D(
        perimeter=fp, cell_center_coord=np.array([[15.0, -5.0], [30.0, -5.0]]),
        cell_facepoint_indexes=np.array([[0, 1, 2, -1], [2, 3, 4, -1]], np.int32),
        cell_face_orientation_info=np.zeros((2, 2), np.int32),
        cell_face_orientation_values=np.zeros((0, 2), np.int32),
        cell_center_manning=np.full(2, 0.06, np.float32),
        facepoints_coord=fp,
        facepoints_cell_info=np.zeros((5, 2), np.int32),
        facepoints_cell_index_values=np.zeros((0,), np.int32),
        facepoints_face_orientation_info=np.zeros((5, 2), np.int32),
        facepoints_face_orientation_values=np.zeros((0, 2), np.int32),
        facepoints_is_perimeter=np.ones(5, np.int32),
        faces_cell_indexes=faces_cell,
        faces_facepoint_indexes=faces_fp,
        faces_normal_unit_vector_length=np.zeros((4, 3), np.float32),
        faces_perimeter_info=np.zeros((4, 2), np.int32),
        faces_perimeter_values=np.zeros((0, 2), np.float64),
        cell_count=2,
    )


def test_bc_lines_schema_and_stations_synthetic(tmp_path):
    mesh = _synthetic_perimeter_mesh()
    line = BoundaryConditionLine(name="Upstream Inflow", sa_2d="TestArea",
                                 face_indices=[0, 1, 2])  # spans fp 0->1->2->3
    out = tmp_path / "bc.hdf"
    with h5py.File(out, "w") as f:
        prov = write_boundary_condition_lines(f, [line], mesh)
    assert prov["external_faces_total"] == 3
    with h5py.File(out, "r") as f:
        g = f[BC_LINES_GROUP]
        attr = g["Attributes"][()]
        assert attr.dtype.names == ("Name", "SA-2D", "Type", "Length")
        assert attr["Name"][0] == b"Upstream Inflow"
        assert attr["SA-2D"][0] == b"TestArea"
        assert abs(float(attr["Length"][0]) - 30.0) < 1e-4  # 3 faces x 10 ft
        ext = g["External Faces"][()]
        assert ext.dtype.names[0] == "BC Line ID"
        # facepoint chain 0->1->2->3, stations 0,10,20,30 monotone
        assert list(ext["FP Start Index"]) == [0, 1, 2]
        assert list(ext["FP End Index"]) == [1, 2, 3]
        assert np.allclose(ext["Station Start"], [0, 10, 20])
        assert np.allclose(ext["Station End"], [10, 20, 30])
        # polyline round-trips: Info[pt_start,pt_count,part_start,part_count]
        info = g["Polyline Info"][()]
        parts = g["Polyline Parts"][()]
        pts = g["Polyline Points"][()]
        assert info.tolist() == [[0, 4, 0, 1]]
        assert parts.tolist() == [[0, 4]]
        assert pts.shape == (4, 2)
        assert int(info[:, 1].sum()) == pts.shape[0]


@pytest.mark.skipif(not _MUNCIE.is_file(), reason="Muncie fixture plan HDF absent")
def test_bc_lines_on_real_muncie_perimeter(tmp_path):
    """Author a BC line on the REAL Muncie mesh's external faces (topology only)."""
    mesh, tables, _ = _load_muncie()
    run = perimeter_face_run(mesh, min_elevation=tables.face_min_elevation, n_faces=10)
    assert len(run) == 10
    line = BoundaryConditionLine(name="Inflow BC", sa_2d=_AREA, face_indices=run)
    out = tmp_path / "muncie_bc.hdf"
    with h5py.File(out, "w") as f:
        prov = write_boundary_condition_lines(f, [line], mesh)
        g = f[BC_LINES_GROUP]
        ext = g["External Faces"][()]
        # stations strictly increase along the run; length matches last station
        assert np.all(np.diff(ext["Station Start"]) > 0)
        assert abs(float(g["Attributes"]["Length"][0]) - float(ext["Station End"][-1])) < 1e-3
        # every referenced facepoint is a real Muncie perimeter facepoint
        isper = mesh.facepoints_is_perimeter.astype(bool)
        assert isper[ext["FP Start Index"]].all()
        assert isper[ext["FP End Index"]].all()
    assert prov["lines"][0]["faces"] == 10


@pytest.mark.skipif(not _MUNCIE.is_file(), reason="Muncie fixture plan HDF absent")
def test_ragged_info_values_are_consistent():
    """sum(count) == len(Values) for both subgrid tables after a fresh write."""
    mesh, tables, proj = _load_muncie()
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".hdf") as tf:
        with h5py.File(tf.name, "w") as f:
            write_2d_flow_area(f, _AREA, mesh, tables, PropertyTableOptions(), projection_wkt=proj)
        with h5py.File(tf.name, "r") as f:
            g = f[f"{AREA_GROUP}/{_AREA}"]
            for info_ds, val_ds in [
                ("Cells Volume Elevation Info", "Cells Volume Elevation Values"),
                ("Faces Area Elevation Info", "Faces Area Elevation Values"),
            ]:
                info = g[info_ds][()]
                assert int(info[:, 1].sum()) == g[val_ds].shape[0]
                # contiguous, non-overlapping starts
                expected_starts = np.concatenate([[0], np.cumsum(info[:, 1])[:-1]])
                assert np.array_equal(info[:, 0], expected_starts)
