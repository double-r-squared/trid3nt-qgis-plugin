"""Offline tests for the registered meshers + the mesh artifact and its gate.

Every build here is container-driven or network-driven and is proven live; what
this file exercises are the PURE surfaces: which meshers the router registers and
what each one declares, the display-face round trip, the mesh-artifact record and
its engine-compat gatekeeper, the case-scoped stash and sidecar-key derivation,
and the precondition gate's decision logic with no live session.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    mesh_compatible_with_engine,
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
from trid3nt_server.workflows.mesh.precondition_gate import gate_supplied_mesh
from trid3nt_server.workflows.mesh.watershed import read_2dm_mesh



def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01ABC", name="Coweeta catchment", mode="om2d",
        display_uri="s3://cache/mesh/01ABC/mesh.2dm",
        slf_uri="s3://cache/mesh/01ABC/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=(-83.5, 35.0, -83.4, 35.09),
        engine_compat=["telemac"])
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
    mesher wrote, the bundle an engine re-realizes from, and the probes measured on
    the old cells must not ride into the accepted artifact - the solver would get
    the pre-edit mesh under the edited mesh's name."""
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
              "artifact": {"engine_compat": ["telemac"],
                           "open_boundary_info": {"open_node_count": 93},
                           "provenance": {"mesher": "om2d"}}})

    after = get_mesher("om2d").action("apply_layer_edits").apply(
        before, layer=str(p))

    assert after.node_count == 4 and after.element_count == 2
    for key in ("files", "bundle", "probes"):
        assert key not in after.meta, f"{key} survived the adopted layer"
    # The CLAIMS made about those cells go with them: the edited mesh states its
    # engine compatibility afresh from what it can actually back.
    for key in ("engine_compat", "open_boundary_info"):
        assert key not in after.meta["artifact"], f"{key} survived the adopted layer"
    # What is ABOUT the domain rather than about its cells still rides.
    assert after.meta["utm_epsg"] == 32616
    assert after.meta["artifact"]["provenance"]["mesher"] == "om2d"


def test_read_2dm_rejects_empty():
    import tempfile
    from pathlib import Path

    from trid3nt_server.workflows.mesh.watershed import MeshGenerationError

    p = Path(tempfile.mkdtemp()) / "empty.2dm"
    p.write_text("MESH2D\n")
    with pytest.raises(MeshGenerationError):
        read_2dm_mesh(str(p))


# --------------------------------------------------------------------------- #
# Engine compatibility gatekeeper.
# --------------------------------------------------------------------------- #
def test_compat_telemac_ok():
    ok, reason = mesh_compatible_with_engine(_artifact(), "telemac")
    assert ok is True and reason == "compatible"


def test_compat_telemac_needs_bathymetry():
    ok, reason = mesh_compatible_with_engine(
        _artifact(has_bathymetry=False), "telemac")
    assert ok is False and "bathymetry" in reason.lower()


@pytest.mark.parametrize("engine", ["schism", "swan"])
def test_an_engine_the_tree_no_longer_carries_has_no_rule_to_quote(engine):
    """A requirement row written for an absent solver is a claim nothing backs.
    Until each engine returns and authors its row from the needs it has THEN, the
    honest answer is that no rule is registered - not a richer decline standing on
    fields no mesher writes."""
    ok, reason = mesh_compatible_with_engine(_artifact(), engine)
    assert ok is False and "no mesh-compatibility rule" in reason


def test_compat_unknown_engine_is_incompatible():
    ok, reason = mesh_compatible_with_engine(_artifact(), "made_up_engine")
    assert ok is False and "no mesh-compatibility rule" in reason


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
    assert back.engine_compat == ["telemac"]


# --------------------------------------------------------------------------- #
# Precondition-gate decision logic (no live session -> auto default).
# --------------------------------------------------------------------------- #
def test_gate_no_mesh_returns_no_use():
    d = asyncio.run(gate_supplied_mesh(
        tool_name="telemac_rain_on_grid", engine="telemac", input_mode="auto",
        case_id="emptyCase"))
    assert d.use is False and d.artifact is None


def test_gate_auto_default_uses_compatible_mesh():
    stash_mesh_artifact("gateCaseA", _artifact(mesh_id="gm1"))
    d = asyncio.run(gate_supplied_mesh(
        tool_name="telemac_rain_on_grid", engine="telemac", input_mode="auto",
        case_id="gateCaseA"))
    assert d.use is True
    assert d.artifact is not None and d.artifact.mesh_id == "gm1"
    assert d.note and "labeled default" in d.note


def test_gate_incompatible_mesh_skipped_loud_no_use():
    # a TELEMAC mesh (no authoring bundle) offered to HEC-RAS: skipped, run
    # proceeds fresh.
    stash_mesh_artifact("gateCaseB", _artifact(mesh_id="gm2"))
    d = asyncio.run(gate_supplied_mesh(
        tool_name="hecras_flood_2d", engine="hecras", input_mode="auto",
        case_id="gateCaseB"))
    assert d.use is False and d.artifact is None
    assert d.note and "not compatible" in d.note


