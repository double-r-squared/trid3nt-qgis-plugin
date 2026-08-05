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
    Mesh2D,
    PropertyTableOptions,
    SubgridTables,
    write_2d_flow_area,
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
