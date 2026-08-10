"""Offline tests for the standalone ``generate_mesh`` tool + the mesh precondition
gate (ADR 0200).

The container-driven build (OceanMesh2D image + network) is proven live; here we
exercise the PURE surfaces: registration, mode inference, the MDAL 2dm
writer/reader round-trip, the mesh-artifact record + engine-compat gatekeeper, the
case-scoped stash + sidecar-key derivation, and the precondition-gate decision
logic (auto-default use, incompatible skip, no-mesh) with no live session.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    mesh_compatible_with_engine,
    sidecar_key_for_mesh_uri,
    stash_mesh_artifact,
    stashed_mesh_artifacts,
)
from trid3nt_server.agent.workflows.mesh.generate_mesh.generate_mesh import (
    _infer_mode,
    _write_2dm,
)
from trid3nt_server.agent.workflows.mesh.precondition_gate import gate_supplied_mesh
from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
    read_2dm_mesh,
)


def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01ABC", name="Coweeta watershed", mode="watershed",
        display_uri="s3://cache/mesh/01ABC/mesh.2dm",
        slf_uri="s3://cache/mesh/01ABC/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=(-83.5, 35.0, -83.4, 35.09),
        engine_compat=["telemac"])
    base.update(over)
    return MeshArtifact(**base)


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #
def test_generate_mesh_registered():
    rt = TOOL_REGISTRY.get("generate_mesh")
    assert rt is not None
    assert rt.metadata.cacheable is False
    assert rt.metadata.ttl_class == "live-no-cache"


# --------------------------------------------------------------------------- #
# Mode inference.
# --------------------------------------------------------------------------- #
def test_infer_mode_pour_point_is_watershed():
    assert _infer_mode("auto", (-83.4, 35.05), None) == "watershed"


def test_infer_mode_coastal_default_without_pour_point():
    assert _infer_mode("auto", None, (-82.9, 27.5, -82.4, 28.0)) == "coastal_water_edge"


def test_infer_mode_explicit_override():
    assert _infer_mode("coastal", (-83.4, 35.05), None) == "coastal_water_edge"
    assert _infer_mode("watershed", None, (0, 0, 1, 1)) == "watershed"


# --------------------------------------------------------------------------- #
# 2dm writer/reader round-trip (MDAL display face + supplied-mesh node parse).
# --------------------------------------------------------------------------- #
def test_2dm_round_trip():
    # two triangles in UTM metres.
    pts = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    z = np.array([610.0, 612.5, 615.0, 611.0])
    text = _write_2dm(pts, cells, z)
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


def test_read_2dm_rejects_empty():
    import tempfile
    from pathlib import Path

    from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
        MeshAcquisitionError,
    )

    p = Path(tempfile.mkdtemp()) / "empty.2dm"
    p.write_text("MESH2D\n")
    with pytest.raises(MeshAcquisitionError):
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


def test_compat_schism_missing_gr3():
    # a watershed mesh carries no .gr3, so SCHISM cannot consume it.
    ok, reason = mesh_compatible_with_engine(_artifact(gr3_uri=None), "schism")
    assert ok is False and "gr3" in reason.lower()


def test_compat_schism_needs_open_boundary():
    # a mesh WITH a .gr3 + bathymetry but NO designated open boundary is still
    # incompatible with SCHISM (no seaward boundary to force tides/T-S at).
    ok, reason = mesh_compatible_with_engine(
        _artifact(gr3_uri="s3://cache/mesh/01ABC/hgrid.gr3"), "schism")
    assert ok is False and "open" in reason.lower()


def test_compat_schism_ok_with_open_boundary():
    ok, reason = mesh_compatible_with_engine(
        _artifact(mode="coastal_water_edge",
                  gr3_uri="s3://cache/mesh/01ABC/hgrid.gr3",
                  open_boundary_info={"open_boundary_side": "south",
                                      "open_node_count": 42}),
        "schism")
    assert ok is True and reason == "compatible"


def test_compat_swan_regular_grid_always_false():
    # the SWAN worker is regular-grid only: it can NEVER consume a user mesh, even
    # one carrying a fort.14 -- the honest answer names the regular-grid reason.
    ok, reason = mesh_compatible_with_engine(
        _artifact(fort14_uri="s3://cache/mesh/01ABC/fort.14"), "swan")
    assert ok is False and "regular-grid" in reason.lower()


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
    # a watershed mesh (no .gr3) offered to SCHISM: skipped, run proceeds fresh.
    stash_mesh_artifact("gateCaseB", _artifact(mesh_id="gm2", gr3_uri=None))
    d = asyncio.run(gate_supplied_mesh(
        tool_name="schism_tidal_hydro", engine="schism", input_mode="auto",
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
from trid3nt_server.agent.workflows.mesh.artifact import (  # noqa: E402
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


def test_infer_mode_hecras_via_mesh_mode():
    assert _infer_mode("hecras", (-83.4, 35.05), None) == "hecras_rog"


def test_infer_mode_hecras_via_engine_hint():
    assert _infer_mode("auto", (-83.4, 35.05), None, "hecras") == "hecras_rog"


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


def test_hecras_mesh_declines_telemac_and_schism():
    a = _hecras_artifact()
    assert mesh_compatible_with_engine(a, "telemac")[0] is False
    assert mesh_compatible_with_engine(a, "schism")[0] is False
    # ... and a TELEMAC watershed mesh declines HEC-RAS (no bundle):
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