def test_gate_auto_default_off_declines():
    stash_mesh_artifact("gateCaseC", _artifact(mesh_id="gm3"))
    d = asyncio.run(gate_supplied_mesh(
        tool_name="telemac_rain_on_grid", engine="telemac", input_mode="auto",
        case_id="gateCaseC", default_use=False))
    assert d.use is False


# --------------------------------------------------------------------------- #
# HEC-RAS RoG channel-refined mesh (ADR 0211): mode, artifact bundle, compat, gate.
# --------------------------------------------------------------------------- #
from trid3nt_server.workflows.mesh.artifact import (  # noqa: E402
    HECRAS_INPUT_KEYS, materialize_hecras_mesh_inputs,
)


def _hecras_artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01HEC", name="Coweeta HEC-RAS RoG mesh", mode="hecras_rog",
        display_uri="s3://cache/mesh/01HEC/cells_lonlat.fgb", slf_uri=None,
        utm_epsg=32617, crs_authid="EPSG:32617", has_bathymetry=True,
        node_count=56131, element_count=19462, bbox=(-83.47, 35.02, -83.36, 35.10),
        engine_compat=["hecras"], cells_validated=True,
        channel_target_size_m=22.0, background_size_m=90.0,
        hecras_inputs={
            "seeds": "s3://cache/mesh/01HEC/seeds.f64",
            "breaklines": "s3://cache/mesh/01HEC/breaklines.json",
            "local_dem": "s3://cache/mesh/01HEC/local_dem.tif",
            "prep_json": "s3://cache/mesh/01HEC/prep.json",
            "catchment": "s3://cache/mesh/01HEC/catchment.geojson",
            "flowlines": "s3://cache/mesh/01HEC/flowlines.fgb"})
    base.update(over)
    return MeshArtifact(**base)


def test_hecras_artifact_json_roundtrip():
    a = _hecras_artifact()
    b = MeshArtifact.from_json(a.to_json())
    assert b.mode == "hecras_rog" and b.slf_uri is None
    assert b.hecras_inputs == a.hecras_inputs
    assert b.cells_validated is True
    assert b.channel_target_size_m == 22.0 and b.background_size_m == 90.0
    assert b.engine_compat == ["hecras"]


def test_hecras_compat_ok():
    ok, reason = mesh_compatible_with_engine(_hecras_artifact(), "hecras")
    assert ok is True and reason == "compatible"


def test_hecras_compat_missing_bundle_key_declined_loud():
    inputs = dict(_hecras_artifact().hecras_inputs)
    inputs.pop("local_dem")
    ok, reason = mesh_compatible_with_engine(
        _hecras_artifact(hecras_inputs=inputs), "hecras")
    assert ok is False and "local_dem" in reason


def test_hecras_compat_unvalidated_declined():
    ok, reason = mesh_compatible_with_engine(
        _hecras_artifact(cells_validated=False), "hecras")
    assert ok is False and "valid cell mesh" in reason


def test_hecras_mesh_declines_telemac():
    assert mesh_compatible_with_engine(_hecras_artifact(), "telemac")[0] is False
    # ... and a TELEMAC mesh declines HEC-RAS (no bundle):
    assert mesh_compatible_with_engine(_artifact(), "hecras")[0] is False


def test_gate_offers_hecras_mesh_auto_default():
    stash_mesh_artifact("hecGate", _hecras_artifact(mesh_id="hg1"))
    d = asyncio.run(gate_supplied_mesh(
        tool_name="hecras_flood_2d", engine="hecras", input_mode="auto",
        case_id="hecGate"))
    assert d.use is True
    assert d.artifact is not None and d.artifact.mesh_id == "hg1"


def test_materialize_hecras_bundle_downloads_required_keys():
    downloaded = {}

    class _FakeS3:
        def download_file(self, bucket, key, local):
            downloaded[key.rsplit("/", 1)[-1]] = local
            Path(local).write_text("x")

    with tempfile.TemporaryDirectory() as td:
        out = materialize_hecras_mesh_inputs(_hecras_artifact(), td, _FakeS3())
    assert set(out).issuperset({"seeds", "breaklines", "local_dem", "prep_json"})
    assert "seeds.f64" in downloaded and "prep.json" in downloaded


def test_materialize_hecras_bundle_missing_required_raises():
    inputs = dict(_hecras_artifact().hecras_inputs)
    inputs.pop("seeds")

    class _FakeS3:
        def download_file(self, *a):
            raise AssertionError("should not download when required key missing")

    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(ValueError, match="seeds"):
            materialize_hecras_mesh_inputs(
                _hecras_artifact(hecras_inputs=inputs), td, _FakeS3())
