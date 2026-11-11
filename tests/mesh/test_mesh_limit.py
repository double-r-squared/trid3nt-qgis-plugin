"""A mesh whose estimated node count passes MESH_MAX_NODES refuses before it builds, in AUTO and user-gated alike."""

from __future__ import annotations

import pytest

from trid3nt_server import settings
from trid3nt_server.tools.mesh.meshers import MeshToolError
from trid3nt_server.tools.mesh.session import MeshSession
from trid3nt_server.tools.mesh.tool import tool

_AOI = (-83.50, 35.00, -83.40, 35.09)


def _session(tmp_path, resolution_m=400.0) -> MeshSession:
    return MeshSession(tool.build_mesh(
        mesher="reg_grid", kind="structured_grid", extent=_AOI,
        resolution_m=resolution_m), workdir=tmp_path)


@pytest.mark.parametrize("auto", [True, False])
def test_over_the_limit_refuses_by_name_in_either_mode(tmp_path, auto):
    token = settings.bind_run({"AUTO": auto, "MESH_MAX_NODES": 100})
    try:
        with pytest.raises(MeshToolError) as caught:
            _session(tmp_path).mesh
    finally:
        settings.reset_run(token)
    text = str(caught.value)
    assert caught.value.error_code == "MESH_TOO_LARGE"
    assert "mesh_resolution_m" in text and "MESH_MAX_NODES" in text
    assert "100" in text and "estimated" in text


@pytest.mark.parametrize("auto", [True, False])
def test_under_the_limit_builds(tmp_path, auto):
    token = settings.bind_run({"AUTO": auto, "MESH_MAX_NODES": 1_000_000})
    try:
        assert _session(tmp_path).mesh.node_count > 100
    finally:
        settings.reset_run(token)


def test_raising_the_setting_lets_the_same_ask_through(tmp_path):
    token = settings.bind_run({"MESH_MAX_NODES": 100})
    try:
        with pytest.raises(MeshToolError):
            _session(tmp_path).mesh
    finally:
        settings.reset_run(token)
    token = settings.bind_run({"MESH_MAX_NODES": 10_000_000})
    try:
        assert _session(tmp_path / "again").mesh.node_count > 100
    finally:
        settings.reset_run(token)


def test_the_default_refuses_a_mesh_far_past_it(tmp_path):
    with pytest.raises(MeshToolError, match="MESH_MAX_NODES"):
        _session(tmp_path, resolution_m=0.5).mesh


def _bed_session(tmp_path, cell_deg, **stated):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from trid3nt_server.tools.mesh.tool import mesh_op

    tif = tmp_path / "bed.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=200, width=200, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-80.01, 26.01, cell_deg, cell_deg)) as dst:
        dst.write(np.full((1, 200, 200), -5.0, dtype="float32"))
    return MeshSession(tool.build_mesh(
        mesher="reg_grid", kind="structured_grid",
        extent=(-80.0, 25.99, -79.99, 26.0),
        ops=[mesh_op("set_bed", source=str(tif))], **stated), workdir=tmp_path / "w")


def _built(session, limit):
    token = settings.bind_run({"MESH_MAX_NODES": limit})
    try:
        return session.mesh
    finally:
        settings.reset_run(token)


def test_unstated_takes_the_finest_edge_the_limit_fits_and_notes_it(tmp_path):
    # 1 arcsecond cell (about 28 m) over ~1.1 km2 is ~1,600 nodes; a limit of 400 is coarser
    mesh = _built(_bed_session(tmp_path, 1 / 3600), 400)
    edge = mesh.meta["resolution_m"]
    assert edge > 28.0 and mesh.node_count <= 400 * 1.5
    assert any("derived from MESH_MAX_NODES = 400" in n for n in mesh.meta["edge_notes"])


def test_unstated_on_a_small_domain_the_finest_data_wins(tmp_path):
    mesh = _built(_bed_session(tmp_path, 1 / 3600), 10_000_000)
    assert mesh.meta["resolution_m"] == pytest.approx(27.8, abs=0.1)
    assert "the cell of set_bed source" in mesh.meta["edge_notes"][0]


