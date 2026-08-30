"""Offline tests for the registered meshers + the mesh artifact and its gate.

Every build here is container-driven or network-driven and is proven live; what
this file exercises are the PURE surfaces: which meshers the router registers and
what each one declares, the display-face round trip, the mesh-artifact record and
its engine-compat gatekeeper, the case-scoped stash and sidecar-key derivation,
and the precondition gate's decision logic with no live session.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    sidecar_key_for_mesh_uri,
    stash_mesh_artifact,
    stashed_mesh_artifacts,
)
from trid3nt_server.emission.mesh_display import write_2dm_arrays
from trid3nt_server.workflows.mesh.meshers import (
    MeshToolError,
    get_mesher,
    registered_meshers,
)
from trid3nt_server.workflows.mesh.shared.nodes import MeshNodeError, read_2dm_mesh



def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01ABC", name="Coweeta catchment", mode="om2d",
        display_uri="s3://cache/mesh/01ABC/mesh.2dm",
        slf_uri="s3://cache/mesh/01ABC/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=(-83.5, 35.0, -83.4, 35.09),
        )
    base.update(over)
    return MeshArtifact(**base)


# --------------------------------------------------------------------------- #
# Registration: one tool, and the meshers behind it.
# --------------------------------------------------------------------------- #
def test_build_mesh_registered_and_the_standalone_builder_is_gone():
    rt = TOOL_REGISTRY.get("build_mesh")
    assert rt is not None
    assert rt.metadata.cacheable is False
    assert rt.metadata.ttl_class == "live-no-cache"
    assert TOOL_REGISTRY.get("generate_mesh") is None


def test_the_roster_is_the_meshers_the_tree_carries():
    assert registered_meshers() == ("om2d", "reg_grid")


def test_the_edge_band_declaration_survived_the_dissolution():
    """The 5 m floor and its reason are what a gate card quotes; a tool that
    absorbs another absorbs its declarations too."""
    meta = TOOL_REGISTRY["build_mesh"].metadata
    for param in ("min_edge_length_m", "max_edge_length_m"):
        spec = meta.resolution_spec_for(param)
        assert spec is not None and spec.min_value == 5.0
        assert spec.constraint_source == "solver" and spec.rationale


@pytest.mark.parametrize("mesher,expected", [
    ("om2d", {"kind", "extent", "bed", "refine"}),
    ("reg_grid", {"kind", "extent", "resolution_m"}),
])
def test_each_mesher_declares_its_own_fields(mesher, expected):
    assert set(get_mesher(mesher).fields) == expected


def test_a_field_a_mesher_never_declared_is_refused_by_name():
    from trid3nt_server.workflows.mesh.tool import validate_spec

    with pytest.raises(MeshToolError) as excinfo:
        validate_spec("om2d", {"extent": (0.0, 0.0, 1.0, 1.0),
                               "open_boundary_side": "south"})
    assert excinfo.value.error_code == "MESH_SPEC_UNKNOWN_FIELD"
    assert "open_boundary_side" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 2dm writer/reader round-trip (MDAL display face + supplied-mesh node parse).
# --------------------------------------------------------------------------- #
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


def test_an_adopted_layer_drops_the_meta_bound_to_the_topology_it_replaced():
    """A hand-edited layer is a different topology, so the per-solver geometry the
    mesher wrote and the probes measured on the old cells must not ride into the
    accepted artifact - the solver would get the pre-edit mesh under the edited
    mesh's name."""
    import tempfile
    from pathlib import Path

    from trid3nt_server.workflows.mesh.meshers import Mesh

    pts = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    z = np.array([10.0, 11.0, 12.0, 13.0])
    p = Path(tempfile.mkdtemp()) / "edited.2dm"
    p.write_text(write_2dm_arrays(pts, cells, z))

    before = Mesh(
        points=pts[:3], cells=np.array([[0, 1, 2]]), crs_authid="EPSG:32616",
        bed=z[:3],
        meta={"utm_epsg": 32616,
              "files": {"slf_uri": "/stale/mesh.slf", "cli_uri": "/stale/mesh.cli"},
              "probes": {"open_node_count": 93},
              "artifact": {
                           "open_boundary_info": {"open_node_count": 93},
                           "provenance": {"mesher": "om2d"}}})

    after = get_mesher("om2d").action("apply_layer_edits").apply(
        before, layer=str(p))

    assert after.node_count == 4 and after.element_count == 2
    for key in ("files", "probes"):
        assert key not in after.meta, f"{key} survived the adopted layer"
    # The CLAIMS made about those cells go with them: the edited mesh states its
    # engine compatibility afresh from what it can actually back.
    for key in ("open_boundary_info",):
        assert key not in after.meta["artifact"], f"{key} survived the adopted layer"
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


# --------------------------------------------------------------------------- #
# The artifact answers for its own readiness.
# --------------------------------------------------------------------------- #
def test_a_solve_ready_mesh_names_no_reason():
    assert _artifact().unsolvable_reason() is None


def test_a_mesh_with_no_geometry_file_says_so():
    reason = _artifact(slf_uri=None).unsolvable_reason()
    assert reason is not None and "no SELAFIN geometry" in reason


def test_a_bedless_mesh_with_no_staged_topology_says_so():
    reason = _artifact(has_bathymetry=False).unsolvable_reason()
    assert reason is not None and "no sampled bed" in reason


def test_a_bedless_mesh_whose_bed_is_fitted_onto_a_staged_topology_is_ready():
    """A bundle-realized mesh carries its ground in the topology it stages, so it
    is solve-ready even though no bed was sampled at authoring."""
    art = _artifact(has_bathymetry=False, topology_uri="s3://cache/m/mesh.npz")
    assert art.unsolvable_reason() is None


# --------------------------------------------------------------------------- #
# Sidecar-key derivation + case stash.
# --------------------------------------------------------------------------- #
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


