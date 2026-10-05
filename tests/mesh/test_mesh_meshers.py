"""Offline tests for the registered meshers, the mesh artifact and its gate.

Every build is container- or network-driven and is proven live; the PURE surfaces
run here: which meshers the router registers and what each declares, the
display-face round trip, the artifact record and its engine-compat gatekeeper,
the case-scoped stash and sidecar-key derivation, and the precondition gate."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    sidecar_key_for_mesh_uri,
    stash_mesh_artifact,
    stashed_mesh_artifacts,
)
from trid3nt_server.render.mesh_display import write_2dm_arrays
from trid3nt_server.tools.mesh.meshers import (
    MeshToolError,
    get_mesher,
    registered_meshers,
)
from trid3nt_server.tools.mesh.shared.nodes import MeshNodeError, read_2dm_mesh



def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01ABC", name="Coweeta catchment", mode="om2d",
        display_uri="s3://cache/mesh/01ABC/mesh.2dm", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=(-83.5, 35.0, -83.4, 35.09),
        )
    base.update(over)
    return MeshArtifact(**base)


def test_build_mesh_registered_and_the_standalone_builder_is_gone():
    rt = TOOL_REGISTRY.get("build_mesh")
    assert rt is not None
    assert rt.metadata.cacheable is False
    assert rt.metadata.ttl_class == "live-no-cache"
    assert TOOL_REGISTRY.get("generate_mesh") is None


def test_the_roster_is_the_meshers_the_tree_carries():
    assert registered_meshers() == ("om2d", "reg_grid")


def test_no_resolution_declaration_survives_anywhere():
    """Nothing refuses a resolution: no contract type, no row, no template
    and no mesher declares a range a resolution is held to."""
    root = Path(__file__).resolve().parents[2]
    words = ("Resolution" + "Spec", "resolution_" + "declarations",
             "resolution_" + "specs", "EDGE_RESOLUTION_" + "SPECS")
    found = [f"{path.relative_to(root)}: {word}"
             for top in ("trid3nt_server", "contracts", "plugin")
             for path in (root / top).rglob("*")
             if path.suffix in (".py", ".yaml", ".json") and path.is_file()
             for word in words if word in path.read_text(errors="ignore")]
    assert not found, found


def _bed(tmp_path: Path, cell_deg: float) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    tif = tmp_path / "bed.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=200, width=200, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-80.01, 26.01, cell_deg,
                                             cell_deg)) as dst:
        dst.write(np.full((1, 200, 200), -5.0, dtype="float32"))
    return tif


def _lattice(tif: Path, **stated):
    from trid3nt_server.tools.mesh.tool import MeshTool, mesh_op

    recipe = MeshTool.build_mesh(
        mesher="reg_grid", kind="structured_grid",
        extent=(-80.0, 25.99, -79.99, 26.0),
        ops=[mesh_op("set_bed", source=str(tif))], **stated)
    return get_mesher("reg_grid").build(recipe)


def test_a_mesh_with_no_stated_edge_takes_the_bed_s_cell(tmp_path):
    """One arcsecond at 26 N is 27.8 m across and 30.9 m tall; the finer side
    is the edge, and the mesh says where it came from."""
    mesh = _lattice(_bed(tmp_path, 1 / 3600))
    assert mesh.meta["resolution_m"] == pytest.approx(27.8, abs=0.1)
    assert "the cell of set_bed source" in mesh.meta["edge_notes"][0]


def test_an_edge_far_finer_than_the_data_runs_and_says_so(tmp_path):
    mesh = _lattice(_bed(tmp_path, 1 / 3600), resolution_m=2.0)
    assert mesh.meta["resolution_m"] == 2.0 and mesh.node_count > 250_000
    assert mesh.has_bed
    assert any("2 m edge is finer than the bed's" in note
               for note in mesh.meta["bed_notes"])


def test_no_edge_and_no_raster_is_asked_for_by_name():
    from trid3nt_server.tools.mesh.tool import MeshTool

    recipe = MeshTool.build_mesh(mesher="reg_grid", kind="structured_grid",
                                 extent=(-80.0, 25.99, -79.99, 26.0))
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("reg_grid").build(recipe)
    assert excinfo.value.error_code == "MESH_EDGE_UNSTATED"


@pytest.mark.parametrize("mesher,expected", [
    ("om2d", ("unstructured_tri",)),
    ("reg_grid", ("structured_grid",)),
])
def test_each_mesher_declares_the_kinds_it_makes(mesher, expected):
    assert get_mesher(mesher).kinds == expected


def test_an_op_a_mesher_never_registered_is_refused_by_name():
    from trid3nt_server.tools.mesh.meshers import resolve_op

    with pytest.raises(MeshToolError) as excinfo:
        resolve_op(get_mesher("om2d"), "open_boundary_side")
    assert excinfo.value.error_code == "MESH_OP_UNKNOWN"
    assert "open_boundary_side" in str(excinfo.value)


def test_2dm_round_trip():
    # two triangles in UTM metres.
    pts = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    z = np.array([610.0, 612.5, 615.0, 611.0])
    text = write_2dm_arrays(pts, cells, z)
    assert text.startswith("MESH2D")
    assert "E3T 1 1 2 3 1" in text
    assert "ND 1 500000.000000 3880000.000000 610.000000" in text

    # write to a temp file and read back.
    import tempfile
    from pathlib import Path

    p = Path(tempfile.mkdtemp()) / "m.2dm"
    p.write_text(text)
    rp, rc, rz = read_2dm_mesh(str(p))
    assert rp.shape == (4, 2) and rc.shape == (2, 3)
    assert np.allclose(rp, pts)
    assert np.allclose(rz, z)
    assert rc.tolist() == cells.tolist()


def test_an_adopted_layer_drops_the_meta_bound_to_the_topology_it_replaced(tmp_path):
    """A hand-edited layer is a different topology, so the per-solver geometry the
    mesher wrote and the probes measured on the old cells must not ride into the
    accepted artifact under the edited mesh's name."""
    from trid3nt_server.tools.mesh.meshers import Mesh
    from trid3nt_server.tools.mesh.session import MeshSession
    from trid3nt_server.tools.mesh.tool import tool

    pts = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    z = np.array([10.0, 11.0, 12.0, 13.0])
    edited = tmp_path / "edited.2dm"
    edited.write_text(write_2dm_arrays(pts, cells, z))

    session = MeshSession(
        tool.build_mesh(mesher="reg_grid", extent=(-83.5, 35.0, -83.4, 35.09),
                        resolution_m=2000.0),
        workdir=tmp_path)
    session._mesh = Mesh(
        points=pts[:3], cells=np.array([[0, 1, 2]]), crs_authid="EPSG:32616",
        bed=z[:3],
        meta={"utm_epsg": 32616,
              "probes": {"open_node_count": 93},
              "artifact": {
                           "open_boundary_info": {"open_node_count": 93},
                           "provenance": {"mesher": "om2d"}}})

    session.adopt_layer(str(edited))
    after = session.mesh

    assert after.node_count == 4 and after.element_count == 2
    assert "probes" not in after.meta, "probes survived the adopted layer"
    # What is ABOUT the domain rather than about its cells still rides.
    assert after.meta["utm_epsg"] == 32616
    assert after.meta["artifact"]["provenance"]["mesher"] == "om2d"


