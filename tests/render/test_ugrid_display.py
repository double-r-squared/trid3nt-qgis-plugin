"""The CF-UGRID display face: a mesh of polygon cells, and cell-valued
dataset groups over time, in one netCDF file MDAL opens."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.render.mesh_display import (
    MeshDisplayError,
    write_ugrid,
)
from trid3nt_server.tools.mesh.meshers import Mesh


def _polygon_mesh() -> Mesh:
    """A triangle beside a pentagon, over a painted bed."""
    pts = np.array([[0.0, 0.0], [1, 0], [2, 0], [3, 0], [4, 0], [4, 1],
                    [2, 2], [0, 1]], dtype=float)
    cells = [np.array([0, 1, 7]), np.array([2, 3, 4, 5, 6])]
    return Mesh(points=pts, cells=cells, crs_authid="EPSG:32617",
                bed=np.linspace(10.0, 17.0, pts.shape[0]),
                meta={"utm_epsg": 32617})


def test_a_ragged_mesh_writes_a_readable_ugrid_topology(tmp_path):
    import netCDF4

    path = write_ugrid(_polygon_mesh(), tmp_path / "mesh.ugrid")
    with netCDF4.Dataset(path) as ds:
        assert "UGRID" in ds.Conventions
        assert ds.dimensions["nMesh2D_nodes"].size == 8
        assert ds.dimensions["nMesh2D_faces"].size == 2
        topo = ds.variables["Mesh2D"]
        assert topo.getncattr("cf_role") == "topology_data"
        assert topo.getncattr("face_node_connectivity") == "Mesh2D_face_nodes"
        fn = ds.variables["Mesh2D_face_nodes"]
        assert fn.getncattr("start_index") == 0
        # The triangle's three rows leave the rest fill; the pentagon fills all five.
        assert list(np.asarray(fn[:, 0].filled(-1))) == [0, 1, 7, -1, -1]
        assert list(np.asarray(fn[:, 1].filled(-1))) == [2, 3, 4, 5, 6]
        x = ds.variables["Mesh2D_node_x"][:]
        assert np.allclose(x, np.asarray(_polygon_mesh().points)[:, 0])
        bed = ds.variables["Mesh2D_bed"]
        assert bed.getncattr("standard_name") == "bed_elevation"
        assert np.allclose(bed[:], np.linspace(10.0, 17.0, 8))


def test_cell_valued_dataset_groups_ride_over_time(tmp_path):
    import netCDF4

    mesh = _polygon_mesh()
    path = write_ugrid(
        mesh, tmp_path / "mesh.ugrid",
        datasets={"head": [(0.0, [101.0, 99.5]),
                           (3600.0, [100.5, 99.0])]})
    with netCDF4.Dataset(path) as ds:
        head = ds.variables["head"]
        assert head.shape == (2, 2)
        assert np.allclose(head[0, :], [101.0, 99.5])
        assert np.allclose(head[1, :], [100.5, 99.0])


def test_a_dataset_over_the_wrong_face_count_refuses(tmp_path):
    with pytest.raises(MeshDisplayError) as excinfo:
        write_ugrid(_polygon_mesh(), tmp_path / "mesh.ugrid",
                    datasets={"head": [(0.0, [1.0, 2.0, 3.0])]})
    assert excinfo.value.error_code == "MESH_DATASET_FACE_COUNT"


def test_an_unsafe_dataset_name_refuses(tmp_path):
    with pytest.raises(MeshDisplayError) as excinfo:
        write_ugrid(_polygon_mesh(), tmp_path / "mesh.ugrid",
                    datasets={"bad name!": [(0.0, [1.0, 2.0])]})
    assert excinfo.value.error_code == "MESH_UGRID_DATASET_NAME"


def test_a_cell_with_fewer_than_three_vertices_refuses(tmp_path):
    mesh = Mesh(points=np.array([[0.0, 0.0], [1, 0], [1, 1]]),
                cells=[np.array([0, 1])], crs_authid="EPSG:32617")
    with pytest.raises(MeshDisplayError) as excinfo:
        write_ugrid(mesh, tmp_path / "mesh.ugrid")
    assert excinfo.value.error_code == "MESH_UGRID_EMPTY_CELL"


def test_a_uniform_quad_mesh_also_writes_ugrid(tmp_path):
    import netCDF4

    pts = np.array([[0.0, 0.0], [1, 0], [1, 1], [0, 1]], dtype=float)
    mesh = Mesh(points=pts, cells=np.array([[0, 1, 2, 3]]),
                crs_authid="EPSG:4326")
    path = write_ugrid(mesh, tmp_path / "mesh.ugrid")
    with netCDF4.Dataset(path) as ds:
        assert ds.variables["Mesh2D_node_x"].getncattr("units") == \
            "degrees_east"
        assert ds.dimensions["max_nfaces"].size == 4
