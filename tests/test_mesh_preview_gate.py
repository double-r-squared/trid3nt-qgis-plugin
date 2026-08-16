"""Tests for the shared mesh preview/approve gate (ADR 0099, mesh M2)."""
from __future__ import annotations

from trid3nt_server.mesh.preview_gate import (
    MeshGateStats,
    build_mesh_gate_envelope,
    default_gate_mode,
    mesh_gate_should_fire,
)


def test_tin_default_mode_is_user_gated() -> None:
    assert default_gate_mode("tin") == "user_gated"


def test_regular_paradigms_default_auto() -> None:
    for p in ("regular_grid", "raster_cell_graph", "amr_patches"):
        assert default_gate_mode(p) == "auto"


def test_tin_gate_fires_by_default() -> None:
    assert mesh_gate_should_fire("tin") is True


def test_regular_gate_off_by_default_on_when_user_gated() -> None:
    assert mesh_gate_should_fire("regular_grid") is False
    assert mesh_gate_should_fire("regular_grid", "user_gated") is True
    # An explicit auto override never gates, even for tin.
    assert mesh_gate_should_fire("tin", "auto") is False


def test_envelope_rides_the_existing_spine() -> None:
    stats = MeshGateStats(
        paradigm="tin",
        engine="telemac",
        resolution_param="mesh_resolution_m",
        resolution_m=5.0,
        cells=1200,
        nodes=700,
        preview_uri="s3://runs/abc/mesh_preview.geojson",
        resolution_choices=[2.5, 5.0, 10.0],
        estimated_solve_seconds=90.0,
    )
    env = build_mesh_gate_envelope(stats, tool_name="telemac_river_dye")
    # It IS the payload-warning envelope (existing pause/resume + WS type).
    assert env.envelope_type == "tool-payload-warning"
    assert env.MESSAGE_TYPE == "tool-payload-warning"
    # Stats ride in the granularity enrichment.
    assert env.granularity is not None
    assert env.granularity.engine == "telemac"
    assert env.granularity.resolution_param == "mesh_resolution_m"
    assert env.granularity.estimated_active_cells == 1200
    assert env.granularity.suggested_resolution_m == 5.0
    assert env.granularity.resolution_choices == [2.5, 5.0, 10.0]
    # approve-to-proceed semantics: proceed available + preview handle threaded.
    assert "proceed" in env.options
    assert env.tool_args["mesh_preview_uri"] == "s3://runs/abc/mesh_preview.geojson"
    assert env.tool_args["mesh_resolution_m"] == 5.0


def test_regular_grid_sfincs_envelope() -> None:
    stats = MeshGateStats(
        paradigm="regular_grid",
        engine="sfincs",
        resolution_param="grid_resolution_m",
        resolution_m=100.0,
        cells=46000,
        resolution_choices=[50.0, 100.0, 200.0],
    )
    env = build_mesh_gate_envelope(stats, tool_name="sfincs_flood")
    assert env.granularity.engine == "sfincs"
    assert env.granularity.resolution_param == "grid_resolution_m"
    assert env.granularity.estimated_active_cells == 46000