def test_read_2dm_rejects_empty():
    import tempfile
    from pathlib import Path

    p = Path(tempfile.mkdtemp()) / "empty.2dm"
    p.write_text("MESH2D\n")
    with pytest.raises(MeshNodeError):
        read_2dm_mesh(str(p))


def test_sidecar_key_derivation():
    got = sidecar_key_for_mesh_uri("s3://cache/mesh/01ABC/mesh.2dm")
    assert got == ("cache", "mesh/01ABC/mesh_artifact.json")


def test_sidecar_key_non_s3_is_none():
    assert sidecar_key_for_mesh_uri("/local/mesh.2dm") is None


def test_case_stash_roundtrip():
    stash_mesh_artifact("caseX", _artifact(mesh_id="m1"))
    stash_mesh_artifact("caseX", _artifact(mesh_id="m2"))
    got = stashed_mesh_artifacts("caseX")
    assert [a.mesh_id for a in got] == ["m1", "m2"]  # most-recent last
    assert stashed_mesh_artifacts("noSuchCase") == []


def test_find_case_mesh_artifacts_stash_first():
    stash_mesh_artifact("caseY", _artifact(mesh_id="mY"))
    got = find_case_mesh_artifacts(case_id="caseY")
    assert [a.mesh_id for a in got] == ["mY"]


def test_mesh_artifact_json_roundtrip():
    art = _artifact()
    back = MeshArtifact.from_json(art.to_json())
    assert back.mesh_id == art.mesh_id
    assert back.bbox == art.bbox  # tuple restored from JSON list