def test_a_stated_edge_is_used_as_stated_under_the_limit(tmp_path):
    mesh = _built(_bed_session(tmp_path, 1 / 3600, resolution_m=100.0), 400)
    assert mesh.meta["resolution_m"] == 100.0


def test_a_stated_edge_over_the_limit_refuses(tmp_path):
    with pytest.raises(MeshToolError, match="MESH_MAX_NODES"):
        _built(_bed_session(tmp_path, 1 / 3600, resolution_m=5.0), 400)


def test_raising_the_setting_makes_the_derived_edge_finer(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    coarse = _built(_bed_session(tmp_path / "a", 1 / 3600), 400).meta["resolution_m"]
    finer = _built(_bed_session(tmp_path / "b", 1 / 3600), 1000).meta["resolution_m"]
    assert finer < coarse


def _solve_built(session, *, budget, limit=10_000_000):
    session.solve_estimate = lambda nodes, edge: float(nodes)
    token = settings.bind_run({"MESH_MAX_NODES": limit, "SOLVE_BUDGET_S": budget})
    try:
        return session.mesh
    finally:
        settings.reset_run(token)


def test_unstated_edge_fits_the_solve_budget_and_the_run_notes_say_so(tmp_path):
    mesh = _solve_built(_bed_session(tmp_path, 1 / 3600), budget=400)
    assert mesh.meta["resolution_m"] > 28.0 and mesh.node_count <= 400 * 1.5
    assert any("derived from SOLVE_BUDGET_S = 400" in n for n in mesh.meta["edge_notes"])


def test_the_tighter_limit_sets_the_derived_edge(tmp_path):
    mesh = _solve_built(_bed_session(tmp_path, 1 / 3600), budget=10_000_000, limit=400)
    assert any("derived from MESH_MAX_NODES = 400" in n for n in mesh.meta["edge_notes"])


def test_raising_the_budget_makes_the_derived_edge_finer(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    coarse = _solve_built(_bed_session(tmp_path / "a", 1 / 3600), budget=400).meta["resolution_m"]
    finer = _solve_built(_bed_session(tmp_path / "b", 1 / 3600), budget=1000).meta["resolution_m"]
    assert finer < coarse


@pytest.mark.parametrize("auto", [True, False])
def test_a_stated_edge_over_the_solve_budget_refuses_by_name(tmp_path, auto):
    session = _bed_session(tmp_path, 1 / 3600, resolution_m=5.0)
    token = settings.bind_run({"AUTO": auto})
    try:
        with pytest.raises(MeshToolError) as caught:
            _solve_built(session, budget=400)
    finally:
        settings.reset_run(token)
    text = str(caught.value)
    assert caught.value.error_code == "SOLVE_OVER_BUDGET"
    assert "SOLVE_BUDGET_S" in text and "mesh_resolution_m" in text and "400" in text


def test_a_stated_edge_under_the_solve_budget_stands(tmp_path):
    mesh = _solve_built(_bed_session(tmp_path, 1 / 3600, resolution_m=100.0), budget=400)
    assert mesh.meta["resolution_m"] == 100.0


def test_with_the_budget_unset_nothing_is_fitted_or_refused(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    off = _solve_built(_bed_session(tmp_path / "a", 1 / 3600), budget=0)
    assert off.meta["resolution_m"] == pytest.approx(27.8, abs=0.1)
    assert not any("SOLVE_BUDGET_S" in n for n in off.meta["edge_notes"])
    assert _solve_built(_bed_session(tmp_path / "b", 1 / 3600, resolution_m=5.0), budget=0).meta["resolution_m"] == 5.0


def test_the_budget_is_off_by_default(tmp_path):
    from trid3nt_server.settings import setting

    assert setting("SOLVE_BUDGET_S") == 0


def test_an_engine_with_no_estimate_is_not_held_to_the_budget(tmp_path):
    token = settings.bind_run({"SOLVE_BUDGET_S": 1})
    try:
        mesh = _bed_session(tmp_path, 1 / 3600, resolution_m=5.0).mesh
    finally:
        settings.reset_run(token)
    assert mesh.meta["resolution_m"] == 5.0
