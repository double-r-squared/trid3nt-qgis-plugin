"""The element guard: what a solve is handed has positive area in the engine's own precision."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.tools.mesh.shared.nodes import MeshNodeError, checked_triangles
from trid3nt_server.workflows.telemac.authoring import mesh_files

_SQUARE = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
_TWO = np.array([[0, 1, 2], [0, 2, 3]])


def test_clean_mesh_is_untouched():
    cells, flipped = checked_triangles(_SQUARE, _TWO)
    assert flipped == 0 and (cells == _TWO).all()


def test_clockwise_element_is_reoriented():
    cells, flipped = checked_triangles(_SQUARE, np.array([[0, 2, 1], [0, 2, 3]]))
    assert flipped == 1
    a, b, c = _SQUARE[cells[0]]
    assert (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]) > 0


def test_folded_element_is_refused():
    pts = np.vstack([_SQUARE, [[9.0, 1.0]]])
    cells = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 4]])
    with pytest.raises(MeshNodeError) as err:
        checked_triangles(pts, cells)
    assert err.value.error_code == "MESH_ELEMENTS_INVERTED"
    assert "element 3" in str(err.value) or "element 1" in str(err.value)


def test_sliver_is_refused_only_in_the_precision_that_loses_it():
    pts = np.array([[500000.0, 5000000.0], [500003.0, 5000000.0],
                    [500001.5, 5000000.2]])
    cells = np.array([[0, 1, 2]])
    assert checked_triangles(pts, cells)[1] == 0
    with pytest.raises(MeshNodeError, match="float32"):
        checked_triangles(pts, cells, stored_as="float32")


def _write_2dm(path, points, cells):
    rows = ["MESH2D"] + [f"E3T {i + 1} {a + 1} {b + 1} {c + 1} 1"
                         for i, (a, b, c) in enumerate(cells)]
    rows += [f"ND {i + 1} {x} {y} 0.0" for i, (x, y) in enumerate(points)]
    path.write_text("\n".join(rows) + "\n")


def _art(path, files=None):
    return MeshArtifact(mesh_id="M", name="m", mode="om2d", display_uri=str(path),
                        crs_authid="EPSG:32610", has_bathymetry=True, node_count=4,
                        element_count=2, bbox=(0, 0, 1, 1), engine_files=files or {})


def test_guard_runs_on_a_cached_mesh_and_on_a_fresh_one(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh_files, "_stage", lambda _id, local: str(local))
    monkeypatch.setattr(mesh_files, "_record", lambda *_: None)
    pts = np.array([[500000.0, 5000000.0], [500003.0, 5000000.0],
                    [500001.5, 5000000.2], [500001.5, 5000010.0]])
    display = tmp_path / "mesh.2dm"
    _write_2dm(display, pts, [[0, 1, 2], [0, 2, 3]])
    for files in ({}, {"mesh.slf": "s3://b/mesh.slf", "mesh.cli": "s3://b/mesh.cli"}):
        mesh = {"artifact": _art(display, files), "display_uri": str(display)}
        with pytest.raises(MeshNodeError):
            mesh_files._mesh_files(mesh)
